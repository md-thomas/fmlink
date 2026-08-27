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
  - Without --auto, both stations must be launched with the SAME --mode
    (and --fec on/off) - the receiver has to already know which modulation
    to demodulate with. --auto negotiates this automatically (see
    negotiate()/openwave_link.py's MODE_SNR_TABLE), but only once at
    startup - it's not continuous/mid-session rate adaptation, and its
    threshold table is only empirically verified for BPSK so far (see
    MODE_SNR_TABLE's own comment).
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

    # let both ends negotiate mode/FEC automatically instead of matching --mode by hand
    python3 ota_station.py --station-id K0MDT --dst W1AW --sim-host 127.0.0.1 --auto --ack
"""
import argparse
import asyncio
import sys
import time

import numpy as np

import openwave_fec as fec
import openwave_link as link
import openwave_modem as modem
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
# --auto's mode-negotiation handshake always runs at this fixed mode/FEC,
# regardless of whatever data mode is currently active - see Station's
# negotiate()/_handle_control_frame(). Has to be fixed and known to both
# sides ahead of time (a receiver can't demodulate a burst without already
# knowing its modulation), and should be as robust as possible so a
# MODE_REQUEST/MODE_GRANT can get through even on a link too weak for any
# negotiated data mode.
#
# NOT r1_4_k9 (the data channel's own most-robust floor preset - see
# openwave_link.py's MODE_SNR_TABLE): measured directly while building
# this - a Control frame's fixed ~120-byte overhead (preamble+header+CRC+
# padding) already costs ~81KB at BPSK+r1_4_k9 after pulse-shaping, over
# MAX_UDP_PAYLOAD_BYTES before any actual payload is even counted, so a
# MODE_GRANT could never be sent at all. r1_2_k9 is the strongest preset
# that measurably still fits (~52KB, ~13KB of headroom) - it's the same
# rate as r1_2_k3 (the OTHER empirical anchor, proven good at 0dB) with
# the best available K for slightly better correction at that rate, NOT
# an equivalent substitute for r1_4_k9's much stronger low-SNR margin (K
# mainly buys free-distance at a fixed rate; the big low-SNR gains come
# from a LOWER rate, which doesn't fit here). Real, honest consequence:
# the control channel's own low-SNR ceiling is likely higher (needs a
# better link) than the data channel's r1_4_k9 floor - worth confirming
# during the live SNR-sweep testing pass (see tasklist.txt).
CONTROL_MODE_ID = 0
CONTROL_FEC_PRESET = 'r1_2_k9'
NEGOTIATE_MAX_ATTEMPTS = 4
# Largest UDP payload a standard IPv4 socket can send in one datagram
# (65535 - 8-byte UDP header - 20-byte IPv4 header). A burst that would
# exceed this can't be sent at all - asyncio's transport swallows the
# resulting OSError internally and silently kills itself, so this has to
# be checked before calling sendto(), not caught after.
MAX_UDP_PAYLOAD_BYTES = 65507


def _human_size(num_bytes, short=False):
    """num_bytes -> auto-scaled 'N.NN KB (N bytes)' label, matching
    pipeline_test.sh's human_size() convention elsewhere in this project.
    short=True drops the '(N bytes)' suffix - for compact summary lines
    where the exact byte count isn't the point (unlike e.g. a [tx error]
    message, which needs it to explain exactly why a burst didn't fit)."""
    if num_bytes < 1024:
        label = f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        label = f"{num_bytes / 1024:.2f} KB"
    else:
        label = f"{num_bytes / (1024 * 1024):.2f} MB"
    return label if short else f"{label} ({num_bytes} bytes)"


# LinkReceiver.counts entries worth ever mentioning in ota_station.py's own
# output, grouped the way _link_stats_lines()/_link_counts_compact() render
# them - zero-value counters are always omitted (except 'ok', the anchor
# metric), so a clean session doesn't drown in "Bad length:0  Truncated:0
# ..." noise. LinkReceiver.stats_line() itself is untouched (openwave_cli.py
# still uses it directly) - this is a separate, more scannable rendering
# just for this file's own logging.
_FRAME_COUNT_LABELS = (('crc_fail', 'crc-fail'), ('bad_length', 'bad-length'),
                        ('truncated', 'truncated'), ('dup', 'dup'), ('lost', 'lost'))
_FRAG_COUNT_LABELS = (('frag_aborted', 'aborted'), ('frag_overflow', 'overflow'),
                       ('frag_discarded', 'discarded'))


def _nonzero_parts(counts, labels):
    return [f"{counts[key]} {label}" for key, label in labels if counts[key]]


def _link_counts_compact(receiver):
    """LinkReceiver.counts -> one short 'N ok, N crc-fail, ...' string,
    zero-value counters omitted - used for the session summary's terse
    link total."""
    c = receiver.counts
    parts = [f"{c['ok']} ok"] + _nonzero_parts(c, _FRAME_COUNT_LABELS)
    if c['acks_rx']:
        parts.append(f"{c['acks_rx']} acks rx")
    parts += _nonzero_parts(c, _FRAG_COUNT_LABELS)
    if c['not_for_us']:
        parts.append(f"{c['not_for_us']} not-for-us")
    return ', '.join(parts)


def _link_stats_lines(receiver, indent='  '):
    """LinkReceiver's counters/last-frame state -> a short, scannable list
    of lines for --verbose per-message [stats] logging - a structured
    alternative to LinkReceiver.stats_line()'s single dense line, with
    zero-value counters omitted."""
    c = receiver.counts
    frame_parts = [f"{c['ok']} ok"] + _nonzero_parts(c, _FRAME_COUNT_LABELS)
    lines = [f"{indent}frames:  {', '.join(frame_parts)}"]
    if c['acks_rx']:
        lines.append(f"{indent}acks:    {c['acks_rx']} received")
    frag_parts = _nonzero_parts(c, _FRAG_COUNT_LABELS)
    if frag_parts:
        lines.append(f"{indent}fragments: {', '.join(frag_parts)}")
    if c['not_for_us']:
        lines.append(f"{indent}filtered: {c['not_for_us']} not for us")
    mode_info = link.WAVEFORM_MODES.get(receiver.last_mode)
    modulation = mode_info['modulation'] if mode_info else 'unknown'
    seq_str = str(receiver.last_seq) if receiver.last_seq is not None else '-'
    lines.append(f"{indent}session: seq {seq_str}, mode {receiver.last_mode} ({modulation}), "
                 f"{receiver.last_src or '-'} -> {receiver.last_dst or '-'}")
    return lines


def _traffic_line(label, num_messages, text_bytes, air_bytes, arrow, extra_parts):
    """One line of the session summary's sent/received traffic totals -
    label padded for alignment, byte counts in short (no raw-byte-suffix)
    human-readable form, extra_parts (fragment count, bps, ...) only shown
    when there's actually traffic to report."""
    if num_messages == 0:
        return f"{label:<9} 0 messages"
    msg_word = 'message' if num_messages == 1 else 'messages'
    line = (f"{label:<9} {num_messages} {msg_word}, "
            f"{_human_size(text_bytes, short=True)} text {arrow} "
            f"{_human_size(air_bytes, short=True)} on-air")
    if extra_parts:
        line += f" ({', '.join(extra_parts)})"
    return line


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
                 fec_preset=None, use_ack=False, auto=False, on_message=None, verbose=False):
        self.station_id = station_id
        self.dst_id = dst_id
        self.use_fec = use_fec
        self.use_ack = use_ack
        self.auto = auto
        self.verbose = verbose
        self._on_message = on_message
        # negotiate() runs lazily, at most once, on this station's own
        # FIRST send - see send_text(). A station that only ever RECEIVES
        # never calls negotiate() itself at all (it stays silent and just
        # responds to a peer's MODE_REQUEST when one arrives - see
        # _handle_control_frame - regardless of this flag).
        self._auto_negotiate_done = False

        self.session_start = time.monotonic()
        self.stats = {
            'sent_messages': 0, 'sent_text_bytes': 0, 'sent_air_bytes': 0, 'sent_fragments': 0,
            'received_messages': 0, 'received_text_bytes': 0, 'received_air_bytes': 0,
        }

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
        self._suppress_next_message = False
        self._pending_message_seq = None
        self._last_delivered_seq = None

        # Fixed, never changes regardless of negotiate() - see
        # CONTROL_MODE_ID/CONTROL_FEC_PRESET's module-level docstring.
        control_info = link.WAVEFORM_MODES[CONTROL_MODE_ID]
        self._control_modulation = control_info['modulation']
        self._control_modulate = MODULATORS[self._control_modulation]
        self._control_demodulate = DEMODULATORS[self._control_modulation]
        self._control_fec_generators, self._control_fec_k = fec.PRESETS[CONTROL_FEC_PRESET]
        self._negotiation_seq = None  # seq of our own outstanding negotiate() request, if any
        self._negotiation_event = None
        self._last_rx_snr_db = None

        self._apply_tx_mode(mode_id, fec_preset)
        self._apply_rx_mode(mode_id, fec_preset)

    def _apply_tx_mode(self, mode_id, fec_preset=None):
        """(Re)configures what THIS station uses to SEND - called once
        from __init__, and again by _handle_control_frame() when a
        negotiate() call's own MODE_GRANT lands (the peer telling us what
        IT can receive). Never touches the control channel (see __init__'s
        control_info block) - that's fixed for the station's whole
        lifetime. See _apply_rx_mode for the separate, independent
        receive-side counterpart - negotiation is asymmetric (each side
        grants the other whatever ITS OWN measurement of the incoming
        probe supports), so what this station sends with and what it
        should expect FROM its peer are not necessarily the same mode."""
        mode_info = link.WAVEFORM_MODES[mode_id]
        self.mode_id = mode_id
        self.modulation = mode_info['modulation']
        self._modulate = MODULATORS[self.modulation]
        # fec_preset defaults to the mode's own WAVEFORM_MODES entry, but can
        # be overridden independently - e.g. to use a stronger code than the
        # mode's documented default without changing WAVEFORM_MODES itself.
        self.fec_preset = fec_preset or mode_info['fec_preset']
        self._fec_generators, self._fec_k = fec.PRESETS[self.fec_preset]

        # Depends only on mode/fec, not per-message - recomputed here (not
        # just at __init__) since a negotiated mode change can move this
        # ceiling a lot. Used as build_fragmented_frames()'s chunk_size in
        # send_text(), so ANY message length auto-splits into bursts that
        # actually fit in one UDP datagram - not just messages over
        # link.MAX_PAYLOAD_LEN (that's the protocol's own fragmentation
        # threshold, unrelated to what a given mode/FEC combo can fit on
        # the wire; at a tight combo like mode 0 + r1_4_k7 it can be far
        # smaller than MAX_PAYLOAD_LEN).
        self.max_fragment_chars = max(1, self.max_single_fragment_chars())

    def _apply_rx_mode(self, mode_id, fec_preset=None):
        """(Re)configures what THIS station expects an incoming DATA burst
        from its peer to be using - called once from __init__ (mirroring
        the starting TX mode, since nothing better is known yet), and
        again by _handle_control_frame() whenever THIS station grants a
        mode to its peer (since the peer will start sending in exactly
        that mode). Deliberately separate state from _apply_tx_mode's -
        see that method's docstring for why they can diverge."""
        mode_info = link.WAVEFORM_MODES[mode_id]
        self._rx_modulation = mode_info['modulation']
        self._rx_demodulate = DEMODULATORS[self._rx_modulation]
        rx_fec_preset = fec_preset or mode_info['fec_preset']
        self._rx_fec_generators, self._rx_fec_k = fec.PRESETS[rx_fec_preset]

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
            self._log("[stats]\n" + "\n".join(_link_stats_lines(self.receiver)))
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

    def _demod_attempts(self):
        """(modulation, demodulate_fn, use_fec, fec_generators, fec_k)
        tuples to try, in order, for an incoming burst: the RX mode first -
        what THIS station itself last granted its peer, i.e. what the peer
        should actually be sending with (the common case once negotiated;
        see _apply_rx_mode - this is deliberately NOT self.modulation,
        this station's own SEND mode, since negotiation is asymmetric and
        the two can differ) - falling back to the fixed CONTROL mode so a
        MODE_REQUEST/MODE_GRANT can always be heard no matter what data
        mode is currently active. Skipped entirely when they're already
        identical (e.g. before any negotiation has happened, mode 0
        usually already IS the control mode)."""
        attempts = [(self._rx_modulation, self._rx_demodulate, self.use_fec,
                     self._rx_fec_generators, self._rx_fec_k)]
        if (self._rx_modulation, self._rx_fec_generators, self._rx_fec_k) != \
                (self._control_modulation, self._control_fec_generators, self._control_fec_k) \
                or not self.use_fec:
            attempts.append((self._control_modulation, self._control_demodulate, True,
                              self._control_fec_generators, self._control_fec_k))
        return attempts

    # -- RX pipeline: samples -> demod -> sync/FEC -> LinkReceiver -----
    def _handle_received_samples(self, payload):
        self.stats['received_air_bytes'] += len(payload)
        samples = np.frombuffer(payload, dtype=np.complex64)
        num_symbols = pulse.expected_symbol_count(len(samples))
        filtered = pulse.matched_filter(samples)
        offset = pulse.group_delay()
        symbols = pulse.decimate(filtered, sps=4, offset=offset)[:num_symbols]

        # Try each candidate mode until one yields an actual 'ok' frame -
        # NOT just "yields anything", since a wrong-mode demod attempt can
        # still produce plausible-looking (but CRC-failing) sync hits, and
        # stopping on those would mean a burst sent in the OTHER mode never
        # gets its real, correct-mode attempt tried at all. If nothing ever
        # produces an 'ok', fall back to the first attempt's raw results so
        # existing crc_fail/bad_length diagnostics still fire exactly once.
        #
        # A wrong-mode attempt can also outright RAISE (e.g. demodulating a
        # burst at the wrong bits/symbol count can leave demodulate_qam16's
        # bit count not a multiple of 8) rather than just failing CRC
        # cleanly - confirmed live while testing this. Each candidate is
        # therefore tried in isolation: an exception there just means THIS
        # candidate is a bad fit (exactly as informative as a clean
        # crc_fail would have been), not a reason to give up on the burst
        # entirely before ever trying the mode that actually matches.
        frames, matched_modulation = [], None
        fallback_frames, fallback_modulation = None, None
        for modulation, demod_fn, use_fec, fec_generators, fec_k in self._demod_attempts():
            try:
                recovered = demod_fn(symbols)
                candidate = (list(fec.decode_frames(recovered, fec_generators, fec_k)) if use_fec
                             else list(sync.iter_frames_correlated(recovered)))
            except Exception:
                candidate = []
            if fallback_frames is None:
                fallback_frames, fallback_modulation = candidate, modulation
            if any(status == 'ok' for status, _, _ in candidate):
                frames, matched_modulation = candidate, modulation
                break
        if not frames:
            frames, matched_modulation = fallback_frames, fallback_modulation

        if not frames:
            self._log("[rx] no frame found in this burst - link too weak to even find sync "
                      "(try a shorter --distance-km or a higher --base-snr-db on the simulator)")
            return

        self._last_rx_snr_db = modem.estimate_snr_db(symbols, matched_modulation)
        if self.verbose:
            # Full explanation of what this number is/isn't (blind estimate,
            # no pilot symbols) lives once in openwave_modem.py's
            # estimate_snr_db() docstring - repeating it on every burst
            # under --verbose was pure noise.
            self._log(f"[rx] SNR ~{self._last_rx_snr_db:.1f}dB ({matched_modulation})")

        for status, header, payload_bytes in frames:
            if status == 'ok':
                if header['packet_type'] == link.PACKET_TYPE_CONTROL:
                    self._handle_control_frame(header, payload_bytes)
                    continue
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

    def _handle_control_frame(self, header, payload):
        """Dispatches a decoded PACKET_TYPE_CONTROL frame - see
        openwave_link.py's CONTROL_OP_MODE_REQUEST/CONTROL_OP_MODE_GRANT
        and this class's negotiate()."""
        try:
            parsed = link.parse_control_payload(payload)
        except ValueError as exc:
            self._log(f"[negotiate] ignoring malformed Control frame: {exc}")
            return

        if parsed['op'] == link.CONTROL_OP_MODE_REQUEST:
            granted_mode_id, fec_preset = link.select_mode_for_snr(self._last_rx_snr_db)
            grant = link.build_mode_grant_frame(
                header['seq'], granted_mode_id, fec_preset, self._last_rx_snr_db,
                mode_id=CONTROL_MODE_ID, src_id=self.station_id.encode(),
                dst_id=self.dst_id.encode(),
            )
            self._transmit_control_frame(grant)
            # The peer will start sending TO US in exactly the mode we just
            # granted them - remember that as our own RX expectation (see
            # _apply_rx_mode/_demod_attempts), separate from self.mode_id
            # (what WE send with, only touched by a GRANT we receive).
            self._apply_rx_mode(granted_mode_id, fec_preset)
            self._log(f"[negotiate] {self.dst_id} probed the link (estimated their SNR ~"
                      f"{self._last_rx_snr_db:.1f}dB) - granted them "
                      f"{link.format_mode(granted_mode_id)}/{fec_preset}")
            return

        if parsed['op'] == link.CONTROL_OP_MODE_GRANT:
            if self._negotiation_event is None or header['seq'] != self._negotiation_seq:
                return  # stale grant (e.g. a retried request's earlier reply) - ignore
            self._apply_tx_mode(parsed['mode_id'], parsed['fec_preset'])
            self._log(f"[negotiate] {self.dst_id} granted {link.format_mode(parsed['mode_id'])}/"
                      f"{parsed['fec_preset']} (their estimate of our link SNR: "
                      f"~{parsed['snr_db']:.1f}dB)")
            self._negotiation_event.set()

    async def negotiate(self):
        """Probe the link at the fixed control mode/FEC and adopt whatever
        mode/FEC the peer grants back for this station's future sends (see
        openwave_link.py's MODE_SNR_TABLE/select_mode_for_snr()). Setup-
        only - call once before the first send_text(), not continuously;
        mid-session re-negotiation isn't implemented (would need to handle
        frames already in flight under the old mode). A non-responding peer
        (e.g. one without negotiation support) is non-fatal: logs a
        warning and leaves the current mode/FEC unchanged."""
        seq = self._seq
        self._seq = (self._seq + 1) % link.SEQ_MODULUS
        self._negotiation_seq = seq
        self._negotiation_event = asyncio.Event()

        request = link.build_mode_request_frame(
            seq, mode_id=CONTROL_MODE_ID, src_id=self.station_id.encode(),
            dst_id=self.dst_id.encode())

        for attempt in range(NEGOTIATE_MAX_ATTEMPTS):
            self._transmit_control_frame(request)
            try:
                await asyncio.wait_for(self._negotiation_event.wait(),
                                        timeout=RETRY_POLL_INTERVAL_S * 10)
                return  # _handle_control_frame already applied the grant and logged it
            except asyncio.TimeoutError:
                if self.verbose:
                    self._log(f"[negotiate] no response yet (attempt {attempt + 1}/"
                              f"{NEGOTIATE_MAX_ATTEMPTS}), retrying")

        self._log(f"[negotiate] {self.dst_id} did not respond after {NEGOTIATE_MAX_ATTEMPTS} "
                  f"attempts - keeping {link.format_mode(self.mode_id)}/{self.fec_preset}")
        self._negotiation_seq = None
        self._negotiation_event = None

    def register(self):
        """Send a zero-length 'hello' datagram to the simulator so it
        learns this station's address right away, even if this station
        never sends anything itself (a receive-only listener would
        otherwise be unreachable - the simulator only learns a peer's
        address from something it has sent)."""
        self.transport.sendto(encode_peer_id(self.station_id), self._sim_addr)

    # -- TX pipeline: frame bytes -> FEC -> modulate -> pulse-shape -----
    def _transmit_frame(self, frame_bytes, modulate_fn=None, use_fec=None,
                         fec_generators=None, fec_k=None, mode_label=None):
        """Defaults to the station's current DATA mode/FEC. Control frames
        (see _transmit_control_frame) pass the fixed control-channel
        modulate/FEC instead, reusing this same size-check/send/stats
        logic rather than duplicating it."""
        modulate_fn = modulate_fn or self._modulate
        use_fec = self.use_fec if use_fec is None else use_fec
        fec_generators = self._fec_generators if fec_generators is None else fec_generators
        fec_k = self._fec_k if fec_k is None else fec_k
        mode_label = mode_label or f"mode {self.mode_id}"

        encoded = fec.encode_frame(frame_bytes, fec_generators, fec_k) if use_fec else frame_bytes
        symbols = modulate_fn(encoded)
        samples = pulse.pulse_shape(symbols)
        out = encode_peer_id(self.station_id) + samples.astype(np.complex64).tobytes()
        if len(out) > MAX_UDP_PAYLOAD_BYTES:
            fec_note = "with FEC" if use_fec else "no FEC"
            self._log(f"[tx error] burst is {_human_size(len(out))}, over UDP's "
                      f"{_human_size(MAX_UDP_PAYLOAD_BYTES)} datagram limit - NOT sent. "
                      f"({fec_note} at {mode_label} is too expensive for this message length; "
                      f"try a higher --mode/fewer bits of FEC redundancy, or a shorter message)")
            return
        self.transport.sendto(out, self._sim_addr)
        self.stats['sent_air_bytes'] += len(out)

    def _transmit_control_frame(self, frame_bytes):
        self._transmit_frame(frame_bytes, modulate_fn=self._control_modulate, use_fec=True,
                              fec_generators=self._control_fec_generators,
                              fec_k=self._control_fec_k,
                              mode_label=f"the control channel ({self._control_modulation})")

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
        the extra channel traffic once one piece has already failed.

        With --auto, THIS station's very first send_text() call triggers
        negotiate() first (before building/transmitting anything else),
        exactly once for the whole session - see self._auto_negotiate_done.
        Deliberately not run any earlier than this (e.g. not eagerly at
        startup): a station with nothing to send yet should stay silent
        and just listen, only transmitting once it actually has a message
        to deliver. This is also why a purely receiving station (one that
        never calls send_text() itself) never negotiates its OWN sending
        mode at all - it only ever responds to what it hears (see
        _handle_control_frame), which needs no negotiate() call of its
        own. Attempted once regardless of outcome - a peer that never
        responds isn't retried on every subsequent send, matching --auto's
        setup-only design."""
        if self.auto and not self._auto_negotiate_done:
            self._auto_negotiate_done = True
            self._log(f"[negotiate] have a message to send - negotiating with {self.dst_id} first")
            await self.negotiate()
            self._log(_mode_banner(self.station_id, self))

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
        self._log(f"[ack] delivered - {len(frames)} fragment(s) acknowledged by {self.dst_id} "
                  f"(mode {self.mode_id}/{self.modulation}, {self.fec_preset}){note}")

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
        sent_extra = ([f"{s['sent_fragments']} fragments"] if s['sent_fragments'] != s['sent_messages'] else []) \
            + [f"{sent_bps:.0f} bps"]
        lines = [
            f"[{self.station_id}] session summary ({elapsed:.1f}s):",
            "  " + _traffic_line('sent', s['sent_messages'], s['sent_text_bytes'], s['sent_air_bytes'],
                                 '->', sent_extra),
            "  " + _traffic_line('received', s['received_messages'], s['received_text_bytes'],
                                 s['received_air_bytes'], '<-', [f"{recv_bps:.0f} bps"]),
            f"  link      {_link_counts_compact(self.receiver)}",
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


def _mode_banner(station_id, station):
    fec_note = f"on ({station.fec_preset})" if station.use_fec else "off"
    rx_note = ""
    if station._rx_modulation != station.modulation:
        # Negotiation is asymmetric - each side grants the other whatever
        # ITS OWN measurement supports, so what we send with and what we
        # expect FROM the peer can legitimately differ (see
        # _apply_tx_mode/_apply_rx_mode).
        rx_note = f" (expecting {station._rx_modulation} from {station.dst_id})"
    return (f"[{station_id}] mode={station.mode_id} ({station.modulation}) fec={fec_note}"
            f"{rx_note} - max ~{station.max_fragment_chars} chars per burst at these settings "
            f"(UDP's {MAX_UDP_PAYLOAD_BYTES}-byte datagram limit); longer messages "
            f"auto-fragment into multiple bursts.")


async def run(args):
    # --auto's whole point is picking the mode/FEC for you from the
    # threshold table (see openwave_link.py's MODE_SNR_TABLE), which is
    # denominated in FEC-preset terms - so it implies FEC is in play even
    # if --fec itself wasn't passed. --fec/--fec-preset still work
    # standalone without --auto, unchanged.
    station = Station(
        station_id=args.station_id, dst_id=args.dst, mode_id=args.mode,
        use_fec=args.fec or args.auto, fec_preset=args.fec_preset, use_ack=args.ack,
        auto=args.auto, verbose=args.verbose,
    )
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: station, local_addr=('0.0.0.0', args.local_port),
    )
    station._sim_addr = (args.sim_host, args.sim_port)
    station.register()
    local_port = transport.get_extra_info('sockname')[1]
    print(f"[{args.station_id}] ack={'on' if args.ack else 'off'} auto={'on' if args.auto else 'off'} "
          f"- local port {local_port}, relaying via {args.sim_host}:{args.sim_port}.", file=sys.stderr)
    print(_mode_banner(args.station_id, station), file=sys.stderr)

    # With --auto, negotiation is deliberately NOT run here - it happens
    # lazily, at most once, on this station's own first send_text() call
    # (see Station.send_text()'s docstring): a station with nothing to
    # send yet just listens, and only responds to a peer's incoming
    # MODE_REQUEST if/when one arrives (_handle_control_frame, unconditional
    # regardless of --auto) - it never probes on its own until it actually
    # has a message to deliver.
    if args.auto:
        print(f"[{args.station_id}] --auto: will negotiate mode/FEC with {args.dst} on first "
              f"send, not now - listening until then.", file=sys.stderr)

    print(f"[{args.station_id}] type a message and press Enter.", file=sys.stderr)

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
                         help='Request ACKs and retry on timeout, per fragment (stop-and-wait: '
                              'each fragment is sent and its own ACK awaited/retried before the '
                              'next - a lost fragment only costs that fragment a retry, not the '
                              'whole message)')
    parser.add_argument('--auto', action='store_true',
                         help='Negotiate mode/FEC with the peer automatically at startup, instead '
                              'of requiring matching --mode/--fec on both ends - probes the link '
                              'at a fixed robust control mode, estimates its SNR, and adopts '
                              'whatever the peer grants back (see openwave_link.py\'s '
                              'MODE_SNR_TABLE). Implies FEC is available for use even without '
                              '--fec. One-time at startup, not continuous re-adaptation.')
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
