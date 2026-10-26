"""
FMlink link-layer framing: header build/parse and CRC32 packet integrity.
Shared between phy_sim_loopback.grc, tx_pluto.grc, and rx_rtlsdr.grc's epy_blocks
so the flowgraphs can't drift out of sync on the framing format - see
FMlink_Specification.txt for the full spec this implements a subset of.

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
has no fragment-index or reassembly-ID field (see FMlink_Specification.txt) -
just Fragmented and More Fragments bits. FragmentReassembler infers "start of
a new run" as "a fragmented frame arrived while no run was already in
progress", using the existing Sequence Number as a consecutive per-fragment
counter. This detects a lost fragment *within* a run (a sequence gap aborts
it), but can't detect a lost *first* fragment of a run, since there's nothing
marking a frame as specifically first rather than mid-run. See
FragmentReassembler's docstring for the full reasoning.

The other header fields (Waveform Mode ID, Source/Destination ID) are written
at their correct fixed offsets so the 32-byte layout matches the full spec,
but are placeholder (zero/empty) values on every TX chain so far. rx_rtlsdr.grc
is the one RX chain that parses and acts on them now (format_mode() for
display, is_for_station() for Destination ID address filtering) -
phy_sim_loopback.grc's loopback RX does not.
"""
import math
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
# The audio PHY sends only the first 16 bytes of the same PN preamble:
# its receiver correlates against samples rather than matching bits, and
# 16 bytes delivered the same as 32 through the FM model (fading 5-80 km/h,
# 8-15 dB CNR) while 8 and below started missing frames in slow fading.
# See PROTOCOL_V2_PROPOSAL.md. The IQ PHY keeps all 32.
AUDIO_PREAMBLE_LEN = 16
AUDIO_MARKER = PREAMBLE[:AUDIO_PREAMBLE_LEN] + SYNC_WORD
IQ_MARKER = PREAMBLE + SYNC_WORD

HEADER_LEN = 20
CRC_LEN = 4
MAX_PAYLOAD_LEN = 1024
MAX_FRAGMENTS = 64  # defensive cap on a single reassembly - see FragmentReassembler

PACKET_TYPE_DATA = 0x01
PACKET_TYPE_ACK = 0x02
PACKET_TYPE_BEACON = 0x03
PACKET_TYPE_CONTROL = 0x04
# The payload is one AX.25 frame (as KISS carries it: addresses, control,
# PID and info, no flags or FCS) for a KISS host - see fmlink_tnc.py.
PACKET_TYPE_AX25 = 0x05

_PACKET_TYPE_NAMES = {
    PACKET_TYPE_DATA: 'Data',
    PACKET_TYPE_ACK: 'ACK',
    PACKET_TYPE_BEACON: 'Beacon',
    PACKET_TYPE_CONTROL: 'Control',
    PACKET_TYPE_AX25: 'AX.25',
}


def format_packet_type(packet_type):
    """Packet Type byte -> display name, e.g. 'Data' or 'Unknown(0x05)'."""
    name = _PACKET_TYPE_NAMES.get(packet_type)
    return name if name is not None else f'Unknown(0x{packet_type:02X})'


VERSION = 2

SEQ_MODULUS = 0x10000  # Sequence Number is a 16-bit field

FLAG_ACK_REQUESTED = 0x01
FLAG_ACK_RESPONSE = 0x02
FLAG_FRAGMENTED = 0x04
FLAG_MORE_FRAGMENTS = 0x08
FLAG_PRIORITY = 0x10
# The payload starts with a header-extension block - see split_extension().
FLAG_HEADER_EXTENSION = 0x20
# bits 6-7 reserved

_FLAG_NAMES = (
    (FLAG_ACK_REQUESTED, 'ack_requested'),
    (FLAG_ACK_RESPONSE, 'ack_response'),
    (FLAG_FRAGMENTED, 'fragmented'),
    (FLAG_MORE_FRAGMENTS, 'more_fragments'),
    (FLAG_PRIORITY, 'priority'),
    (FLAG_HEADER_EXTENSION, 'header_extension'),
)


def parse_flags(flags_byte):
    """Flags byte -> {'ack_requested': bool, 'ack_response': bool, 'fragmented': bool,
    'more_fragments': bool, 'priority': bool, 'header_extension': bool}."""
    return {name: bool(flags_byte & bit) for bit, name in _FLAG_NAMES}


def format_flags(flags_byte):
    """Flags byte -> short display string, e.g. 'ack_requested,priority' or '-' if none set."""
    names = [name for bit, name in _FLAG_NAMES if flags_byte & bit]
    return ','.join(names) if names else '-'

# Frame format v2 (see PROTOCOL_V2_PROPOSAL.md): version(4 bits)|type(4 bits)
# in one byte, mode(B) flags(B) seq(H) src(6s) dst(6s) payload_len(H)
# fragment_index(B) - 20 bytes, down from v1's 32 (v1 had 8-byte ASCII IDs,
# separate version/type bytes and 7 reserved bytes). All multi-byte fields
# big-endian. Callsigns are packed base-40 (see pack_callsign()).
#
# fragment_index: the position of this frame within its fragmented-message
# run (0 = this frame genuinely starts a new run). It gives
# FragmentReassembler an authoritative signal for "is this really a new
# run" instead of inferring it from "nothing else is in progress" - that
# inference was silently wrong whenever a run got abandoned mid-stream (e.g.
# MAX_FRAGMENTS overflow): the very next leftover continuation fragment used
# to get misread as a fresh start, so a receiver could silently get delivered
# a truncated tail of an oversized message with a normal 'ok' status and no
# indication anything was wrong. See FragmentReassembler's docstring.
_HEADER_STRUCT = struct.Struct('>BBBH6s6sHB')
assert _HEADER_STRUCT.size == HEADER_LEN


# Callsign alphabet for the packed 6-byte Source/Destination fields: index 0
# is end/padding, so a 9-character callsign is 9 base-40 digits, and
# 40**9 < 2**48 always fits in 6 bytes.
CALLSIGN_ALPHABET = '\0ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-.'
CALLSIGN_MAX_LEN = 9
_CALLSIGN_FIELD_LEN = 6


def pack_callsign(callsign):
    """Callsign (str or ASCII bytes, up to 9 characters from
    CALLSIGN_ALPHABET; lowercase is folded to uppercase) -> 6 packed bytes:
    the characters as base-40 digits, most significant first, padded with
    0 to 9 digits, as a 48-bit big-endian integer. '' packs to all zeros.
    Raises ValueError for anything that can't be represented."""
    if isinstance(callsign, bytes):
        callsign = callsign.rstrip(b'\x00').decode('ascii', errors='replace')
    callsign = callsign.upper()
    if len(callsign) > CALLSIGN_MAX_LEN:
        raise ValueError(f'callsign {callsign!r} is longer than {CALLSIGN_MAX_LEN} characters')
    for ch in callsign:
        if ch == '\0' or ch not in CALLSIGN_ALPHABET:
            raise ValueError(f'callsign {callsign!r}: {ch!r} is not allowed '
                             f'(A-Z, 0-9, /, -, . only)')
    value = 0
    for ch in callsign.ljust(CALLSIGN_MAX_LEN, '\0'):
        value = value * 40 + CALLSIGN_ALPHABET.index(ch)
    return value.to_bytes(_CALLSIGN_FIELD_LEN, 'big')


