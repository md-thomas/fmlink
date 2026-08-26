#!/usr/bin/env python3
"""
Live OpenWave station: builds/parses real OpenWave frames and pushes them
through the actual modem chain (modulate -> pulse-shape on TX, matched-
filter -> demodulate -> sync/FEC on RX) to/from a peer station, relayed
through an `ota_sim` network simulator (see /data/Projects/ota_sim) that
models path loss, fading, AWGN, and latency between them.

Unlike pipeline_test.sh/file_xfer_test.sh (one-shot, file-based, single
process), this runs as a live, interactive, bidirectional process - type a
line, it gets framed/modulated/sent; incoming decoded messages print as
they arrive. Two of these plus one `python3 -m ota_sim.simulator` make up
a full simulated link. All three processes can run on one machine, or be
split across two or three separate machines - each just needs to be told
the others' network addresses (--sim-host/--sim-port).

Requires `ota_sim` installed: from this repo, `pip install -e ../ota_sim`.

Known v1 limitations:
  - Both stations in a session must be launched with the SAME --mode (and
    --fec on/off) - there is no in-band mode negotiation yet (that's
    tasklist.txt Phase 6.4). The receiver has to already know which
    modulation to demodulate with.
  - This is an interactive-messaging tool, not a bulk-file-transfer one:
    UDP's ~65KB single-datagram ceiling bounds worst-case on-air sample
    counts (low-order modulation + strong FEC + a near-MAX_PAYLOAD_LEN
    fragment can exceed it). Use file_xfer_test.sh for bulk transfers.
  - --ack retries per fragment (stop-and-wait: send one, await its own
    ACK, then the next), so a lost fragment only costs that fragment a
    retry, not the whole message - but fragments are still sent strictly
    one at a time, so more fragments means more round-trip latency, not
    more risk. True windowed/parallel selective-repeat (multiple
    fragments in flight at once) isn't implemented - would also need
    FragmentReassembler reworked to tolerate out-of-order arrival.

    # station K0MDT, talking to W1AW, via a simulator on this same machine
    python3 ota_station.py --station-id K0MDT --dst W1AW --sim-host 127.0.0.1

    # the other end
    python3 ota_station.py --station-id W1AW --dst K0MDT --sim-host 127.0.0.1

    # with FEC and a closed ACK loop
    python3 ota_station.py --station-id K0MDT --dst W1AW --sim-host 127.0.0.1 --fec --ack
"""
import argparse
import asyncio
import sys
import time

import numpy as np

import openwave_fec as fec
import openwave_link as link
import openwave_pulse as pulse
import openwave_sync as sync
from modulate import MODULATORS
from demodulate import DEMODULATORS

try:
    from ota_sim.simulator import encode_peer_id, PEER_ID_LEN
except ImportError:
    sys.exit("error: ota_sim is not installed - run `pip install -e ../ota_sim` "
             "from this repo (see /data/Projects/ota_sim/README.md)")

RETRY_POLL_INTERVAL_S = 0.2
DEFAULT_FEC_PRESET = 'r1_4_k7'
# Largest UDP payload a standard IPv4 socket can send in one datagram
# (65535 - 8-byte UDP header - 20-byte IPv4 header). A burst that would
# exceed this can't be sent at all - asyncio's transport swallows the
# resulting OSError internally and silently kills itself, so this has to
# be checked before calling sendto(), not caught after.
MAX_UDP_PAYLOAD_BYTES = 65507


def _human_size(num_bytes):
    """num_bytes -> auto-scaled 'N.NN KB (N bytes)' label, matching
    pipeline_test.sh's human_size() convention elsewhere in this project."""
    if num_bytes < 1024:
        label = f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        label = f"{num_bytes / 1024:.2f} KB"
    else:
        label = f"{num_bytes / (1024 * 1024):.2f} MB"
    return f"{label} ({num_bytes} bytes)"


