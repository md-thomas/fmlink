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
    to demodulate with. --auto negotiates this automatically: the station
    that gets a message ready FIRST measures its own link and tells the
    peer to use that mode too (see negotiate()/openwave_link.py's
    MODE_SNR_TABLE) - lazily, on that station's own first send, not
    eagerly at startup. Session-scoped, not continuous - re-run it anytime
    with the /renegotiate command. /ping and /ping survey give a live look
    at how the link (or every mode) is actually performing right now.
    MODE_SNR_TABLE's 16-QAM threshold is deliberately conservative (its
    own comment has the full story); BPSK/QPSK are grounded in real
    round-trip reliability measurements.
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

try:
    # Importing this is enough to make input() (see _stdin_lines()) do
    # readline-style line editing and up/down-arrow history for free -
    # it's not a function this module calls directly, just a hook Python's
    # input() picks up automatically once it's imported. Not in the
    # stdlib on Windows; degrades to plain input() (no history) there.
    import readline  # noqa: F401
except ImportError:
    pass

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
# /ping and /ping survey - see Station.ping()/ping_survey()/_ping_trials().
PING_TRIAL_COUNT = 5
PING_TIMEOUT_S = 2.0
# --bandwidth narrow's ceiling on what --auto is willing to pick (see
# Station.__init__'s bandwidth param/negotiate()) - this simulator's own
# channel model has no concept of a symbol-rate limit (mode 5's 9600
# sym/s is exactly as "free" here as mode 0's 600), but a real deployment
# might (e.g. a narrowband/audio-bandwidth channel that just can't carry
# a faster symbol rate no matter how good its SNR is). 1200 caps
# selection to modes 0-2 - the highest symbol rate in WAVEFORM_MODES that
# still has a BPSK sibling one step down, i.e. the same "1200 sym/s
# ceiling" a classic packet-radio-style narrowband channel would impose.
NARROWBAND_MAX_SYMBOL_RATE = 1200
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


def _snr_db_to_evm_percent(snr_db):
    """Converts openwave_modem.py's estimate_snr_db() output back to an
    EVM_rms percentage - the conventional RF unit for this exact same
    measurement. estimate_snr_db() computes SNR_dB = 10*log10(1/noise_power)
    with signal energy normalized to 1, so noise_power (mean-square error)
    = 10**(-SNR_dB/10) and EVM_rms (a fraction of the ideal symbol
    magnitude) = sqrt(noise_power) = 10**(-SNR_dB/20); this just expresses
    that as a percentage instead of re-deriving it from symbols - it's a
    pure unit conversion of the same underlying blind estimate, not a
    separate/more accurate measurement (see that function's docstring for
    the estimator's own accuracy caveats, which apply here identically)."""
    return 100 * 10 ** (-snr_db / 20)


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


def _ping_survey_summary(rows):
    """rows: list of (mode_id, results, per-trial-summary-string), one
    per mode tested by ping_survey() -> a final consolidated block
    recapping every mode together - the per-mode rows already printed
    live scroll away in a longer session, so this is the "see everything
    at once, after the fact" view - plus a pick for the best mode that
    was FULLY reliable (every trial succeeded), matching the same "use
    the best you can reliably get, not more than the link needs"
    philosophy as MODE_SNR_TABLE itself. "Best" is ranked by actual
    throughput (link.mode_raw_bps()), NOT by mode_id/row order - mode_id
    is not guaranteed to increase monotonically with throughput (see
    WAVEFORM_MODES' comment - e.g. mode 5 is slower than mode 4)."""
    lines = ["[ping] survey summary:"]
    best_mode_id = None
    for mode_id, results, summary in rows:
        mode_info = link.WAVEFORM_MODES[mode_id]
        label = f"Mode {mode_id} ({mode_info['modulation']}, {mode_info['symbol_rate']} sym/s)"
        lines.append(f"  {label:<32} {summary}")
        if results and all(r['ok'] for r in results):
            if best_mode_id is None or link.mode_raw_bps(mode_id) > link.mode_raw_bps(best_mode_id):
                best_mode_id = mode_id

    if best_mode_id is not None:
        mode_info = link.WAVEFORM_MODES[best_mode_id]
        lines.append(f"  best fully-reliable mode: Mode {best_mode_id} "
                     f"({mode_info['modulation']}, {mode_info['symbol_rate']} sym/s)")
    else:
        lines.append("  no mode was fully reliable this pass (every mode dropped at least "
                     "one ping) - the link may be too weak or too fading-prone right now")
    return "\n".join(lines)


