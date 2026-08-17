"""
OpenWave link-layer framing: header build/parse and CRC32 packet integrity.
Shared between phy_sim_loopback.grc, tx_pluto.grc, and rx_rtlsdr.grc's epy_blocks
so the flowgraphs can't drift out of sync on the framing format - see
OpenWave_Description.txt for the full spec this implements a subset of.

Minimal (phase-1) scope: Payload Length, CRC32, Sequence Number, the
ack_requested/ack_response Flags bits (build_ack_frame()/RetryManager for
ACK+retry), and the fragmented/more_fragments Flags bits (build_fragmented_frames()/
FragmentReassembler for messages over MAX_PAYLOAD_LEN) are real and enforced.

Note RetryManager is a pure state machine with no RF/GNU Radio knowledge.
phy_sim_loopback.grc wires a real closed loop for it (RX's ack_needed ->
ack_responder_0 -> the same TX/channel -> RX again -> ack_received -> the
original TX), since it has both a TX and RX chain in one process. tx_pluto.grc
and rx_rtlsdr.grc do not and structurally can't: the RTL-SDR RX station has no
transmit capability at all and can never send an ACK back over real air, and
tx_pluto.grc has no RX chain to receive one even if it could. RetryManager and
build_ack_frame() are implemented and tested independent of any of this, so
they're ready for a future full-duplex pairing.

Fragmentation has a real protocol-design gap worth knowing about: the header
has no fragment-index or reassembly-ID field (see OpenWave_Description.txt) -
just Fragmented and More Fragments bits. FragmentReassembler infers "start of
a new run" as "a fragmented frame arrived while no run was already in
progress", using the existing Sequence Number as a consecutive per-fragment
counter. This detects a lost fragment *within* a run (a sequence gap aborts
it), but can't detect a lost *first* fragment of a run, since there's nothing
marking a frame as specifically first rather than mid-run. See
FragmentReassembler's docstring for the full reasoning.

The other header fields (Waveform Mode ID, Source/Destination ID) are written
at their correct fixed offsets so the 32-byte layout matches the full spec,
but are placeholder values on TX and are not parsed/acted on by RX yet.
"""
import struct
import time
import zlib


def _lfsr_pn_bits(num_bits, register_bits=9, taps=(1, 2, 5, 9), seed=1):
    """Fibonacci LFSR bit generator. (register_bits=9, taps=(1,2,5,9)) is a
    verified maximal-length configuration for THIS specific bit convention
    (tap positions 1-indexed from the LSB, feedback shifted into the MSB,
    output taken from the LSB) - empirically confirmed (not just assumed from
    a textbook polynomial table, which uses different conventions) to produce
    an ideal two-valued autocorrelation (peak at lag 0, exactly -1 at every
    other lag) over its full period of 2**9-1=511 bits. num_bits=256 is well
    under that period, so the returned window doesn't wrap/repeat within
    itself.
    """
    state = seed & ((1 << register_bits) - 1)
    assert state != 0, "LFSR seed must be nonzero"
    bits = []
    for _ in range(num_bits):
        bits.append(state & 1)
        feedback = 0
        for t in taps:
            feedback ^= (state >> (t - 1)) & 1
        state = (state >> 1) | (feedback << (register_bits - 1))
    return bits


def _pn_bytes(num_bytes):
    bits = _lfsr_pn_bits(num_bytes * 8)
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return bytes(out)


# 128 symbols (2 bits/symbol) = 256 bits = 32 bytes. A pseudo-noise sequence
# has a sharp, unambiguous autocorrelation peak, unlike a simple repeating
# pattern (e.g. the previous 0x55 x8 preamble): numerically, this sequence's
# worst correlation sidelobe is ~9% of its peak, while 0x55-repeated
# correlates at ~99% of its own peak at every 2-bit-multiple offset - making
# the old preamble nearly useless for pinpointing exact frame start under
# noise, since many offsets look almost equally "correct" to a correlator.
PREAMBLE = _pn_bytes(32)
SYNC_WORD = b'\x1A\xCF\xFC\x1D'

HEADER_LEN = 32
CRC_LEN = 4
MAX_PAYLOAD_LEN = 1024
MAX_FRAGMENTS = 64  # defensive cap on a single reassembly - see FragmentReassembler

PACKET_TYPE_DATA = 0x01
PACKET_TYPE_ACK = 0x02
PACKET_TYPE_BEACON = 0x03
PACKET_TYPE_CONTROL = 0x04

VERSION = 1