def _waveform_modes_table():
    lines = ["Waveform Modes (openwave_link.py's WAVEFORM_MODES):"]
    for mode_id, info in sorted(link.WAVEFORM_MODES.items()):
        lines.append(f"  {mode_id}: {info['modulation']:<5} {info['symbol_rate']:>5} sym/s  "
                     f"(mode's own default fec_preset: {info['fec_preset']})")
    lines.append("")
    lines.append(f"FEC presets (openwave_fec.py's PRESETS): {', '.join(sorted(fec.PRESETS))}")
    return "\n".join(lines)


class Station(asyncio.DatagramProtocol):
    """One live OpenWave station. Sends text via send_text(); received,
    decoded messages arrive via the on_message callback. Talks to a peer
    only indirectly, through an ota_sim simulator's UDP relay - never
    directly to the peer's address."""

    def __init__(self, station_id, dst_id, mode_id=0, use_fec=False,
                 fec_preset=None, use_ack=False, on_message=None, verbose=False):
        self.station_id = station_id
        self.dst_id = dst_id
        self.mode_id = mode_id
        self.use_fec = use_fec
        self.use_ack = use_ack
        self.verbose = verbose
        self._on_message = on_message

        self.session_start = time.monotonic()
        self.stats = {
            'sent_messages': 0, 'sent_text_bytes': 0, 'sent_air_bytes': 0, 'sent_fragments': 0,
            'received_messages': 0, 'received_text_bytes': 0, 'received_air_bytes': 0,
        }

        mode_info = link.WAVEFORM_MODES[mode_id]
        self.modulation = mode_info['modulation']
        self._modulate = MODULATORS[self.modulation]
        self._demodulate = DEMODULATORS[self.modulation]
        # fec_preset defaults to the mode's own WAVEFORM_MODES entry, but can
        # be overridden independently - e.g. to use a stronger code than the
        # mode's documented default without changing WAVEFORM_MODES itself.
        self.fec_preset = fec_preset or mode_info['fec_preset']
        self._fec_generators, self._fec_k = fec.PRESETS[self.fec_preset]

        self.transport = None
        self._sim_addr = None
        self._seq = 0

        self.receiver = link.LinkReceiver(
            station_id=station_id,
            on_message=self._handle_message,
            on_ack_needed=self._on_ack_needed,
            on_ack_received=self._on_ack_received,
        )
        self.retry_mgr = link.RetryManager() if use_ack else None

        # Computed once here (depends only on mode/fec, not per-message) and
        # used as build_fragmented_frames()'s chunk_size in send_text(), so
        # ANY message length auto-splits into bursts that actually fit in
        # one UDP datagram - not just messages over link.MAX_PAYLOAD_LEN
        # (that's the protocol's own fragmentation threshold, unrelated to
        # what a given mode/FEC combo can fit on the wire; at a tight combo
        # like mode 0 + r1_4_k7 it can be far smaller than MAX_PAYLOAD_LEN).
        self.max_fragment_chars = max(1, self.max_single_fragment_chars())
        self._suppress_next_message = False
        self._pending_message_seq = None
        self._last_delivered_seq = None

    def _handle_message(self, text):
        if self._suppress_next_message:
            self._log(f"[rx] duplicate of the last message (already delivered) - "
                      f"the peer's ACK likely didn't make it back last time, so it retried")
            return
        self._last_delivered_seq = self._pending_message_seq
        self.stats['received_messages'] += 1
        self.stats['received_text_bytes'] += len(text.encode('utf-8'))
        self._log(f"{self.dst_id}: {text}")
        if self.verbose:
            self._log(f"[stats] {self.receiver.stats_line()}")
        if self._on_message:
            self._on_message(text)

    def _log(self, msg):
        print(msg, file=sys.stderr)

    # -- asyncio.DatagramProtocol -------------------------------------
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < PEER_ID_LEN:
            return
        payload = data[PEER_ID_LEN:]
        try:
            self._handle_received_samples(payload)
        except Exception as exc:  # a malformed/short burst shouldn't kill the station
            self._log(f"[rx error] {exc!r}")

    # -- RX pipeline: samples -> demod -> sync/FEC -> LinkReceiver -----
    def _handle_received_samples(self, payload):
        self.stats['received_air_bytes'] += len(payload)
        samples = np.frombuffer(payload, dtype=np.complex64)
        num_symbols = pulse.expected_symbol_count(len(samples))
        filtered = pulse.matched_filter(samples)
        offset = pulse.group_delay()
        symbols = pulse.decimate(filtered, sps=4, offset=offset)[:num_symbols]
        recovered = self._demodulate(symbols)

        if self.use_fec:
            frames = list(fec.decode_frames(recovered, self._fec_generators, self._fec_k))
        else:
            frames = list(sync.iter_frames_correlated(recovered))

        if not frames:
            self._log("[rx] no frame found in this burst - link too weak to even find sync "
                      "(try a shorter --distance-km or a higher --base-snr-db on the simulator)")

        for status, header, payload_bytes in frames:
            if status == 'ok':
                # LinkReceiver itself tracks a repeated seq as a 'dup' stat
                # but still delivers it (the right call for its other use -
                # offline analysis of a capture file, where you may want to
                # see every frame including retransmits) - a live station
                # should only ever show the user each message once, so
                # suppress on_message without touching LinkReceiver's own
                # counts/behavior. Compared against our OWN last-delivered
                # seq (not receiver.last_seq, which is overwritten by every
                # individual fragment fed, including ones that never
                # publish) - a retried multi-fragment message's LAST
                # fragment carries the same seq as the original run's last
                # fragment, which is exactly what LinkReceiver._publish()
                # uses to identify the whole reassembled message either
                # way, single-fragment or not.
                self._pending_message_seq = header['seq']
                self._suppress_next_message = (header['seq'] == self._last_delivered_seq)
                counts_before = dict(self.receiver.counts)
                self.receiver.feed_frame(header, payload_bytes)
                self._log_fragment_events(counts_before)
            else:
                self._log(f"[rx] {status} - burst not recovered "
                          f"(try --fec, a shorter --distance-km, or a higher --base-snr-db)")
                self.receiver.feed_crc_failure(status)

    def _log_fragment_events(self, counts_before):
        """FragmentReassembler (via LinkReceiver) tracks aborted/overflowed/
        discarded fragment runs as plain counters, with no callback of its
        own - a missing fragment just leaves it waiting forever, silently,
        until a later mismatched fragment (or a fresh run starting) trips
        one of these counts. Without this, a multi-fragment message that
        loses even one fragment along the way vanishes with zero
        indication why - the single biggest reason a longer message
        'just doesn't arrive' while individual bursts look fine. Fragments
        themselves are NOT ACK/retry-tracked (see build_fragmented_frames'
        docstring), so a lost fragment's data is unrecoverable either way -
        this only makes that loss visible instead of silent."""
        c = self.receiver.counts
        if c['frag_aborted'] > counts_before['frag_aborted']:
            self._log("[rx] a fragmented message was abandoned mid-run - a fragment never "
                      "arrived (lost/corrupted) before a later one broke the sequence. "
                      "Fragments aren't retried, so this message is unrecoverable - try "
                      "a shorter message, a stronger --fec, or a better link.")
        if c['frag_overflow'] > counts_before['frag_overflow']:
            self._log("[rx] a fragmented message exceeded the fragment-count safety limit "
                      "without completing - abandoned.")
        if c['frag_discarded'] > counts_before['frag_discarded']:
            self._log("[rx] ignored a stray fragment that didn't match any message in progress "
                      "(wrong sequence/index/source - likely a symptom of an earlier lost fragment).")

    def register(self):
        """Send a zero-length 'hello' datagram to the simulator so it
        learns this station's address right away, even if this station
        never sends anything itself (a receive-only listener would
        otherwise be unreachable - the simulator only learns a peer's
        address from something it has sent)."""
        self.transport.sendto(encode_peer_id(self.station_id), self._sim_addr)

    # -- TX pipeline: frame bytes -> FEC -> modulate -> pulse-shape -----
    def _transmit_frame(self, frame_bytes):
        encoded = (fec.encode_frame(frame_bytes, self._fec_generators, self._fec_k)
                   if self.use_fec else frame_bytes)
        symbols = self._modulate(encoded)
        samples = pulse.pulse_shape(symbols)
        out = encode_peer_id(self.station_id) + samples.astype(np.complex64).tobytes()
        if len(out) > MAX_UDP_PAYLOAD_BYTES:
            fec_note = f"--fec-preset {self.fec_preset}" if self.use_fec else "no FEC"
            self._log(f"[tx error] burst is {_human_size(len(out))}, over UDP's "
                      f"{_human_size(MAX_UDP_PAYLOAD_BYTES)} datagram limit - NOT sent. "
                      f"({fec_note} + {self.modulation} at mode {self.mode_id} is too expensive "
                      f"for this message length; try a higher --mode/fewer bits of FEC redundancy, "
                      f"or a shorter message)")
            return
        self.transport.sendto(out, self._sim_addr)
        self.stats['sent_air_bytes'] += len(out)

    def max_single_fragment_chars(self):
        """Binary-search the largest single-fragment ASCII message that
        fits in one UDP datagram at this station's current --mode/--fec
        settings - purely informational, computed once at startup so the
        size ceiling (see _transmit_frame) is visible up front instead of
        discovered message-by-message. A longer message just auto-
        fragments into multiple bursts (build_fragmented_frames), each
        independently bounded the same way - this is the per-burst limit,
        not a hard cap on total message length."""
        def burst_size(n):
            frame = link.build_frame(b'x' * n, seq=0, mode_id=self.mode_id,
                                      src_id=self.station_id.encode(), dst_id=self.dst_id.encode())
            encoded = (fec.encode_frame(frame, self._fec_generators, self._fec_k)
                       if self.use_fec else frame)
            symbols = self._modulate(encoded)
            samples = pulse.pulse_shape(symbols)
            return len(samples.astype(np.complex64).tobytes()) + PEER_ID_LEN

        lo, hi = 0, link.MAX_PAYLOAD_LEN
        if burst_size(hi) <= MAX_UDP_PAYLOAD_BYTES:
            return hi
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if burst_size(mid) <= MAX_UDP_PAYLOAD_BYTES:
                lo = mid
            else:
                hi = mid - 1
        return lo

    async def send_text(self, text):
        """Build, FEC-encode (if enabled), modulate, pulse-shape, and
        transmit one message. Longer-than-max_fragment_chars messages
        auto-split into independent bursts (build_fragmented_frames()'s
        existing semantics - each fragment is its own frame/burst),
        chunked to what actually fits in one UDP datagram at this
        station's --mode/--fec settings, not link.MAX_PAYLOAD_LEN.

        With --ack, fragments are sent stop-and-wait: each one is
        transmitted and its OWN ACK awaited (with retry) before the next
        is sent, so a lost fragment only costs that fragment a retry, not
        the whole message - see openwave_link.py's LinkReceiver, which
        acks every fragment individually as it arrives, not just the last
        one. If any single fragment exhausts its retries, the remaining
        fragments aren't sent - a still-incomplete message isn't worth
        the extra channel traffic once one piece has already failed."""
        text_bytes = text.encode('utf-8')
        flags = link.FLAG_ACK_REQUESTED if self.use_ack else 0
        frames, self._seq = link.build_fragmented_frames(
            text_bytes, seq_start=self._seq, mode_id=self.mode_id,
            chunk_size=self.max_fragment_chars,
            flags=flags, src_id=self.station_id.encode(), dst_id=self.dst_id.encode(),
        )
        first_seq = (self._seq - len(frames)) % link.SEQ_MODULUS

        self.stats['sent_messages'] += 1
        self.stats['sent_text_bytes'] += len(text_bytes)
        self.stats['sent_fragments'] += len(frames)
        air_bytes_before = self.stats['sent_air_bytes']

        if not self.use_ack:
            for frame in frames:
                self._transmit_frame(frame)
            if self.verbose:
                air_bytes = self.stats['sent_air_bytes'] - air_bytes_before
                self._log(f"[stats] sent {_human_size(len(text_bytes))} text -> "
                          f"{len(frames)} fragment(s), {_human_size(air_bytes)} on-air "
                          f"(no --ack: no delivery confirmation)")
            return

        started_at = time.monotonic()
        for i, frame in enumerate(frames):
            seq = (first_seq + i) % link.SEQ_MODULUS
            if not await self._send_with_retry(seq, frame):
                self._log(f"[ack] gave up on fragment {i + 1}/{len(frames)} (seq {seq}) - "
                          f"message incomplete, aborting remaining fragments")
                return
            if self.verbose and len(frames) > 1:
                self._log(f"[ack] fragment {i + 1}/{len(frames)} delivered")

        elapsed = time.monotonic() - started_at
        note = ""
        if self.verbose:
            air_bytes = self.stats['sent_air_bytes'] - air_bytes_before
            bps = (len(text_bytes) * 8 / elapsed) if elapsed > 0 else 0
            note = (f" ({_human_size(len(text_bytes))} text / {_human_size(air_bytes)} on-air "
                    f"in {elapsed:.2f}s, {bps:.0f} bps text-level)")
        self._log(f"[ack] delivered - {len(frames)} fragment(s) acknowledged by {self.dst_id}{note}")

    # -- ACK/retry ------------------------------------------------------
    def _on_ack_needed(self, seq):
        ack_frame = link.build_ack_frame(
            seq, mode_id=self.mode_id, src_id=self.station_id.encode(),
            dst_id=self.dst_id.encode(),
        )
        self._transmit_frame(ack_frame)

    def _on_ack_received(self, seq):
        if self.retry_mgr:
            self.retry_mgr.note_ack(seq)

    async def _send_with_retry(self, seq, frame_bytes):
        """Transmit frame_bytes (seq already baked into its header) and
        wait for its ACK, retrying per RetryManager's policy - reused
        as-is, just reset and driven fresh per fragment here instead of
        once per whole message. Returns True if acked, False if retries
        were exhausted."""
        self.retry_mgr.reset()
        # note_sent() before the actual transmit: an incoming ACK is only
        # ever processed by the event loop on a later iteration (never
        # synchronously inside our own sendto() call), so this ordering
        # doesn't matter for real UDP - but marking "pending" first, before
        # anything could possibly race it, costs nothing and removes the
        # assumption entirely.
        self.retry_mgr.note_sent(seq)
        self._transmit_frame(frame_bytes)
        while True:
            await asyncio.sleep(RETRY_POLL_INTERVAL_S)
            state = self.retry_mgr.poll()
            if state == 'idle':
                return True
            if state == 'retry':
                if self.verbose:
                    self._log(f"[ack] seq {seq} timed out, retrying")
                self.retry_mgr.note_sent(seq)
                self._transmit_frame(frame_bytes)
            elif state == 'give_up':
                return False

    def stats_summary(self):
        """Session totals: messages/bytes sent and received, at both the
        text level (what you typed) and the on-air level (actual bytes
        transmitted as complex samples - inflated by FEC and, at low-order
        modulation, by bits/symbol), plus average throughput over the
        whole session (includes idle time between messages, so this is a
        session-average, not an instantaneous rate) and the underlying
        LinkReceiver's own counters (OK/CRC fail/Dup/Lost/etc)."""
        elapsed = time.monotonic() - self.session_start
        s = self.stats
        sent_bps = (s['sent_text_bytes'] * 8 / elapsed) if elapsed > 0 else 0
        recv_bps = (s['received_text_bytes'] * 8 / elapsed) if elapsed > 0 else 0
        lines = [
            f"[{self.station_id}] session stats after {elapsed:.1f}s:",
            f"  sent:     {s['sent_messages']} message(s), {_human_size(s['sent_text_bytes'])} text "
            f"-> {_human_size(s['sent_air_bytes'])} on-air ({s['sent_fragments']} fragment(s) total, "
            f"~{sent_bps:.0f} bps text-level session average)",
            f"  received: {s['received_messages']} message(s), {_human_size(s['received_text_bytes'])} text "
            f"<- {_human_size(s['received_air_bytes'])} on-air "
            f"(~{recv_bps:.0f} bps text-level session average)",
            f"  link:     {self.receiver.stats_line()}",
        ]
        return "\n".join(lines)