def unpack_callsign(field):
    """Inverse of pack_callsign(): 6 bytes -> the callsign as ASCII bytes
    (b'' for an all-zero field), stopping at the first end/padding digit."""
    value = int.from_bytes(field, 'big')
    digits = []
    for _ in range(CALLSIGN_MAX_LEN):
        digits.append(value % 40)
        value //= 40
    chars = []
    for digit in reversed(digits):
        if digit == 0:
            break
        chars.append(CALLSIGN_ALPHABET[digit])
    return ''.join(chars).encode('ascii')


def build_header(payload_len, version=VERSION, packet_type=PACKET_TYPE_DATA,
                  mode_id=0, flags=0, seq=0, src_id=b'', dst_id=b'', fragment_index=0):
    """Build the 20-byte v2 header. src_id/dst_id are callsigns (str or
    ASCII bytes) packed with pack_callsign() - ValueError if one can't be.
    seq wraps automatically at SEQ_MODULUS (16 bits) - callers don't need to.
    fragment_index only matters when the Fragmented flag is set - see this
    module's header-struct comment and FragmentReassembler."""
    if not (0 <= payload_len <= MAX_PAYLOAD_LEN):
        raise ValueError(f'payload_len {payload_len} out of range 0..{MAX_PAYLOAD_LEN}')
    if not (0 <= version <= 0xF and 0 <= packet_type <= 0xF):
        raise ValueError(f'version {version} and packet_type {packet_type} must each fit in 4 bits')
    return _HEADER_STRUCT.pack(
        (version << 4) | packet_type, mode_id, flags, seq % SEQ_MODULUS,
        pack_callsign(src_id), pack_callsign(dst_id), payload_len, fragment_index % 0x100,
    )


def parse_header(header_bytes):
    """Parse a 20-byte v2 header into a dict of fields. src_id/dst_id come
    back unpacked, as the callsign's ASCII bytes (e.g. b'K0MDT')."""
    if len(header_bytes) != HEADER_LEN:
        raise ValueError(f'header must be exactly {HEADER_LEN} bytes, got {len(header_bytes)}')
    version_type, mode_id, flags, seq, src_id, dst_id, payload_len, fragment_index = \
        _HEADER_STRUCT.unpack(header_bytes)
    return {
        'version': version_type >> 4, 'packet_type': version_type & 0xF, 'mode_id': mode_id,
        'flags': flags, 'seq': seq, 'src_id': unpack_callsign(src_id),
        'dst_id': unpack_callsign(dst_id), 'payload_len': payload_len,
        'fragment_index': fragment_index,
    }


def split_extension(payload):
    """For a frame with FLAG_HEADER_EXTENSION set: payload -> (entries,
    application payload). The payload starts with [block length N][N bytes
    of entries], each entry [type][length L][L bytes]; entries is a list of
    (type, value bytes), in order - a receiver ignores types it doesn't
    know. No entry types are defined yet. Raises ValueError if the block
    is malformed."""
    if not payload:
        raise ValueError('header-extension flag set but the payload is empty')
    block_len = payload[0]
    block = payload[1:1 + block_len]
    if len(block) < block_len:
        raise ValueError(f'header extension declares {block_len} bytes, only {len(block)} present')
    entries, pos = [], 0
    while pos < len(block):
        if pos + 2 > len(block):
            raise ValueError('header extension entry truncated')
        entry_type, entry_len = block[pos], block[pos + 1]
        value = block[pos + 2:pos + 2 + entry_len]
        if len(value) < entry_len:
            raise ValueError('header extension entry runs past the block')
        entries.append((entry_type, value))
        pos += 2 + entry_len
    return entries, payload[1 + block_len:]


def application_payload(header, payload):
    """The payload a frame actually carries for its application - payload
    minus any header-extension block (see split_extension())."""
    if header['flags'] & FLAG_HEADER_EXTENSION:
        return split_extension(payload)[1]
    return payload


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


def build_frame_with_bad_length(payload, declared_length, lead_pad=24, end_pad=24,
                                 version=VERSION, packet_type=PACKET_TYPE_DATA,
                                 mode_id=0, flags=0, seq=0, src_id=b'', dst_id=b''):
    """Like build_frame(), but writes declared_length into the header's
    Payload Length field instead of len(payload) - for deliberately testing
    FrameParser's 'bad_length' rejection path (see fmlink_cli.py's `send
    --bad-length`). declared_length is NOT range-checked (that's the point -
    a real payload_len this far outside 0..MAX_PAYLOAD_LEN should never occur
    naturally); FrameParser catches it immediately after parsing the header,
    before it would ever try to read declared_length bytes of payload or
    check a CRC over them - so the CRC computed here (over the real
    header+payload, which is a lie once declared_length disagrees with
    len(payload)) is never actually checked by a well-behaved receiver."""
    header = _HEADER_STRUCT.pack(
        (version << 4) | packet_type, mode_id, flags, seq % SEQ_MODULUS,
        pack_callsign(src_id), pack_callsign(dst_id), declared_length % 0x10000, 0,
    )
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