SEQ_MODULUS = 0x10000  # Sequence Number is a 16-bit field

FLAG_ACK_REQUESTED = 0x01
FLAG_ACK_RESPONSE = 0x02
FLAG_FRAGMENTED = 0x04
FLAG_MORE_FRAGMENTS = 0x08
FLAG_PRIORITY = 0x10
# bits 5-7 reserved

_FLAG_NAMES = (
    (FLAG_ACK_REQUESTED, 'ack_requested'),
    (FLAG_ACK_RESPONSE, 'ack_response'),
    (FLAG_FRAGMENTED, 'fragmented'),
    (FLAG_MORE_FRAGMENTS, 'more_fragments'),
    (FLAG_PRIORITY, 'priority'),
)


def parse_flags(flags_byte):
    """Flags byte -> {'ack_requested': bool, 'ack_response': bool, 'fragmented': bool,
    'more_fragments': bool, 'priority': bool}."""
    return {name: bool(flags_byte & bit) for bit, name in _FLAG_NAMES}


def format_flags(flags_byte):
    """Flags byte -> short display string, e.g. 'ack_requested,priority' or '-' if none set."""
    names = [name for bit, name in _FLAG_NAMES if flags_byte & bit]
    return ','.join(names) if names else '-'

# version(B) type(B) mode(B) flags(B) seq(H) src(8s) dst(8s) payload_len(H) reserved(8s)
# All multi-byte fields big-endian, matching OpenWave_Description.txt's byte-dump examples.
_HEADER_STRUCT = struct.Struct('>BBBBH8s8sH8s')
assert _HEADER_STRUCT.size == HEADER_LEN


def build_header(payload_len, version=VERSION, packet_type=PACKET_TYPE_DATA,
                  mode_id=0, flags=0, seq=0, src_id=b'', dst_id=b''):
    """Build the 32-byte header. src_id/dst_id are truncated/zero-padded to 8 bytes.
    seq wraps automatically at SEQ_MODULUS (16 bits) - callers don't need to."""
    if not (0 <= payload_len <= MAX_PAYLOAD_LEN):
        raise ValueError(f'payload_len {payload_len} out of range 0..{MAX_PAYLOAD_LEN}')
    return _HEADER_STRUCT.pack(
        version, packet_type, mode_id, flags, seq % SEQ_MODULUS,
        src_id[:8].ljust(8, b'\x00'), dst_id[:8].ljust(8, b'\x00'),
        payload_len, b'\x00' * 8,
    )


def parse_header(header_bytes):
    """Parse a 32-byte header into a dict of fields."""
    if len(header_bytes) != HEADER_LEN:
        raise ValueError(f'header must be exactly {HEADER_LEN} bytes, got {len(header_bytes)}')
    version, packet_type, mode_id, flags, seq, src_id, dst_id, payload_len, _reserved = \
        _HEADER_STRUCT.unpack(header_bytes)
    return {
        'version': version, 'packet_type': packet_type, 'mode_id': mode_id,
        'flags': flags, 'seq': seq, 'src_id': src_id, 'dst_id': dst_id,
        'payload_len': payload_len,
    }


def build_frame(payload, lead_pad=24, end_pad=24, **header_kwargs):
    """Full over-the-air frame: lead-in zeros + preamble + sync word + header
    + payload + CRC32 (over header+payload) + trailing zeros.

    The trailing zeros are flush margin for the matched-filter/timing-recovery
    pipeline's group delay - not a marker to search for (RX now knows the exact
    frame length from the header), just samples so the last real bytes have
    somewhere to flow into before the burst source goes idle.
    """
    header = build_header(len(payload), **header_kwargs)
    crc = zlib.crc32(header + payload) & 0xFFFFFFFF
    packet = header + payload + struct.pack('>I', crc)
    return b'\x00' * lead_pad + PREAMBLE + SYNC_WORD + packet + b'\x00' * end_pad