def _waveform_modes_table():
    lines = ["Waveform Modes (openwave_link.py's WAVEFORM_MODES):"]
    for mode_id, info in sorted(link.WAVEFORM_MODES.items()):
        lines.append(f"  {mode_id}: {info['modulation']:<5} {info['symbol_rate']:>5} sym/s  "
                     f"(mode's own default fec_preset: {info['fec_preset']})")
    lines.append("")
    lines.append(f"FEC presets (openwave_fec.py's PRESETS): {', '.join(sorted(fec.PRESETS))}")
    return "\n".join(lines)


def _help_text():
    """The full interactive-command reference for /help - see
    _handle_command() for what each one actually does. Kept separate from
    the one-line startup hint (which just names the commands) since this
    is meant to be read on demand, not on every launch."""
    return "\n".join([
        "Commands (anything else is sent as a chat message):",
        "  /ping                       quick round-trip timing/quality check at the current mode",
        "  /ping survey                really tests every mode this station can use (see /bandwidth)",
        "  /renegotiate                re-run the mode/FEC handshake with the peer now",
        "  /mode [N]                   show the current mode, or switch to Waveform Mode N (tells the peer)",
        "  /modes                      list the Waveform Modes table",
        "  /fec [PRESET]               show the current FEC preset, or switch it (keeps the mode, tells the peer)",
        "  /bandwidth [narrow|wide]    show or set the ceiling on automatic mode selection",
        "  /help (or /?)               show this list",
        "  /quit (or /exit)            close this station",
    ])