def build_selective_ack_frame(first_seq, count, received, lead_pad=24, end_pad=24, **header_kwargs):
    """A selective ACK for a batched message (BURST_BATCHING_PROPOSAL.md):
    an ACK frame whose Sequence Number is the message's first segment's and
    whose payload says which of its segments arrived - one count byte (1..
    MAX_FRAGMENTS), then a bitmap, bit i (LSB first) = fragment index i.
    An ACK with an empty payload stays a plain single-frame ACK."""
    if not 1 <= count <= MAX_FRAGMENTS:
        raise ValueError(f'selective ACK count {count} out of range 1..{MAX_FRAGMENTS}')
    bitmap = bytearray((count + 7) // 8)
    for index in received:
        if 0 <= index < count:
            bitmap[index // 8] |= 1 << (index % 8)
    header_kwargs.setdefault('packet_type', PACKET_TYPE_ACK)
    header_kwargs['flags'] = header_kwargs.get('flags', 0) | FLAG_ACK_RESPONSE
    return build_frame(bytes([count]) + bytes(bitmap), lead_pad=lead_pad, end_pad=end_pad,
                       seq=first_seq, **header_kwargs)


def parse_selective_ack(payload):
    """A selective ACK's payload -> (count, set of fragment indices received).
    Raises ValueError if it's malformed."""
    if not payload:
        raise ValueError('empty selective ACK payload')
    count = payload[0]
    if not 1 <= count <= MAX_FRAGMENTS or len(payload) < 1 + (count + 7) // 8:
        raise ValueError(f'malformed selective ACK (count {count}, {len(payload)} bytes)')
    return count, {i for i in range(count) if payload[1 + i // 8] >> (i % 8) & 1}


# Payload format for PACKET_TYPE_CONTROL frames - ota_station.py's --auto
# mode-negotiation handshake (see its negotiate()/_handle_control_frame()).
# Not part of FMlink_Specification.txt's spec yet - this is a v1 protocol
# extension living entirely in the payload, so it doesn't need a header
# format change. op byte first so parse_control_payload() can dispatch
# before knowing which build_* function produced it.
#
# Two-phase handshake: REQUEST/GRANT (phase 1) exists purely so the
# INITIATOR has something of its peer's to measure (a station can only
# ever measure what arrives AT it, never what it sends) - the GRANT's own
# embedded mode_id/fec_preset is informational only now, not adopted by
# either side. SET/SET_ACK (phase 2) is the initiator authoritatively
# telling its peer the ONE mode/FEC it measured and chose - both stations
# end up on that same mode, not two independently-chosen ones. See
# ota_station.py's negotiate() for the full flow.
CONTROL_OP_MODE_REQUEST = 1
CONTROL_OP_MODE_GRANT = 2
CONTROL_OP_MODE_SET = 3
CONTROL_OP_MODE_SET_ACK = 4
# PING/PONG: ota_station.py's /ping and /ping survey diagnostics - a bare
# round trip (no payload beyond the op byte) for measuring RTT/link
# quality, either at whatever mode is currently active (/ping) or, for a
# survey, at each waveform mode in turn after actually switching both
# stations to it via a normal negotiate()-style MODE_SET/MODE_SET_ACK
# exchange first (see ota_station.py's _announce_mode()/ping_survey()) -
# PING/PONG themselves carry no mode information, unlike REQUEST/GRANT.
CONTROL_OP_PING = 5
CONTROL_OP_PONG = 6

_CONTROL_GRANT_STRUCT = struct.Struct('>BfBB')  # op, snr_db (float32), mode_id, fec preset ID
_CONTROL_SET_STRUCT = struct.Struct('>BBB')  # op, mode_id, fec preset ID
# Optional trailing capabilities byte on MODE_GRANT and MODE_SET (added
# 2026-10-08). Stations that predate it ignore trailing bytes and send none,
# which reads as 0: no optional features.
CAP_BATCH = 0x01  # burst batching with selective ACKs (BURST_BATCHING_PROPOSAL.md)

# 1-byte wire IDs for fmlink_fec.py's PRESETS, used in MODE_GRANT/MODE_SET
# payloads (v1 sent the preset's name as ASCII, ~7 bytes). This numbering
# is part of the wire format: never renumber, only append new presets.
FEC_PRESET_IDS = {
    'r1_2_k3': 1, 'r1_2_k7': 2, 'r1_2_k9': 3,
    'r1_3_k3': 4, 'r1_3_k7': 5, 'r1_3_k9': 6,
    'r1_4_k3': 7, 'r1_4_k7': 8, 'r1_4_k9': 9,
    'r1_5_k7': 10, 'r1_5_k9': 11,
    'r1_6_k7': 12, 'r1_6_k9': 13,
}
_FEC_PRESETS_BY_ID = {preset_id: name for name, preset_id in FEC_PRESET_IDS.items()}


def _fec_preset_id(fec_preset):
    try:
        return FEC_PRESET_IDS[fec_preset]
    except KeyError:
        raise ValueError(f'FEC preset {fec_preset!r} has no wire ID (see FEC_PRESET_IDS)') from None


def _fec_preset_name(preset_id):
    try:
        return _FEC_PRESETS_BY_ID[preset_id]
    except KeyError:
        raise ValueError(f'unknown FEC preset ID {preset_id}') from None


def build_mode_request_frame(seq, lead_pad=24, end_pad=24, **header_kwargs):
    """A station probing for a mode grant: Packet Type Control, empty
    content beyond the op byte. Sent at a fixed, always-robust mode/FEC
    (ota_station.py's CONTROL_MODE_ID/CONTROL_FEC_PRESET) so it has the
    best chance of surviving even a link too weak for any negotiated data
    mode. seq lets the requester match a later grant back to this specific
    request (echoed, like build_ack_frame's acked_seq)."""
    header_kwargs.setdefault('packet_type', PACKET_TYPE_CONTROL)
    return build_frame(bytes([CONTROL_OP_MODE_REQUEST]), lead_pad=lead_pad,
                        end_pad=end_pad, seq=seq, **header_kwargs)


def build_mode_grant_frame(seq, granted_mode_id, fec_preset, snr_db, lead_pad=24, end_pad=24,
                            capabilities=0, **header_kwargs):
    """A station's reply to a mode request: the mode_id/fec_preset it
    selected for the requester to use (see select_mode_for_snr()), plus the
    snr_db estimate that selection was based on (purely informational - the
    requester adopts granted_mode_id/fec_preset, not snr_db). fec_preset is
    a PRESETS name, sent as its 1-byte FEC_PRESET_IDS wire ID.

    granted_mode_id is deliberately a distinct name from the header's own
    mode_id field (in header_kwargs, if the caller passes one) - they're
    unrelated: header mode_id describes what mode THIS frame itself was
    sent with (always the fixed control mode), while granted_mode_id is
    payload content naming a DIFFERENT mode for the requester's future
    sends."""
    header_kwargs.setdefault('packet_type', PACKET_TYPE_CONTROL)
    payload = _CONTROL_GRANT_STRUCT.pack(CONTROL_OP_MODE_GRANT, snr_db, granted_mode_id,
                                         _fec_preset_id(fec_preset)) + bytes([capabilities])
    return build_frame(payload, lead_pad=lead_pad, end_pad=end_pad, seq=seq, **header_kwargs)


def build_mode_set_frame(seq, chosen_mode_id, fec_preset, lead_pad=24, end_pad=24, capabilities=0,
                         **header_kwargs):
    """The initiator's authoritative announcement, after measuring its OWN
    reception of the peer's mode_grant_frame reply: "use this mode/FEC for
    BOTH directions" - unlike build_mode_grant_frame's snr_db, this
    carries no measurement, just the decision itself. fec_preset is sent
    as its 1-byte wire ID, same as build_mode_grant_frame.

    chosen_mode_id is deliberately a distinct name from the header's own
    mode_id field (in header_kwargs, if the caller passes one) - same
    reasoning as build_mode_grant_frame's granted_mode_id: header mode_id
    is what mode THIS frame itself was sent with (the fixed control
    mode), unrelated to chosen_mode_id, which is payload content naming
    the mode both stations are about to switch to."""
    header_kwargs.setdefault('packet_type', PACKET_TYPE_CONTROL)
    payload = (_CONTROL_SET_STRUCT.pack(CONTROL_OP_MODE_SET, chosen_mode_id, _fec_preset_id(fec_preset))
               + bytes([capabilities]))
    return build_frame(payload, lead_pad=lead_pad, end_pad=end_pad, seq=seq, **header_kwargs)


def build_mode_set_ack_frame(seq, lead_pad=24, end_pad=24, **header_kwargs):
    """Acknowledges a mode_set_frame (echoing its seq) - the initiator
    waits for this before applying the new mode itself and before sending
    any real data, so a lost MODE_SET can't leave the two stations on
    different modes."""
    header_kwargs.setdefault('packet_type', PACKET_TYPE_CONTROL)
    return build_frame(bytes([CONTROL_OP_MODE_SET_ACK]), lead_pad=lead_pad,
                        end_pad=end_pad, seq=seq, **header_kwargs)


def build_ping_frame(seq, lead_pad=24, end_pad=24, **header_kwargs):
    """A bare round-trip probe for ota_station.py's /ping - no payload
    beyond the op byte; seq is what a reply is matched back to (like
    build_ack_frame's acked_seq). Unlike build_mode_request_frame, this
    is sent at whatever mode is CURRENTLY active (the caller's choice,
    via header_kwargs/the transmit path used) - the point is to measure
    that mode's real round-trip behavior, not to probe at a fixed
    control mode."""
    header_kwargs.setdefault('packet_type', PACKET_TYPE_CONTROL)
    return build_frame(bytes([CONTROL_OP_PING]), lead_pad=lead_pad,
                        end_pad=end_pad, seq=seq, **header_kwargs)


def build_pong_frame(seq, lead_pad=24, end_pad=24, **header_kwargs):
    """Replies to a ping_frame, echoing its seq - same bare shape as
    build_ping_frame, just the other op byte."""
    header_kwargs.setdefault('packet_type', PACKET_TYPE_CONTROL)
    return build_frame(bytes([CONTROL_OP_PONG]), lead_pad=lead_pad,
                        end_pad=end_pad, seq=seq, **header_kwargs)


def parse_control_payload(payload):
    """A Control frame's payload -> {'op': int} for a request, set-ack,
    ping, or pong; {'op': int, 'mode_id': int, 'fec_preset': str,
    'snr_db': float} for a grant; or {'op': int, 'mode_id': int,
    'fec_preset': str} for a set. Raises ValueError for an unrecognized/
    malformed op - a CRC-verified frame guarantees the bytes are what the
    sender put there, but not that the sender speaks this same control-
    payload version."""
    if not payload:
        raise ValueError('empty Control frame payload')
    op = payload[0]
    if op in (CONTROL_OP_MODE_REQUEST, CONTROL_OP_MODE_SET_ACK, CONTROL_OP_PING, CONTROL_OP_PONG):
        return {'op': op}
    if op == CONTROL_OP_MODE_GRANT:
        if len(payload) < _CONTROL_GRANT_STRUCT.size:
            raise ValueError(f'mode grant payload too short ({len(payload)} bytes)')
        _, snr_db, mode_id, preset_id = _CONTROL_GRANT_STRUCT.unpack(payload[:_CONTROL_GRANT_STRUCT.size])
        return {'op': op, 'mode_id': mode_id, 'fec_preset': _fec_preset_name(preset_id),
                'snr_db': snr_db, 'capabilities': _trailing_byte(payload, _CONTROL_GRANT_STRUCT.size)}
    if op == CONTROL_OP_MODE_SET:
        if len(payload) < _CONTROL_SET_STRUCT.size:
            raise ValueError(f'mode set payload too short ({len(payload)} bytes)')
        _, mode_id, preset_id = _CONTROL_SET_STRUCT.unpack(payload[:_CONTROL_SET_STRUCT.size])
        return {'op': op, 'mode_id': mode_id, 'fec_preset': _fec_preset_name(preset_id),
                'capabilities': _trailing_byte(payload, _CONTROL_SET_STRUCT.size)}
    raise ValueError(f'unrecognized Control op {op}')


def _trailing_byte(payload, at):
    return payload[at] if len(payload) > at else 0


def needs_ack(header):
    """True if a received (CRC-verified) header is a Data frame asking to be acknowledged."""
    return header['packet_type'] == PACKET_TYPE_DATA and parse_flags(header['flags'])['ack_requested']


def is_ack(header):
    """True if a received (CRC-verified) header is itself an ACK response."""
    return header['packet_type'] == PACKET_TYPE_ACK and parse_flags(header['flags'])['ack_response']


def iter_frames(data):
    """Scan raw bytes for PREAMBLE+SYNC_WORD occurrences (as produced by
    build_frame()/build_fragmented_frames()) and yield a
    (status, header_or_None, payload_or_None) tuple for each: ('ok', header,
    payload), ('crc_fail', None, None), or ('bad_length', None, None).

    Byte-granularity and noise-free - this is for offline/file-based testing
    of the link layer itself (see fmlink_cli.py), NOT a stand-in for a
    real sync-word correlator over a noisy sample stream. That job (finding
    the sync word in a bitstream that's gone through modulation, a channel,
    and demodulation) is what digital_correlate_access_code_tag_bb +
    FrameParser do together inside the GNU Radio flowgraphs - this function
    only has to find literal bytes in a literal buffer.
    """
    marker = PREAMBLE + SYNC_WORD
    pos = 0
    while True:
        idx = data.find(marker, pos)
        if idx == -1:
            return
        parser = FrameParser()
        i = idx + len(marker)
        found_result = False
        while i < len(data):
            status, payload = parser.feed(data[i])
            i += 1
            if status == 'ok':
                yield 'ok', parser.last_header, payload
                found_result = True
                break
            if status in ('crc_fail', 'bad_length'):
                yield status, None, None
                found_result = True
                break
        if not found_result:
            return  # ran out of bytes mid-frame; nothing more to find
        pos = idx + 1


def describe_frames(data):
    """Like iter_frames(), but yields full diagnostic detail instead of just
    (header, payload) - every raw byte range (preamble, sync word, header,
    payload, CRC) plus whether the CRC actually matched, for tools that want
    to *display* a frame rather than just consume it (see fmlink_cli.py's
    `dump` command). Byte-granularity and noise-free, same caveat as
    iter_frames() about not being a stand-in for a real sync-word correlator.

    Yields one dict per PREAMBLE+SYNC_WORD marker found in data:
      {'offset': int, 'status': 'ok'|'crc_fail'|'bad_length'|'truncated',
       'preamble': bytes, 'sync_word': bytes,
       'header_bytes': bytes or None, 'header': dict or None,
       'payload': bytes or None, 'crc_bytes': bytes or None,
       'total_len': int or None}
    'header'/'payload'/'crc_bytes'/'total_len' are None whenever there
    weren't enough bytes left to parse that far (status 'truncated'), or
    (header_bytes/header only) when payload_len itself was out of range
    (status 'bad_length' - the header parsed, but nothing past it can be
    trusted to be at the right offset)."""
    marker = PREAMBLE + SYNC_WORD
    pos = 0
    while True:
        idx = data.find(marker, pos)
        if idx == -1:
            return
        preamble = data[idx:idx + len(PREAMBLE)]
        sync_word = data[idx + len(PREAMBLE):idx + len(marker)]
        header_start = idx + len(marker)
        header_bytes = data[header_start:header_start + HEADER_LEN]

        if len(header_bytes) < HEADER_LEN:
            yield {'offset': idx, 'status': 'truncated', 'preamble': preamble,
                   'sync_word': sync_word, 'header_bytes': None, 'header': None,
                   'payload': None, 'crc_bytes': None, 'total_len': None}
            return

        header = parse_header(header_bytes)
        if not (0 <= header['payload_len'] <= MAX_PAYLOAD_LEN):
            yield {'offset': idx, 'status': 'bad_length', 'preamble': preamble,
                   'sync_word': sync_word, 'header_bytes': header_bytes, 'header': header,
                   'payload': None, 'crc_bytes': None, 'total_len': None}
            pos = idx + 1
            continue

        payload_start = header_start + HEADER_LEN
        payload_end = payload_start + header['payload_len']
        crc_end = payload_end + CRC_LEN
        if len(data) < crc_end:
            yield {'offset': idx, 'status': 'truncated', 'preamble': preamble,
                   'sync_word': sync_word, 'header_bytes': header_bytes, 'header': header,
                   'payload': None, 'crc_bytes': None, 'total_len': None}
            return

        payload = data[payload_start:payload_end]
        crc_bytes = data[payload_end:crc_end]
        received_crc, = struct.unpack('>I', crc_bytes)
        status = 'ok' if (zlib.crc32(header_bytes + payload) & 0xFFFFFFFF) == received_crc else 'crc_fail'

        yield {'offset': idx, 'status': status, 'preamble': preamble, 'sync_word': sync_word,
               'header_bytes': header_bytes, 'header': header, 'payload': payload,
               'crc_bytes': crc_bytes, 'total_len': crc_end - idx}
        pos = idx + 1


class LinkReceiver:
    """RX dispatch logic: address filtering, ACK vs. fragmented vs. plain
    Data frame handling, sequence gap/duplicate tracking, and a formatted
    stats line. This is the byte-level, GNU-Radio-free counterpart to the
    dispatch logic embedded in rx_rtlsdr.grc's stream_display_sink_0
    epy_block - feed it (header, payload) pairs from iter_frames() (offline/
    file-based use) or your own sync detector + FrameParser, and it behaves
    the same way.

    on_message/on_ack_needed/on_ack_received are optional callbacks (default
    no-ops), fired as described in feed_frame(). on_raw_message is an
    additional optional callback that fires alongside on_message with the
    payload's original, undecoded bytes - on_message's text is produced via
    payload_bytes.decode('utf-8', errors='replace'), which is lossy/
    corrupting for arbitrary binary content (e.g. a file transfer), so
    on_raw_message exists for callers that need the real bytes back (see
    fmlink_cli.py's `send --file`/`recv --output-file`).
    """

    def __init__(self, station_id='', on_message=None, on_ack_needed=None,
                 on_ack_received=None, on_raw_message=None,
                 on_selective_ack_needed=None, on_selective_ack_received=None):
        self.station_id = station_id
        self.reassembler = FragmentReassembler()
        # Burst batching (BURST_BATCHING_PROPOSAL.md), once both stations
        # have negotiated it: segments reassemble in any order and are
        # acknowledged by selective ACKs (see feed_frame()).
        self.selective = False
        self.selective_reassembler = SelectiveReassembler()
        self._on_selective_ack_needed = on_selective_ack_needed or (lambda src, first_seq, now: None)
        self._on_selective_ack_received = on_selective_ack_received or (lambda seq, count, got: None)
        self.counts = {'ok': 0, 'crc_fail': 0, 'bad_length': 0, 'truncated': 0, 'dup': 0,
                        'lost': 0, 'acks_rx': 0, 'frag_aborted': 0, 'frag_overflow': 0,
                        'frag_discarded': 0, 'not_for_us': 0, 'bad_extension': 0}
        self.last_seq = None
        self.last_flags = 0
        self.last_mode = 0
        self.last_src = ''
        self.last_dst = ''
        self._on_message = on_message or (lambda text: None)
        self._on_ack_needed = on_ack_needed or (lambda seq: None)
        self._on_ack_received = on_ack_received or (lambda seq: None)
        self._on_raw_message = on_raw_message or (lambda payload_bytes: None)

    def _update_seq(self, header):
        seq = header['seq']
        if self.last_seq is not None:
            if seq == self.last_seq:
                self.counts['dup'] += 1
            else:
                gap = (seq - self.last_seq - 1) % SEQ_MODULUS
                # Bound the gap we'll believe as real loss vs. e.g. TX having
                # restarted (seq reset to 0) - avoids one bogus huge count.
                if 0 < gap < 1000:
                    self.counts['lost'] += gap
        self.last_seq = seq
        self.last_flags = header['flags']

    def _update_addressing(self, header):
        self.last_mode = header['mode_id']
        self.last_src = decode_station_id(header['src_id'])
        self.last_dst = decode_station_id(header['dst_id'])

    def _publish(self, header, payload_bytes):
        self.counts['ok'] += 1
        self._on_raw_message(payload_bytes)
        text = payload_bytes.decode('utf-8', errors='replace')
        if text:
            self._on_message(text)

    def feed_frame(self, header, payload):
        """Feed one CRC-verified (header, payload) pair - e.g. the 'ok'
        result from FrameParser.feed(), or one yielded by iter_frames().
        Updates counts/last_* and fires callbacks:
          - dropped silently (counted under 'not_for_us') if Destination ID
            doesn't clear is_for_station()
          - an ACK frame -> on_ack_received(seq)
          - a fragment -> buffered until the run completes, then on_message
            fires with the full reassembled text
          - a plain Data frame -> on_message fires immediately
        A Data frame (fragmented or not) with the ack_requested flag fires
        on_ack_needed(seq) immediately on arrival - per FRAME, not just once
        when a fragmented run completes - so a caller doing per-fragment
        retry (stop-and-wait) can tell exactly which fragments landed.

        A fragmented frame whose seq repeats the immediately-preceding
        frame's (the caller retried it, most likely because its ACK got
        lost on the way back, not because the fragment itself was bad) is
        re-acked here but NOT re-fed to the reassembler: it expects strictly
        incrementing fragment_index per call and would otherwise treat the
        repeat as an out-of-order mismatch and wrongly abort an
        in-progress or just-completed run. Non-fragmented duplicates are
        unaffected - they still reach _publish() and get delivered, same as
        always (see _update_seq()'s 'dup' counting; callers that want to
        suppress duplicate delivery to a user do so themselves, e.g.
        ota_station.py)."""
        is_repeat = self.last_seq is not None and header['seq'] == self.last_seq
        self._update_seq(header)
        self._update_addressing(header)

        if not is_for_station(header['dst_id'], self.station_id):
            self.counts['not_for_us'] += 1
            return

        if is_ack(header):
            self.counts['acks_rx'] += 1
            if payload:
                try:
                    count, received = parse_selective_ack(payload)
                except ValueError:
                    self.counts['bad_extension'] += 1
                    return
                self._on_selective_ack_received(header['seq'], count, received)
            else:
                self._on_ack_received(header['seq'])
            return

        # A malformed header-extension block can't be trusted to say where the
        # application payload starts - drop the frame (unacked, so a sender
        # retries) rather than deliver misaligned bytes.
        try:
            payload = application_payload(header, payload)
        except ValueError:
            self.counts['bad_extension'] += 1
            return

        if self.selective and parse_flags(header['flags'])['fragmented']:
            # A batched segment: any order, resends welcome. Acknowledge
            # (selectively) only after it's been taken in - now if it asks
            # for an ACK, else let the station decide when the batch is over.
            frag_status, reassembled = self.selective_reassembler.feed(header, payload)
            self.counts['frag_aborted'] = self.selective_reassembler.abort_count
            self.counts['frag_overflow'] = self.selective_reassembler.overflow_count
            self.counts['frag_discarded'] = self.selective_reassembler.discarded_count
            if frag_status == 'complete':
                self._publish(header, reassembled)
            first_seq = (header['seq'] - header['fragment_index']) % SEQ_MODULUS
            self._on_selective_ack_needed(header['src_id'], first_seq, needs_ack(header))
            return

        if needs_ack(header):
            self._on_ack_needed(header['seq'])

        if parse_flags(header['flags'])['fragmented']:
            if is_repeat:
                return
            frag_status, reassembled = self.reassembler.feed(header, payload)
            self.counts['frag_aborted'] = self.reassembler.abort_count
            self.counts['frag_overflow'] = self.reassembler.overflow_count
            self.counts['frag_discarded'] = self.reassembler.discarded_count
            if frag_status == 'complete':
                self._publish(header, reassembled)
            return

        self._publish(header, payload)

    def feed_crc_failure(self, status):
        """Call for a non-'ok' FrameParser.feed()/iter_frames() result:
        status is 'crc_fail' or 'bad_length' from those, or - only from
        fmlink_fec.py's decode_frames(), which unlike the uncoded sync
        path doesn't swallow it internally - 'truncated' (not enough data
        remained for a complete block, e.g. a burst cut short by noise
        below the sync-word/header hit)."""
        self.counts[status] += 1

    def stats_line(self):
        """Formatted one-line summary, matching rx_rtlsdr.grc's 'stats' port."""
        c = self.counts
        seq_str = str(self.last_seq) if self.last_seq is not None else '-'
        return (f"OK:{c['ok']}  CRC fail:{c['crc_fail']}  Bad length:{c['bad_length']}  "
                f"Truncated:{c['truncated']}  "
                f"Dup:{c['dup']}  Lost:{c['lost']}  ACKs rx:{c['acks_rx']}  "
                f"Frag abort:{c['frag_aborted']}  Frag overflow:{c['frag_overflow']}  "
                f"Frag discarded:{c['frag_discarded']}  "
                f"Not for us:{c['not_for_us']}  |  "
                f"Seq:{seq_str}  Flags:{format_flags(self.last_flags)}  "
                f"{format_mode(self.last_mode)}  "
                f"Src:{self.last_src or '-'}  Dst:{self.last_dst or '-'}")


# WAVEFORM_MODES is the PHY-plugin contract: what each Waveform Mode ID
# *should* imply (modulation, symbol rate, FEC preset), so FMlink stays a
# link-layer/framing protocol over a pluggable PHY rather than being tied to
# any one modulation. Modulation names match modulate.py's MODULATORS /
# demodulate.py's DEMODULATORS dict keys ('bpsk'/'qpsk'/'16qam'); fec_preset
# values match fmlink_fec.py's PRESETS keys.
#
# This is a reference table for callers to consult, not force-wired into
# modulate.py/demodulate.py's -t flag or fec_encode.py/fec_decode.py's
# --preset flag, which remain independent CLI choices today - see
# tasklist.txt Phase 6 for the follow-up to unify these.
WAVEFORM_MODES = {
    0: {'modulation': 'bpsk',  'symbol_rate': 600,  'fec_preset': 'r1_2_k7'},
    1: {'modulation': 'bpsk',  'symbol_rate': 1200, 'fec_preset': 'r1_2_k7'},
    # Mode 2 fills a gap an original 5-mode spread (0/1/[gap]/3/4 in what
    # are now modes 0/1/3/4/5) skipped: every OTHER modulation step
    # doubled throughput via symbol rate alone, but the modulation-order
    # steps doubled it via bits/symbol AND symbol rate at once (a 4x
    # jump), with no "same bits/symbol bump, same rate as the slower BPSK
    # mode" QPSK entry in between. Renumbered into its natural throughput
    # slot (not appended at the end) - FMlink_Specification.txt's
    # Waveform Modes table/worked example and every mode-number reference
    # elsewhere were updated to match when this happened.
    2: {'modulation': 'qpsk',  'symbol_rate': 1200, 'fec_preset': 'r1_2_k7'},
    3: {'modulation': 'qpsk',  'symbol_rate': 2400, 'fec_preset': 'r1_2_k7'},
    4: {'modulation': 'qpsk',  'symbol_rate': 4800, 'fec_preset': 'r1_2_k7'},
    5: {'modulation': '16qam', 'symbol_rate': 9600, 'fec_preset': 'r1_2_k7'},
}

# Derived for back-compat with any existing symbol-rate-only lookup.
WAVEFORM_MODE_SYMBOL_RATES = {m: info['symbol_rate'] for m, info in WAVEFORM_MODES.items()}

# bits/symbol per modulation scheme - matches modulate.py's MODULATORS/
# demodulate.py's DEMODULATORS keys. Used by mode_raw_bps() below; also
# the same convention FMlink_Specification.txt's Waveform Modes table
# uses for its "bps raw" column (symbol_rate * bits/symbol).
_BITS_PER_SYMBOL = {'bpsk': 1, 'qpsk': 2, '16qam': 4}


def mode_raw_bps(mode_id):
    """Waveform Mode ID -> raw bit rate (symbol_rate * bits/symbol),
    ignoring FEC overhead - a mode-agnostic way to compare/rank modes by
    actual throughput. Use this rather than assuming mode_id order tracks
    throughput when ranking modes (ota_station.py's _ping_survey_summary()
    does) - WAVEFORM_MODES happens to be in throughput order today, but
    a table edit could easily break that again, the way inserting what's
    now mode 2 briefly did before it was renumbered into place."""
    info = WAVEFORM_MODES[mode_id]
    return info['symbol_rate'] * _BITS_PER_SYMBOL[info['modulation']]

# Mode/FEC auto-selection thresholds for ota_station.py's --auto handshake:
# (min_snr_db, mode_id, fec_preset), ordered weakest-link-first.
# select_mode_for_snr() picks the highest-throughput row whose min_snr_db is
# still <= the measured value - so this is read as "once the link is at
# least this good, step up to this mode/preset".
#
# IMPORTANT: snr_db here is NOT a calibrated/textbook SNR - it's whatever
# fmlink_modem.py's estimate_snr_db() reports on the negotiation
# handshake's own BPSK control-channel burst specifically (see
# ota_station.py's CONTROL_MODE_ID). That estimator is a blind heuristic
# with a real, measured optimistic bias (see its docstring/tests) - these
# thresholds are denominated in ITS output scale, not ground-truth channel
# SNR, on purpose.
#
# The mode 0/r1_4_k9 floor and the mode 1/r1_2_k3 step are grounded in
# fmlink_fec.py's real empirical sweep data (0dB true SNR -> r1_2_k3 98%
# good; -6dB true -> only r1_4_k9 is reliable), calibrated against THIS
# estimator by directly measuring what it reports on ota_sim.OTALink at
# those same declared --base-snr-db values (8 trials each, tight spread):
#   declared -6dB (r1_2_k3 known BAD)  -> blind estimate ~2.8-3.1dB
#   declared  0dB (r1_2_k3 known GOOD) -> blind estimate ~6.6-7.0dB
#   declared  3dB (r1_2_k3 known GOOD) -> blind estimate ~9.2-9.6dB
# 5.0 sits with real margin on both sides of that known-bad/known-good gap.
# An earlier version of this table used a threshold of 3.0 derived from a
# synthetic-AWGN-only guess, not the real channel - live-tested (see
# tasklist.txt) at declared -6dB and found to read ~2.9-3.1, i.e. right at
# that threshold, and demonstrably unreliable there. This is exactly the
# kind of miscalibration the "test it" half of this feature was meant to
# catch.
#
# The QPSK row (9.0, pointing at mode 4 - the fastest QPSK sibling, 4800
# sym/s) is now a real reliability measurement too, not just an estimate
# reading: 15/15 successful round trips (real send_text() + --ack, not
# just "did the estimator read high enough") at EVERY declared
# --base-snr-db tested from 6dB through 18dB, WITH fading enabled (fading
# is on by default - an earlier pass here only tested with it off, which
# is not representative). No threshold change needed for this row.
#
# The 16-QAM row (pointing at mode 5, 9600 sym/s) is a real, and
# materially different, finding: live-tested the same way (real
# send_text()+--ack round trips, fading on) and found genuinely
# UNRELIABLE at 12-15dB true SNR (12/15, 11/15 - the exact conditions a
# user hit live, where --auto picked 16-QAM and a message needed several
# retries before the sender gave up despite the data having actually
# arrived - see tasklist.txt), fully reliable (15/15) only from 18dB true
# SNR up. Mapping that through estimate_snr_db() - WITH fading on this
# time, unlike the mode 0/1 calibration above - surfaced a harder
# problem: fading makes the control probe's own single reading noisy
# enough that the "unreliable at 15dB" and "reliable at 18dB" ranges of
# ITS estimate overlap substantially (measured: 14.7-23.8 at declared
# 15dB vs 17.6-26.8 at declared 18dB, 10 trials each) - a single BPSK
# probe just doesn't predict a 16-QAM burst's own independent fade draw
# precisely. No threshold here can be both safe AND commonly reachable;
# 24.0 is deliberately conservative (favors avoiding another false
# "reliable" pick over recovering this mode's throughput often) - most of
# the measured 15dB-unreliable readings fall below it, so 16-QAM will now
# mostly only get picked on genuinely excellent links (roughly 21dB+ true
# SNR). Fixing this properly would mean negotiate() averaging multiple
# probes instead of trusting one - noted as a follow-up, not done here.
#
# Mode 1 is preferred over its same-modulation, lower-symbol-rate sibling
# (0), and mode 4 over ITS slower QPSK siblings (2, 3), whenever
# reachable - this simulated baseband chain doesn't model any reliability
# cost from a higher symbol rate, only real decode reliability
# (modulation + FEC), so there's no reason to ever pick a slower
# same-family mode over a faster one at the same threshold, UNLESS a
# max_symbol_rate ceiling rules the faster one out (a real-world
# narrowband deployment that genuinely can't use a faster symbol rate,
# even though this simulator would happily allow it - see
# select_mode_for_snr()'s max_symbol_rate param). That's exactly why the
# QPSK family (2/3/4) gets one row EACH at the same 9.0 threshold below,
# not just one row for the fastest (4): select_mode_for_snr() keeps the
# LAST row whose threshold is satisfied, so with no ceiling that's still
# mode 4 (last in table order) - but a ceiling that excludes mode 4's row
# (symbol_rate too high) leaves mode 3's or mode 2's row still standing,
# rather than falling through to nothing reachable at this reliability
# tier at all. A first version of this table had only one QPSK row and a
# narrow ceiling would have made --auto skip QPSK entirely, straight from
# BPSK to 16-QAM - caught by test_max_symbol_rate_caps_selection_even_
# at_high_snr expecting the best mode UNDER the ceiling, not just any
# mode reachable overall.
MODE_SNR_TABLE = (
    (-100.0, 0, 'r1_4_k9'),
    (5.0, 1, 'r1_2_k3'),
    (9.0, 2, 'r1_2_k3'),
    (9.0, 3, 'r1_2_k3'),
    (9.0, 4, 'r1_2_k3'),
    (24.0, 5, 'r1_2_k3'),
)


# Mode selection for a FADING link (ota_station.py --phy audio detects
# fading per burst - see fmlink_audio.burst_quality()). MODE_SNR_TABLE
# assumes noise spread evenly through a burst; mobile fading on an FM link
# instead concentrates it into short dropouts below the FM threshold, many
# per burst, and r1_2_k3 can't repair those error bursts: measured through
# ota_sim's FM model (mic port), mode 2/r1_2_k3 delivered 6% at 15 dB CNR
# and 80 km/h - where MODE_SNR_TABLE picks it - while mode 0/r1_4_k9
# delivered 100%. A stronger K=7 code holds up far better under fading at
# the same rate, so this table uses r1_2_k7 throughout.
#
# Thresholds are the burst's AVERAGE data-aided SNR over the whole probe
# (not the marker-only estimate MODE_SNR_TABLE uses), measured WITH
# fmlink_fec's default golden interleaving - which is what makes fast
# modes usable on a fading link at all (it turns each fade's error burst
# into scattered errors). Study: 16 bursts per point at 5/30/80 km/h,
# 440 MHz, 8-20 dB CNR; each row is the lowest average SNR at which its
# mode delivered >= 94% at both 30 and 80 km/h (mode 1: ~7.4 dB, mode 2:
# ~10 dB; at ~7.5 dB mode 2 managed 81-94%). Slow fading (5 km/h) fades
# last ~100 ms and still take out whole stretches of a burst - per-burst
# delivery there is lower (about 50% across 8-20 dB) and ARQ retries carry
# it, but this table still beat a slow-fading-safe one on throughput at
# every speed tested. Stops at mode 2: faster modes weren't measured under
# fading.
FADING_MODE_SNR_TABLE = (
    (-100.0, 0, 'r1_4_k9'),
    (7.0, 1, 'r1_2_k7'),
    (9.5, 2, 'r1_2_k7'),
)


def select_mode_for_snr(snr_db, max_symbol_rate=None, table=MODE_SNR_TABLE, probe_symbol_rate=None):
    """Estimated SNR (see MODE_SNR_TABLE's docstring for exactly what this
    should be) -> (mode_id, fec_preset): the highest-throughput mode this
    link can apparently support. Always returns something, even for an SNR
    below every threshold (falls back to the most robust row).

    probe_symbol_rate, if given, is the symbol rate of the burst snr_db was
    measured on. The table's thresholds are SNRs at the candidate mode's OWN
    symbol rate, but on a real audio channel noise fills a fixed bandwidth,
    so a faster mode sees proportionally more of it: each row is judged on
    snr_db - 10*log10(row_rate / probe_symbol_rate). Measured over the audio
    path: mode 1 reads ~3.1 dB below a mode 0 probe, mode 2 ~2.5 dB below.
    None (the default) compares snr_db directly, which is right for the
    simulator's IQ path, where SNR doesn't depend on symbol rate.

    max_symbol_rate, if given, excludes any row whose mode's symbol_rate
    exceeds it before picking - for a real deployment that physically
    can't use a given symbol rate (e.g. a narrowband/audio-bandwidth
    channel), not something this simulator's own channel model has any
    concept of on its own. None (the default) considers every row,
    unchanged from before this parameter existed. table defaults to
    MODE_SNR_TABLE; pass FADING_MODE_SNR_TABLE for a link detected as
    fading."""
    rows = table
    if max_symbol_rate is not None:
        rows = tuple(row for row in rows if WAVEFORM_MODES[row[1]]['symbol_rate'] <= max_symbol_rate)
        if not rows:
            rows = (table[0],)  # even the floor row's symbol_rate must fit any sane ceiling
    best = rows[0]
    for row in rows:
        row_snr = snr_db
        if probe_symbol_rate:
            row_snr -= 10 * math.log10(WAVEFORM_MODES[row[1]]['symbol_rate'] / probe_symbol_rate)
        if row[0] <= row_snr:
            best = row
    return best[1], best[2]


BROADCAST_ID = 'ALL'


def format_mode(mode_id):
    """Waveform Mode ID -> short display string, e.g. 'Mode 2 (2400 sym/s, qpsk)'."""
    info = WAVEFORM_MODES.get(mode_id)
    if info is None:
        return f'Mode {mode_id} (unknown)'
    return f'Mode {mode_id} ({info["symbol_rate"]} sym/s, {info["modulation"]})'


def decode_station_id(id_bytes):
    """Decode a Source/Destination ID as parse_header() returns it (the
    unpacked callsign's ASCII bytes) to a str, trailing 0x00 stripped. Never raises on malformed input
    (replaces undecodable bytes) - a CRC-verified frame's src/dst field is
    still only as trustworthy as whatever the sender actually put there, and
    every TX chain so far sends an all-zero placeholder (decodes to '')."""
    return id_bytes.rstrip(b'\x00').decode('utf-8', errors='replace')


def is_for_station(dst_id_bytes, station_id):
    """True if a received frame's Destination ID should be accepted by a
    receiver identifying as station_id.

    station_id='' means "not configured to filter" - accepts every frame.
    This is also the only sane default today since no TX chain populates a
    real Destination ID yet (see this module's docstring): they all send the
    same all-zero placeholder, which decodes to ''.

    When station_id IS set, accepts an exact match or either spelling of
    broadcast: the literal 'ALL' callsign from FMlink_Specification.txt's
    example, or an empty/placeholder dst_id (so today's TX chains keep being
    heard by an addressed receiver instead of silently going unheard just
    because they don't populate a real destination yet)."""
    if not station_id:
        return True
    dst = decode_station_id(dst_id_bytes)
    return dst in ('', BROADCAST_ID, station_id)


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
                                   seq=seq, flags=flags, fragment_index=i, **header_kwargs))
    return frames, (seq_start + len(chunks)) % SEQ_MODULUS


class FragmentReassembler:
    """Reassembles a run of consecutively-sequenced Fragmented frames back into
    the original payload. Feed it every CRC-verified (header, payload) pair
    where parse_flags(header['flags'])['fragmented'] is True - non-fragmented
    frames are already complete on their own and shouldn't go through this.

    Uses header['fragment_index'] as the authoritative "is this really the
    start of a new run" signal (fragment_index == 0), rather than inferring
    it from "nothing else is in progress" the way an earlier version of this
    class did. That inference was silently wrong whenever a run got
    abandoned mid-stream (e.g. a MAX_FRAGMENTS overflow): the very next
    leftover continuation fragment (fragment_index != 0) would get
    misread as a fresh start, and if the remaining leftover fragments
    happened to run to completion on their own, a receiver could get
    silently delivered a truncated TAIL of the oversized original message,
    with a normal 'ok' status and no indication anything was ever wrong.

    Also tracks the expected Source ID across a run (not just Sequence
    Number/fragment_index), so two different stations' fragmented messages
    can't accidentally interleave into one garbled reassembly just because
    their sequence numbers happened to line up.
    """

    def __init__(self, max_fragments=MAX_FRAGMENTS):
        self.max_fragments = max_fragments
        self.abort_count = 0
        self.overflow_count = 0
        self.discarded_count = 0
        self.reset()

    def reset(self):
        self._chunks = []
        self._expected_seq = None
        self._expected_index = None
        self._src_id = None

    @property
    def in_progress(self):
        return self._expected_seq is not None

    def feed(self, header, payload):
        """Returns (status, result):
          ('waiting', None)    - buffered, still expecting more fragments
          ('complete', bytes)  - final fragment arrived; full reassembled payload
          ('overflow', None)   - exceeded max_fragments without completing;
                                  run abandoned defensively
          ('discarded', None)  - a continuation fragment (fragment_index != 0)
                                  arrived that doesn't match any in-progress
                                  run (wrong/no run, wrong seq, wrong index, or
                                  wrong source) - ignored rather than
                                  misinterpreted as a new run's start. If a run
                                  WAS in progress when this happened, that run
                                  is also aborted (see abort_count) since a
                                  mismatch means data was already lost.

        fragment_index == 0 is trusted unconditionally as a new run's start,
        aborting any stale in-progress run first (counted under
        abort_count - a running total, not a per-call flag).
        """
        seq = header['seq']
        frag_index = header['fragment_index']
        src_id = header['src_id']
        more = parse_flags(header['flags'])['more_fragments']

        if frag_index == 0:
            if self.in_progress:
                self.abort_count += 1
            self._chunks = [payload]
            self._expected_seq = (seq + 1) % SEQ_MODULUS
            self._expected_index = 1
            self._src_id = src_id
        else:
            matches = (self.in_progress and seq == self._expected_seq and
                       frag_index == self._expected_index and src_id == self._src_id)
            if not matches:
                if self.in_progress:
                    self.abort_count += 1
                self.reset()
                self.discarded_count += 1
                return 'discarded', None
            self._chunks.append(payload)
            self._expected_seq = (seq + 1) % SEQ_MODULUS
            self._expected_index += 1

        if len(self._chunks) > self.max_fragments:
            self.reset()
            self.overflow_count += 1
            return 'overflow', None

        if not more:
            result = b''.join(self._chunks)
            self.reset()
            return 'complete', result

        return 'waiting', None


class SelectiveReassembler:
    """Reassembles a batched message (BURST_BATCHING_PROPOSAL.md), whose
    segments can arrive in any order: a batch resends only the segments the
    receiver reported missing. Segments belong together when they share a
    source and a first sequence number (seq - fragment_index); a message is
    delivered only when every index 0..last is present, last being the one
    without the More Fragments flag - so, like FragmentReassembler, it can
    never deliver a truncated message. One message is assembled at a time
    (a sender doesn't start the next until this one is acknowledged): a
    segment of a different message abandons the current one, except a late
    resend of the message just completed, which is a duplicate.

    status(src_id, first_seq) gives what a selective ACK reports.
    """

    def __init__(self, max_fragments=MAX_FRAGMENTS):
        self.max_fragments = max_fragments
        self.abort_count = 0
        self.overflow_count = 0
        self.discarded_count = 0
        self.duplicate_count = 0
        self._done = None        # (key, count) of the message last completed
        self._abandoned = None   # key of a message given up on (overflow)
        self.reset()

    def reset(self):
        self._key = None
        self._chunks = {}
        self._last = None

    @property
    def in_progress(self):
        return self._key is not None

    def feed(self, header, payload):
        """Returns (status, result): ('waiting', None), ('complete', bytes),
        ('duplicate', None) for a segment already held or of the message
        already delivered, ('overflow', None) and ('discarded', None) as in
        FragmentReassembler."""
        index = header['fragment_index']
        key = (header['src_id'], (header['seq'] - index) % SEQ_MODULUS)
        if self._done is not None and key == self._done[0]:
            self.duplicate_count += 1
            return 'duplicate', None
        if key == self._abandoned:
            self.discarded_count += 1
            return 'discarded', None
        if index >= self.max_fragments:
            if self._key == key:
                self.reset()
            self._abandoned = key
            self.overflow_count += 1
            return 'overflow', None
        if key != self._key:
            if self.in_progress:
                self.abort_count += 1
            self.reset()
            self._key = key
        if index in self._chunks:
            self.duplicate_count += 1
            return 'duplicate', None
        self._chunks[index] = payload
        if not parse_flags(header['flags'])['more_fragments']:
            self._last = index
        if self._last is not None and len(self._chunks) == self._last + 1:
            result = b''.join(self._chunks[i] for i in range(self._last + 1))
            self._done = (key, self._last + 1)
            self.reset()
            return 'complete', result
        return 'waiting', None

    def status(self, src_id, first_seq):
        """(count, set of fragment indices held) for a selective ACK of the
        message from src_id starting at first_seq, or None if unknown."""
        key = (src_id, first_seq % SEQ_MODULUS)
        if self._done is not None and key == self._done[0]:
            return self._done[1], set(range(self._done[1]))
        if key == self._key and self._chunks:
            count = self._last + 1 if self._last is not None else max(self._chunks) + 1
            return count, set(self._chunks)
        return None