class FrameParser:
    """Incremental header+payload+CRC32 collector.

    Feed it bytes starting exactly at the header (i.e. right after the sync-word
    correlator's tag). Call reset() on each new sync tag - one logical frame
    attempt per sync event.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.buf = bytearray()
        self._payload_len = None
        self.last_header = None

    def feed(self, byte):
        """Feed one byte. Returns (status, payload):
          ('incomplete', None) - still collecting
          ('bad_length', None) - header's payload_len field is out of range;
              frame abandoned (this instance needs a reset()/new sync tag)
          ('crc_fail', None)   - complete frame, but CRC32 didn't match
          ('ok', bytes)        - complete frame, CRC32 verified, payload returned

        self.last_header (dict, see parse_header) is set as soon as the header
        itself is parsed - available after any non-'incomplete' status. NOTE:
        on 'crc_fail' the header fields (seq, flags, ...) are NOT trustworthy -
        the CRC covers the whole header+payload, so a corrupted header can still
        parse without a struct error while containing garbage values. Only rely
        on last_header when status is 'ok'.
        """
        self.buf.append(byte)

        if self._payload_len is None:
            if len(self.buf) < HEADER_LEN:
                return 'incomplete', None
            header = parse_header(bytes(self.buf[:HEADER_LEN]))
            self.last_header = header
            if not (0 <= header['payload_len'] <= MAX_PAYLOAD_LEN):
                return 'bad_length', None
            self._payload_len = header['payload_len']

        total_len = HEADER_LEN + self._payload_len + CRC_LEN
        if len(self.buf) < total_len:
            return 'incomplete', None

        packet = bytes(self.buf[:total_len])
        header_and_payload = packet[:HEADER_LEN + self._payload_len]
        received_crc, = struct.unpack('>I', packet[HEADER_LEN + self._payload_len:total_len])
        payload = packet[HEADER_LEN:HEADER_LEN + self._payload_len]

        if (zlib.crc32(header_and_payload) & 0xFFFFFFFF) != received_crc:
            return 'crc_fail', None
        return 'ok', payload


def build_ack_frame(acked_seq, lead_pad=24, end_pad=24, **header_kwargs):
    """Build a full over-the-air ACK frame acknowledging acked_seq: Packet Type
    ACK, Flags' ACK Response bit set, empty payload. The Sequence Number field
    carries the seq of the Data frame being acknowledged (not an independent
    ACK sequence of its own), so the original sender can match it directly to
    a pending retry."""
    header_kwargs.setdefault('packet_type', PACKET_TYPE_ACK)
    header_kwargs['flags'] = header_kwargs.get('flags', 0) | FLAG_ACK_RESPONSE
    return build_frame(b'', lead_pad=lead_pad, end_pad=end_pad, seq=acked_seq, **header_kwargs)


def needs_ack(header):
    """True if a received (CRC-verified) header is a Data frame asking to be acknowledged."""
    return header['packet_type'] == PACKET_TYPE_DATA and parse_flags(header['flags'])['ack_requested']


def is_ack(header):
    """True if a received (CRC-verified) header is itself an ACK response."""
    return header['packet_type'] == PACKET_TYPE_ACK and parse_flags(header['flags'])['ack_response']


class RetryManager:
    """Tracks one in-flight ACK-requested frame at a time and decides when to
    retransmit or give up. Pure state machine - no RF/GNU Radio knowledge,
    driven entirely by the caller through note_sent()/note_ack()/poll().
    time_func is injectable (defaults to time.monotonic) so tests don't need
    real wall-clock sleeps.

    max_retries counts retransmissions beyond the original send - e.g.
    max_retries=3 allows up to 4 total transmissions (1 original + 3 retries)
    before giving up.
    """

    def __init__(self, max_retries=3, timeout_s=2.0, time_func=None):
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self._time = time_func or time.monotonic
        self.reset()

    def reset(self):
        self.pending_seq = None
        self.attempts = 0
        self._sent_at = None

    @property
    def is_pending(self):
        return self.pending_seq is not None

    def note_sent(self, seq):
        """Call each time a frame with this seq is (re)transmitted."""
        self.pending_seq = seq
        self.attempts += 1
        self._sent_at = self._time()

    def note_ack(self, acked_seq):
        """Call when an ACK frame arrives. Returns True if it resolves the
        currently-pending frame (matching seq); False for a stale/unrelated ACK
        (nothing pending, or the ACK is for a different seq)."""
        if self.pending_seq is not None and acked_seq == self.pending_seq:
            self.reset()
            return True
        return False

    def poll(self):
        """Call periodically (e.g. on a timer tick). Returns one of:
          'idle'    - nothing pending
          'waiting' - still within timeout_s, keep waiting
          'retry'   - timed out and attempts remain; caller should retransmit
                      and then call note_sent() again with the same seq
          'give_up' - exceeded max_retries; state is reset automatically, the
                      caller should treat this as a failed delivery
        """
        if self.pending_seq is None:
            return 'idle'
        if self._time() - self._sent_at < self.timeout_s:
            return 'waiting'
        if self.attempts > self.max_retries:
            self.reset()
            return 'give_up'
        return 'retry'


def split_payload(payload, chunk_size=MAX_PAYLOAD_LEN):
    """Split payload into a list of chunks of at most chunk_size bytes each.
    b'' splits to a single empty chunk, not zero chunks, so callers always get
    at least one piece to build a frame from."""
    if not payload:
        return [b'']
    return [payload[i:i + chunk_size] for i in range(0, len(payload), chunk_size)]


def build_fragmented_frames(payload, seq_start, chunk_size=MAX_PAYLOAD_LEN,
                             lead_pad=24, end_pad=24, **header_kwargs):
    """Build the over-the-air frame(s) for payload, auto-fragmenting if it's
    longer than chunk_size. Each fragment is a fully independent burst (its
    own preamble/sync word/header/CRC/padding) - there's no continuous
    multi-frame streaming format, so treating fragments as separate bursts
    keeps this consistent with how build_frame() already works.

    Returns (frames, next_seq): frames is a list of complete over-the-air byte
    strings (length 1 if payload fit in a single frame - and in that case, no
    fragmentation flags are set, identical to a plain build_frame() call).
    next_seq is seq_start + however many sequence numbers this call consumed
    (one per fragment), already wrapped mod SEQ_MODULUS, ready to pass as the
    next message's seq_start.
    """
    chunks = split_payload(payload, chunk_size)
    base_flags = header_kwargs.pop('flags', 0)

    if len(chunks) == 1:
        frame = build_frame(chunks[0], lead_pad=lead_pad, end_pad=end_pad,
                             seq=seq_start, flags=base_flags, **header_kwargs)
        return [frame], (seq_start + 1) % SEQ_MODULUS

    frames = []
    for i, chunk in enumerate(chunks):
        flags = base_flags | FLAG_FRAGMENTED
        if i < len(chunks) - 1:
            flags |= FLAG_MORE_FRAGMENTS
        seq = (seq_start + i) % SEQ_MODULUS
        frames.append(build_frame(chunk, lead_pad=lead_pad, end_pad=end_pad,
                                   seq=seq, flags=flags, **header_kwargs))
    return frames, (seq_start + len(chunks)) % SEQ_MODULUS


class FragmentReassembler:
    """Reassembles a run of consecutively-sequenced Fragmented frames back into
    the original payload. Feed it every CRC-verified (header, payload) pair
    where parse_flags(header['flags'])['fragmented'] is True - non-fragmented
    frames are already complete on their own and shouldn't go through this.

    Design gap worth knowing (see this module's docstring): there's no
    dedicated "first fragment" flag or fragment-index/reassembly-ID field in
    the OpenWave header. This class infers "start of a new run" as "a
    fragmented frame arrived while no run was already in progress" and uses
    the Sequence Number as a consecutive per-fragment counter. That correctly
    detects a gap *within* an already-started run (aborts it, starts fresh
    from whatever arrived), but can't tell a genuinely-first fragment apart
    from a mid-run fragment if the real first one never arrived at all -
    there's nothing in the header that says "I am fragment #1 of N."
    """

    def __init__(self, max_fragments=MAX_FRAGMENTS):
        self.max_fragments = max_fragments
        self.abort_count = 0
        self.overflow_count = 0
        self.reset()

    def reset(self):
        self._chunks = []
        self._expected_seq = None

    @property
    def in_progress(self):
        return self._expected_seq is not None

    def feed(self, header, payload):
        """Returns (status, result):
          ('waiting', None)   - buffered, still expecting more fragments
          ('complete', bytes) - final fragment arrived; full reassembled payload
          ('overflow', None)  - exceeded max_fragments without completing;
                                 run abandoned defensively

        A sequence gap that aborts a stale in-progress run doesn't get its own
        return status - the frame that caused it is simply treated as the
        start of a new run (status 'waiting' or 'complete' as appropriate).
        Check abort_count (a running total, not a per-call flag) if you need
        to track how often that happens.
        """
        seq = header['seq']
        more = parse_flags(header['flags'])['more_fragments']

        if self.in_progress and seq != self._expected_seq:
            self.reset()
            self.abort_count += 1

        if not self.in_progress:
            self._chunks = []

        self._chunks.append(payload)
        self._expected_seq = (seq + 1) % SEQ_MODULUS

        if len(self._chunks) > self.max_fragments:
            self.reset()
            self.overflow_count += 1
            return 'overflow', None

        if not more:
            result = b''.join(self._chunks)
            self.reset()
            return 'complete', result

        return 'waiting', None