class Station(asyncio.DatagramProtocol):
    """One live OpenWave station. Sends text via send_text(); received,
    decoded messages arrive via the on_message callback. Talks to a peer
    only indirectly, through an ota_sim simulator's UDP relay - never
    directly to the peer's address."""

    def __init__(self, station_id, dst_id, mode_id=0, use_fec=False,
                 fec_preset=None, use_ack=False, auto=False, bandwidth='wide',
                 on_message=None, verbose=False):
        self.station_id = station_id
        self.dst_id = dst_id
        self.use_fec = use_fec
        self.use_ack = use_ack
        self.auto = auto
        # Ceiling on what --auto is willing to pick (see
        # NARROWBAND_MAX_SYMBOL_RATE/select_mode_for_snr's max_symbol_rate
        # param) - None (wide, the default) considers every mode, matching
        # behavior before this parameter existed. Manual --mode/--fec and
        # /ping survey are NOT constrained by this - it only limits what
        # negotiate() will automatically choose.
        self.max_symbol_rate = NARROWBAND_MAX_SYMBOL_RATE if bandwidth == 'narrow' else None
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
        self._mode_set_seq = None  # seq of our own outstanding MODE_SET, if any
        self._mode_set_event = None
        self._ping_seq = None  # seq of our own outstanding /ping, if any
        self._ping_event = None
        # Set by _handle_received_samples() whenever a burst arrives (or
        # fails to even sync) WHILE a ping is outstanding - lets
        # _ping_trials() tell "nothing came back at all" apart from "a
        # reply came back but was corrupted" on a timeout, instead of
        # reporting both identically as "timed out". Reset at the start
        # of each trial.
        self._ping_last_rx_status = None
        self._negotiating = False  # reentrancy guard - see negotiate()
        self._last_rx_snr_db = None

        self._apply_mode(mode_id, fec_preset)

    def _apply_mode(self, mode_id, fec_preset=None):
        """(Re)configures this station's mode/FEC - used for BOTH sending
        and receiving (see _demod_attempts()). Called once from __init__,
        and again by _handle_control_frame() once negotiate() (initiator
        side) or an incoming MODE_SET (responder side) establishes the
        session's one mode - see negotiate()'s docstring for the full
        two-phase handshake. Never touches the control channel (see
        __init__'s control_info block) - that's fixed for the station's
        whole lifetime."""
        mode_info = link.WAVEFORM_MODES[mode_id]
        self.mode_id = mode_id
        self.modulation = mode_info['modulation']
        self._modulate = MODULATORS[self.modulation]
        self._demodulate = DEMODULATORS[self.modulation]
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
        tuples to try, in order, for an incoming burst: this station's
        current mode first (the common case once negotiated - both
        directions always share one mode, see negotiate()), falling back
        to the fixed CONTROL mode so a MODE_REQUEST/GRANT/SET/SET_ACK can
        always be heard no matter what data mode is currently active.
        Skipped entirely when they're already identical (e.g. before any
        negotiation has happened, mode 0 usually already IS the control
        mode)."""
        attempts = [(self.modulation, self._demodulate, self.use_fec,
                     self._fec_generators, self._fec_k)]
        if (self.modulation, self._fec_generators, self._fec_k) != \
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
            if self._ping_event is not None:
                self._ping_last_rx_status = 'no_sync'
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
                if self._ping_event is not None:
                    self._ping_last_rx_status = status

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
        openwave_link.py's CONTROL_OP_* constants and this class's
        negotiate() for the full two-phase handshake (probe so the
        initiator has something to measure, then an authoritative
        MODE_SET both stations end up sharing)."""
        try:
            parsed = link.parse_control_payload(payload)
        except ValueError as exc:
            self._log(f"[negotiate] ignoring malformed Control frame: {exc}")
            return

        if parsed['op'] == link.CONTROL_OP_MODE_REQUEST:
            # Phase 1 reply: purely something for the requester to measure
            # (self._last_rx_snr_db was already updated off THIS frame by
            # _handle_received_samples before this dispatch). Still
            # computes/logs an opinion for visibility, but does NOT apply
            # anything here - only an eventual MODE_SET is authoritative.
            # Respects OUR OWN --bandwidth ceiling even though this reply
            # is non-binding, so the logged opinion doesn't suggest a mode
            # this station wouldn't actually be willing to use itself.
            proposed_mode_id, proposed_fec_preset = link.select_mode_for_snr(
                self._last_rx_snr_db, max_symbol_rate=self.max_symbol_rate)
            grant = link.build_mode_grant_frame(
                header['seq'], proposed_mode_id, proposed_fec_preset, self._last_rx_snr_db,
                mode_id=CONTROL_MODE_ID, src_id=self.station_id.encode(),
                dst_id=self.dst_id.encode(),
            )
            self._transmit_control_frame(grant)
            self._log(f"[negotiate] {self.dst_id} probed the link (estimated their SNR ~"
                      f"{self._last_rx_snr_db:.1f}dB) - replied so they can measure us "
                      f"(our own, non-authoritative opinion: "
                      f"{link.format_mode(proposed_mode_id)}/{proposed_fec_preset})")
            return

        if parsed['op'] == link.CONTROL_OP_MODE_GRANT:
            if self._negotiation_event is None or header['seq'] != self._negotiation_seq:
                return  # stale/unmatched reply (e.g. a retried request's earlier reply) - ignore
            # self._last_rx_snr_db is OUR OWN measurement of this frame -
            # negotiate() decides from that, not from parsed['mode_id']/
            # fec_preset (the peer's own opinion, logged only).
            self._log(f"[negotiate] measured {self.dst_id}'s reply at ~"
                      f"{self._last_rx_snr_db:.1f}dB (their own, non-authoritative opinion: "
                      f"{link.format_mode(parsed['mode_id'])}/{parsed['fec_preset']})")
            self._negotiation_event.set()
            return

        if parsed['op'] == link.CONTROL_OP_MODE_SET:
            # The peer initiated and measured its own link to us - adopt
            # its decision for BOTH directions, and mark ourselves as
            # already negotiated so our own first send later doesn't
            # redundantly re-negotiate and second-guess an already-
            # established session mode.
            self._apply_mode(parsed['mode_id'], parsed['fec_preset'])
            self._auto_negotiate_done = True
            ack = link.build_mode_set_ack_frame(
                header['seq'], mode_id=CONTROL_MODE_ID, src_id=self.station_id.encode(),
                dst_id=self.dst_id.encode())
            self._transmit_control_frame(ack)
            self._log(f"[negotiate] {self.dst_id} set the link to "
                      f"{link.format_mode(parsed['mode_id'])}/{parsed['fec_preset']} - adopted")
            return

        if parsed['op'] == link.CONTROL_OP_MODE_SET_ACK:
            if self._mode_set_event is None or header['seq'] != self._mode_set_seq:
                return  # stale/unmatched ack - ignore
            self._mode_set_event.set()
            return

        if parsed['op'] == link.CONTROL_OP_PING:
            # Reply at THIS station's current active mode (not the fixed
            # control mode) - /ping and /ping survey exist specifically to
            # measure real round-trip behavior at whatever mode is in use,
            # see _ping_trials()/_transmit_frame() (default = current mode).
            pong = link.build_pong_frame(
                header['seq'], mode_id=self.mode_id, src_id=self.station_id.encode(),
                dst_id=self.dst_id.encode())
            self._transmit_frame(pong)
            return

        if parsed['op'] == link.CONTROL_OP_PONG:
            if self._ping_event is None or header['seq'] != self._ping_seq:
                return  # stale/unmatched pong - ignore
            self._ping_event.set()

    async def negotiate(self):
        """Two-phase handshake, run by whichever station has a message
        ready first (see send_text()) - or explicitly via /renegotiate:
        (1) probe the peer, since a station can only ever measure signals
        arriving AT it, never what it sends - this station has nothing to
        measure until the peer replies; (2) having now measured its OWN
        reception of that reply, pick a mode/FEC (openwave_link.py's
        MODE_SNR_TABLE/select_mode_for_snr()) and authoritatively tell
        the peer to use it too, via _announce_mode() - both stations end
        up on the SAME mode for BOTH directions, not two independently-
        chosen ones (see openwave_link.py's CONTROL_OP_* module comment
        for the full rationale). Session-scoped: call once before the
        first send_text() (already done automatically - see send_text()),
        or again at any time via /renegotiate; not continuous. Known gap:
        if both stations happen to get their own first message to send
        within the same handshake window, both may start this as their
        own initiator concurrently - not handled (no tie-breaking). A
        non-responding peer at either phase is non-fatal: logs a warning
        and leaves (or proceeds with) the current mode/FEC. Guarded
        against overlapping calls (self._negotiating) - a second call
        while one's already in flight is a no-op, not a second concurrent
        handshake stomping on the first's state."""
        if self._negotiating:
            self._log("[negotiate] already negotiating - ignoring overlapping call")
            return
        self._negotiating = True
        try:
            # -- Phase 1: probe, so we have something of the peer's to measure --
            seq = self._seq
            self._seq = (self._seq + 1) % link.SEQ_MODULUS
            self._negotiation_seq = seq
            self._negotiation_event = asyncio.Event()

            request = link.build_mode_request_frame(
                seq, mode_id=CONTROL_MODE_ID, src_id=self.station_id.encode(),
                dst_id=self.dst_id.encode())

            probed = False
            for attempt in range(NEGOTIATE_MAX_ATTEMPTS):
                self._transmit_control_frame(request)
                try:
                    await asyncio.wait_for(self._negotiation_event.wait(),
                                            timeout=RETRY_POLL_INTERVAL_S * 10)
                    probed = True
                    break
                except asyncio.TimeoutError:
                    if self.verbose:
                        self._log(f"[negotiate] no response yet (attempt {attempt + 1}/"
                                  f"{NEGOTIATE_MAX_ATTEMPTS}), retrying")

            self._negotiation_seq = None
            self._negotiation_event = None

            if not probed:
                self._log(f"[negotiate] {self.dst_id} did not respond after "
                          f"{NEGOTIATE_MAX_ATTEMPTS} attempts - keeping "
                          f"{link.format_mode(self.mode_id)}/{self.fec_preset}")
                return

            # -- Decide from OUR OWN measurement, then tell the peer (phase 2) --
            mode_id, fec_preset = link.select_mode_for_snr(
                self._last_rx_snr_db, max_symbol_rate=self.max_symbol_rate)
            self._log(f"[negotiate] measured our own link at ~{self._last_rx_snr_db:.1f}dB - "
                      f"setting both directions to {link.format_mode(mode_id)}/{fec_preset}")
            await self._announce_mode(mode_id, fec_preset)
        finally:
            self._negotiating = False

    async def _announce_mode(self, mode_id, fec_preset=None):
        """Applies mode_id/fec_preset to THIS station immediately, then
        authoritatively tells the peer to do the same (MODE_SET, retried
        until MODE_SET_ACK confirms) - negotiate()'s phase 2, factored out
        so it's reusable anywhere a mode/FEC needs to be EXPLICITLY chosen
        (not one picked from select_mode_for_snr()) and communicated to
        the peer: ping_survey() (switching through every mode to test it
        for real) and the /mode, /fec commands (a manual override) both
        go through this. A lost/unconfirmed MODE_SET could otherwise
        leave the two stations on different modes, which earlier live-
        testing showed goes badly.
        Returns True if the peer confirmed, False if it never did (the
        local application happens either way - proceeding anyway is the
        right call: a peer that never confirms is either not listening
        or doesn't support this, and there's nothing better to fall back
        to)."""
        self._apply_mode(mode_id, fec_preset)

        seq = self._seq
        self._seq = (self._seq + 1) % link.SEQ_MODULUS
        self._mode_set_seq = seq
        self._mode_set_event = asyncio.Event()

        set_frame = link.build_mode_set_frame(
            seq, mode_id, self.fec_preset, mode_id=CONTROL_MODE_ID,
            src_id=self.station_id.encode(), dst_id=self.dst_id.encode())

        confirmed = False
        for attempt in range(NEGOTIATE_MAX_ATTEMPTS):
            self._transmit_control_frame(set_frame)
            try:
                await asyncio.wait_for(self._mode_set_event.wait(),
                                        timeout=RETRY_POLL_INTERVAL_S * 10)
                confirmed = True
                break
            except asyncio.TimeoutError:
                if self.verbose:
                    self._log(f"[negotiate] {self.dst_id} hasn't confirmed "
                              f"{link.format_mode(mode_id)}/{self.fec_preset} yet "
                              f"(attempt {attempt + 1}/{NEGOTIATE_MAX_ATTEMPTS}), retrying")

        self._mode_set_seq = None
        self._mode_set_event = None

        if confirmed:
            self._log(f"[negotiate] {self.dst_id} confirmed - both directions now "
                      f"{link.format_mode(mode_id)}/{self.fec_preset}")
        else:
            self._log(f"[negotiate] {self.dst_id} never confirmed "
                      f"{link.format_mode(mode_id)}/{self.fec_preset} after "
                      f"{NEGOTIATE_MAX_ATTEMPTS} attempts - proceeding anyway on our end")
        return confirmed

    async def _ping_trials(self, count=PING_TRIAL_COUNT):
        """Runs `count` ping/pong round trips at whatever mode is
        CURRENTLY active (no mode-switching here - see ping()/
        ping_survey() for that). Each trial is a single attempt with its
        own short timeout - a lost ping is recorded as a failure for
        that trial, not retried within it, matching a real `ping` tool's
        semantics rather than the ACK/negotiate retry-until-success
        pattern used elsewhere in this file.

        Returns (results, summary_line); results is a list of one dict
        per trial: {'ok': bool, 'rtt_s': float|None, 'snr_db': float|None,
        'evm_percent': float|None, 'burst_bytes': int, 'failure_reason':
        str|None}. burst_bytes is always set (the ping itself is sent
        regardless of outcome, and is the same size every trial - mode/
        FEC/payload don't change within one call); the rest are only set
        on success (or, for failure_reason, only on failure).
        failure_reason distinguishes a genuine timeout - nothing arrived
        at all ('no_reply') - from a reply that DID arrive but couldn't
        be used ('no_sync', or a decode status like 'crc_fail'/
        'bad_length'/'truncated') - see _handle_received_samples()'s
        _ping_last_rx_status hook. This is an approximation: it
        attributes ANY RX failure seen while this ping is outstanding to
        the ping itself, which is usually right (nothing else is
        normally in flight during a ping trial) but isn't a guarantee in
        a busier session."""
        results = []
        for i in range(count):
            seq = self._seq
            self._seq = (self._seq + 1) % link.SEQ_MODULUS
            self._ping_seq = seq
            self._ping_event = asyncio.Event()
            self._ping_last_rx_status = None

            ping_frame = link.build_ping_frame(
                seq, mode_id=self.mode_id, src_id=self.station_id.encode(),
                dst_id=self.dst_id.encode())
            air_bytes_before = self.stats['sent_air_bytes']
            started_at = time.monotonic()
            self._transmit_frame(ping_frame)
            burst_bytes = self.stats['sent_air_bytes'] - air_bytes_before
            try:
                await asyncio.wait_for(self._ping_event.wait(), timeout=PING_TIMEOUT_S)
                rtt_s = time.monotonic() - started_at
                snr_db = self._last_rx_snr_db
                evm_percent = _snr_db_to_evm_percent(snr_db)
                results.append({'ok': True, 'rtt_s': rtt_s, 'snr_db': snr_db,
                                 'evm_percent': evm_percent, 'burst_bytes': burst_bytes,
                                 'failure_reason': None})
                if self.verbose:
                    self._log(f"[ping] {i + 1}/{count}: {rtt_s * 1000:.0f}ms, "
                              f"SNR ~{snr_db:.1f}dB (EVM ~{evm_percent:.1f}%)")
            except asyncio.TimeoutError:
                reason = self._ping_last_rx_status or 'no_reply'
                results.append({'ok': False, 'rtt_s': None, 'snr_db': None,
                                 'evm_percent': None, 'burst_bytes': burst_bytes,
                                 'failure_reason': reason})
                if self.verbose:
                    self._log(f"[ping] {i + 1}/{count}: failed ({reason})")
            finally:
                self._ping_seq = None
                self._ping_event = None
                self._ping_last_rx_status = None

        burst_bytes = results[0]['burst_bytes'] if results else 0
        successes = [r for r in results if r['ok']]
        if successes:
            rtts_ms = [r['rtt_s'] * 1000 for r in successes]
            avg_rtt_ms = sum(rtts_ms) / len(rtts_ms)
            avg_snr = sum(r['snr_db'] for r in successes) / len(successes)
            avg_evm = sum(r['evm_percent'] for r in successes) / len(successes)
            rtt_range = f"{min(rtts_ms):.0f}-{max(rtts_ms):.0f}ms" if len(rtts_ms) > 1 \
                else f"{rtts_ms[0]:.0f}ms"
            summary = (f"{len(successes)}/{count} ok, RTT {rtt_range} (avg {avg_rtt_ms:.0f}ms), "
                       f"SNR ~{avg_snr:.1f}dB (EVM ~{avg_evm:.1f}%), {burst_bytes}B/burst")
        else:
            summary = f"0/{count} ok, {burst_bytes}B/burst"

        failures = [r for r in results if not r['ok']]
        if failures:
            reason_counts = {}
            for r in failures:
                reason_counts[r['failure_reason']] = reason_counts.get(r['failure_reason'], 0) + 1
            reasons = ', '.join(f"{n} {reason}" for reason, n in sorted(reason_counts.items()))
            summary += f", {len(failures)} failed ({reasons})"

        return results, summary

    async def ping(self):
        """/ping: a quick round-trip timing/quality check at the
        CURRENTLY active mode - see _ping_trials(). To really test every
        mode instead of just this one, see ping_survey()."""
        self._log(f"[ping] testing {link.format_mode(self.mode_id)}/{self.fec_preset} "
                  f"against {self.dst_id}...")
        _, summary = await self._ping_trials()
        self._log(f"[ping] {summary}")

    async def ping_survey(self):
        """/ping survey: really tests every waveform mode this station is
        willing to use against the peer, one at a time - not predicted
        from a single measurement (see MODE_SNR_TABLE's own comment for
        why that's unreliable under fading). Respects --bandwidth: narrow
        only tests modes at or below NARROWBAND_MAX_SYMBOL_RATE (there's
        no point surveying a mode this station won't ever actually pick
        or be willing to use) - unlike --auto's ceiling, which only
        constrains automatic selection, the survey's whole point is
        showing real performance for modes actually in play, so it
        follows the same ceiling rather than testing modes off-limits
        under the current deployment. Each mode is actually switched to
        on BOTH stations first (_announce_mode()'s normal confirmed
        MODE_SET/MODE_SET_ACK exchange - the same mechanism negotiate()
        uses, just with an explicitly chosen mode instead of one picked
        from the SNR table) before testing it for real, so this needs no
        new "guess what mode an unexpected burst might be" machinery on
        the receive side. Restores the original mode afterward and logs a
        final consolidated summary of every mode tested together (see
        _ping_survey_summary()) - the live per-mode rows printed during
        the survey scroll away in a longer session, so this is the
        look-back-at-everything-at-once view. Takes several seconds (a
        confirmed mode switch plus PING_TRIAL_COUNT round trips, per
        mode). Known limitation: a real chat message sent by either
        operator during an active survey would be misinterpreted, since
        the active mode is being changed underneath it - not guarded
        against."""
        original_mode_id, original_fec_preset = self.mode_id, self.fec_preset
        mode_ids = sorted(link.WAVEFORM_MODES)
        if self.max_symbol_rate is not None:
            mode_ids = [m for m in mode_ids
                        if link.WAVEFORM_MODES[m]['symbol_rate'] <= self.max_symbol_rate]
        scope = f"modes at or below {self.max_symbol_rate} sym/s" \
            if self.max_symbol_rate is not None else "all modes"
        self._log(f"[ping] surveying {scope} against {self.dst_id} "
                  f"({PING_TRIAL_COUNT} trials each)...")
        rows = []
        for mode_id in mode_ids:
            await self._announce_mode(mode_id)
            results, summary = await self._ping_trials()
            self._log(f"[ping]   {link.format_mode(mode_id):<28} {summary}")
            rows.append((mode_id, results, summary))

        await self._announce_mode(original_mode_id, original_fec_preset)
        self._log(f"[ping] survey complete - back to {link.format_mode(self.mode_id)}/"
                  f"{self.fec_preset}")
        self._log(_ping_survey_summary(rows))

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
    """Yield lines typed on stdin without blocking the event loop.

    Uses input(), not sys.stdin.readline() - the two look interchangeable
    but aren't: readline (the module, imported at the top of this file)
    only hooks into input(), giving up/down-arrow command history and
    basic line editing for free. sys.stdin.readline() is a plain buffered
    read with none of that, no matter what's imported. input() raises
    EOFError at end-of-input instead of returning '' - caught below to
    end the generator the same way the old readline()-based version did."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, input)
        except EOFError:
            return
        line = line.rstrip('\n')
        if line:
            yield line


async def _handle_command(station, line):
    """Dispatches a '/'-prefixed stdin line to a diagnostic/control action
    instead of sending it as a chat message - see run()'s stdin loop.

    /ping and /ping survey (or --survey) map to Station.ping()/
    ping_survey(). /renegotiate to Station.negotiate() (works regardless
    of --auto, and regardless of whether this station has already
    negotiated once). /modes lists the Waveform Modes table. /mode [N]
    with no argument reports the current mode/FEC; with one, switches to
    Waveform Mode N - like /fec PRESET (switches just the FEC preset,
    keeping the current mode), both go through _announce_mode(), the same
    confirmed MODE_SET/MODE_SET_ACK exchange negotiate()/ping_survey()
    already use, so the peer is actually told and confirms before this
    station considers the switch done - not just a local setting change.
    Either one also marks the session as already-negotiated
    (_auto_negotiate_done), so a manual choice made before this station's
    own first send isn't immediately overridden by --auto's lazy trigger.
    /mode also enforces the current --bandwidth ceiling (max_symbol_rate)
    - a narrow ceiling is a hard limit on what this deployment can
    actually carry, not just a hint for automatic selection, so a manual
    /mode request above it is rejected rather than silently honored.
    /bandwidth [narrow|wide] reports or changes the --bandwidth ceiling
    (Station.max_symbol_rate) live - unlike /mode and /fec, it's a
    constraint on FUTURE automatic selection, not a mode choice itself,
    so it doesn't touch the current mode or trigger a re-negotiation on
    its own (follow up with /renegotiate to actually apply it). /help (or
    /?) prints the full command reference (_help_text()) - the one-line
    startup hint just points here rather than listing every command.

    Unrecognized commands, an out-of-range mode, an unknown FEC preset, or
    an invalid /bandwidth value just log an error - not fatal.

    Returns True to keep the session running, False if the command was
    /quit or /exit and run()'s stdin loop should stop."""
    parts = line[1:].split()
    cmd = parts[0].lower() if parts else ''

    if cmd == 'ping':
        survey = len(parts) > 1 and parts[1].lstrip('-').lower() == 'survey'
        await (station.ping_survey() if survey else station.ping())

    elif cmd == 'renegotiate':
        await station.negotiate()

    elif cmd == 'modes':
        station._log(_waveform_modes_table())

    elif cmd == 'mode':
        if len(parts) < 2:
            station._log(f"[cmd] current: {link.format_mode(station.mode_id)}/"
                         f"{station.fec_preset} - usage: /mode N (see /modes for the list)")
            return True
        try:
            mode_id = int(parts[1])
            if mode_id not in link.WAVEFORM_MODES:
                raise ValueError
        except ValueError:
            station._log(f"[cmd] /mode: {parts[1]!r} is not a valid Waveform Mode ID - see /modes")
            return True
        # A narrow --bandwidth/--bandwidth ceiling is a hard limit on what
        # this deployment can actually carry, not just a hint for
        # automatic selection to prefer - /mode must respect it too, the
        # same as negotiate()/ping_survey() already do, or "narrow" would
        # be trivially bypassable by hand.
        symbol_rate = link.WAVEFORM_MODES[mode_id]['symbol_rate']
        if station.max_symbol_rate is not None and symbol_rate > station.max_symbol_rate:
            station._log(f"[cmd] /mode: {link.format_mode(mode_id)} needs {symbol_rate} sym/s, "
                         f"over the current bandwidth=narrow ceiling of "
                         f"{station.max_symbol_rate} sym/s - use /bandwidth wide first if you "
                         f"really want this mode")
            return True
        await station._announce_mode(mode_id)
        station._auto_negotiate_done = True

    elif cmd == 'fec':
        if len(parts) < 2:
            station._log(f"[cmd] current FEC preset: {station.fec_preset} "
                         f"(use={'on' if station.use_fec else 'off'}) - usage: /fec PRESET "
                         f"(one of {', '.join(sorted(fec.PRESETS))})")
            return True
        preset = parts[1]
        if preset not in fec.PRESETS:
            station._log(f"[cmd] /fec: {preset!r} is not a known FEC preset - "
                         f"one of {', '.join(sorted(fec.PRESETS))}")
            return True
        # Explicitly requesting a preset unambiguously means "use FEC,
        # with this preset" - without this, /fec on a station started
        # without --fec/--auto would silently do nothing (fec_preset gets
        # set, but self.use_fec stays off, so nothing on the wire
        # actually changes). Set before _announce_mode() so
        # max_fragment_chars is recomputed with FEC's overhead accounted
        # for (see _apply_mode()/max_single_fragment_chars()).
        station.use_fec = True
        await station._announce_mode(station.mode_id, preset)
        station._auto_negotiate_done = True

    elif cmd == 'bandwidth':
        if len(parts) < 2:
            current = 'narrow' if station.max_symbol_rate is not None else 'wide'
            station._log(f"[cmd] current bandwidth: {current} - usage: /bandwidth {{narrow,wide}}")
            return True
        value = parts[1].lower()
        if value not in ('narrow', 'wide'):
            station._log(f"[cmd] /bandwidth: {parts[1]!r} must be 'narrow' or 'wide'")
            return True
        # Unlike /mode and /fec, this is a CEILING on future automatic
        # selection (--auto/negotiate()/ping_survey()), not a mode choice
        # itself - see Station.__init__'s max_symbol_rate. Doesn't touch
        # the currently active mode or re-negotiate on its own; the
        # ceiling only takes effect the next time something picks a mode
        # automatically.
        station.max_symbol_rate = NARROWBAND_MAX_SYMBOL_RATE if value == 'narrow' else None
        station._log(f"[cmd] bandwidth set to {value} - doesn't change the current mode by "
                     f"itself; use /renegotiate (or /mode) to actually apply it")

    elif cmd in ('help', '?'):
        station._log(_help_text())

    elif cmd in ('quit', 'exit'):
        station._log("[cmd] closing...")
        return False

    else:
        station._log(f"[cmd] unrecognized command: {line!r} - try /help")

    return True