async def _stdin_lines():
    """Yield lines typed on stdin without blocking the event loop."""
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        line = line.rstrip('\n')
        if line:
            yield line


async def run(args):
    station = Station(
        station_id=args.station_id, dst_id=args.dst, mode_id=args.mode,
        use_fec=args.fec, fec_preset=args.fec_preset, use_ack=args.ack,
        verbose=args.verbose,
    )
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: station, local_addr=('0.0.0.0', args.local_port),
    )
    station._sim_addr = (args.sim_host, args.sim_port)
    station.register()
    local_port = transport.get_extra_info('sockname')[1]
    fec_note = f"on ({station.fec_preset})" if args.fec else "off"
    print(f"[{args.station_id}] mode={args.mode} ({station.modulation}) fec={fec_note} "
          f"ack={'on' if args.ack else 'off'} - local port {local_port}, "
          f"relaying via {args.sim_host}:{args.sim_port}.", file=sys.stderr)
    print(f"[{args.station_id}] max ~{station.max_fragment_chars} chars per burst at these "
          f"settings (UDP's {MAX_UDP_PAYLOAD_BYTES}-byte datagram limit) - longer messages "
          f"auto-fragment into multiple bursts. Type a message and press Enter.",
          file=sys.stderr)

    try:
        async for line in _stdin_lines():
            await station.send_text(line)
    finally:
        transport.close()
        print(station.stats_summary(), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, epilog=_waveform_modes_table(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--station-id', required=True, help="This station's own callsign")
    parser.add_argument('--dst', default='ALL', help="Peer's callsign (default: ALL/broadcast)")
    parser.add_argument('--sim-host', required=True, help='ota_sim simulator address')
    parser.add_argument('--sim-port', type=int, default=9500, help='ota_sim simulator port (default: 9500)')
    parser.add_argument('--local-port', type=int, default=0,
                         help='Local UDP port to bind (default: 0 = OS-assigned; set explicitly '
                              'when crossing machines/firewalls)')
    parser.add_argument('--mode', type=int, default=0, choices=sorted(link.WAVEFORM_MODES),
                         help='Waveform Mode ID - selects modulation/symbol rate (see Waveform '
                              'Modes table below; default: 0)')
    parser.add_argument('--fec', action='store_true',
                         help=f'FEC-encode/decode using --fec-preset (default: {DEFAULT_FEC_PRESET})')
    parser.add_argument('--fec-preset', choices=sorted(fec.PRESETS), default=DEFAULT_FEC_PRESET,
                         help=f'FEC preset to use when --fec is set - overrides the --mode\'s own '
                              f'default preset (default: {DEFAULT_FEC_PRESET}; see FEC presets below)')
    parser.add_argument('--ack', action='store_true',
                         help='Request ACKs and retry on timeout (a timeout resends the whole '
                              'message, all fragments, not just the missing piece)')
    parser.add_argument('--verbose', '-v', action='store_true',
                         help='Print per-message stats as they happen (size, fragment count, '
                              'timing/throughput, LinkReceiver counters). A session summary '
                              '(bytes/messages sent+received, elapsed time) always prints on exit '
                              'regardless of this flag.')
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