def _mode_banner(station_id, station):
    fec_note = f"on ({station.fec_preset})" if station.use_fec else "off"
    return (f"[{station_id}] mode={station.mode_id} ({station.modulation}) fec={fec_note} "
            f"- max ~{station.max_fragment_chars} chars per burst at these settings "
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
        auto=args.auto, bandwidth=args.bandwidth, verbose=args.verbose,
    )
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: station, local_addr=('0.0.0.0', args.local_port),
    )
    station._sim_addr = (args.sim_host, args.sim_port)
    station.register()
    local_port = transport.get_extra_info('sockname')[1]
    bandwidth_note = f" bandwidth={args.bandwidth}" if args.auto else ""
    print(f"[{args.station_id}] ack={'on' if args.ack else 'off'} auto={'on' if args.auto else 'off'}"
          f"{bandwidth_note} - local port {local_port}, relaying via "
          f"{args.sim_host}:{args.sim_port}.", file=sys.stderr)
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

    print(f"[{args.station_id}] type a message and press Enter, or a command "
          f"(/help for the list).", file=sys.stderr)

    try:
        async for line in _stdin_lines():
            if line.startswith('/'):
                if not await _handle_command(station, line):
                    break
            else:
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
                         help='Negotiate mode/FEC with the peer automatically instead of requiring '
                              'matching --mode/--fec on both ends - whichever station gets a '
                              'message ready first probes the peer, measures its OWN reception of '
                              'the reply, and picks the best mode/FEC both stations then use (see '
                              'openwave_link.py\'s MODE_SNR_TABLE). Implies FEC is available even '
                              'without --fec. Runs lazily on this station\'s own first send, not '
                              'eagerly at startup; session-scoped, not continuous - re-run anytime '
                              'with /renegotiate.')
    parser.add_argument('--bandwidth', choices=('narrow', 'wide'), default='wide',
                         help='Ceiling on what --auto is willing to pick (default: wide, no '
                              'ceiling - every mode in WAVEFORM_MODES is eligible). narrow caps '
                              f'selection to modes at or below {NARROWBAND_MAX_SYMBOL_RATE} '
                              'sym/s, for a link that physically cannot carry a faster symbol '
                              'rate no matter how good its SNR is (this simulator\'s own channel '
                              'model has no such limit on its own). Does not affect manual '
                              '--mode/--fec or /ping survey, only automatic selection.')
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
