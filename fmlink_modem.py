"""
FMlink PHY-layer modulation: bits <-> symbols for supported schemes.

BPSK, QPSK, and 16-QAM so far (see tasklist.txt Phase 6 for the eventual
multi-mode plan - 8PSK/OFDM still to come). Each scheme is deliberately the
simplest possible mapping for its bits/symbol count, both to validate the
modulate.py/demodulate.py round trip and as a building block before
pulse-shaping/sample-rate concerns enter the picture. This module operates
entirely at the symbol level: one complex value per symbol, no timing/
sample-rate axis - there's no "symbol rate" parameter anywhere here on
purpose. See modulate.py/demodulate.py's docstrings for why that's a
deliberate scope boundary for now (RRC pulse shaping/upsampling to a real
sample rate is a separate future step).

Bit order: MSB-first within each byte (bit 7 down to bit 0), matching GNU
Radio's blocks_unpack_k_bits_bb/blocks_pack_k_bits_bb convention, so this
stays consistent with the existing .grc flowgraphs' TX/RX chains if it's
ever wired in alongside them. Multi-bit-per-symbol schemes (QPSK, 16-QAM)
consume bits in that same order, earliest bit first.

Symbol file format: raw interleaved float32 I/Q (real, imag, real, imag,
...), little-endian - GNU Radio's ".cfile" convention (numpy's complex64
layout). No header, so this reads directly into numpy as
`numpy.fromfile(path, dtype=numpy.complex64)` or GNU Radio's File Source
block if that's ever useful, even though nothing here depends on numpy or
GNU Radio itself.
"""
import math
import struct

BPSK_POINTS = (-1 + 0j, 1 + 0j)  # index = bit value: 0 -> -1, 1 -> +1

# Gray-coded QPSK: I and Q are two independent BPSK axes (same 0->-1, 1->+1
# convention as BPSK_POINTS on each), normalized by 1/sqrt(2) so average
# symbol energy is 1 - the same convention BPSK_POINTS already uses, so
# fmlink_channel.py's SNR-to-noise-voltage conversion (which measures
# signal power from the samples themselves, not a hardcoded assumption -
# see its docstring) treats both schemes comparably at "the same SNR".
# Gray coding matters here specifically because each bit maps to its own
# axis: a single bit error only moves the received point to its nearest
# neighbor along one axis, never diagonally to the far corner - the
# standard reason QPSK is preferred over a non-Gray 4-point mapping.
_QPSK_SCALE = 2 ** -0.5
QPSK_POINTS = {
    (0, 0): complex(-_QPSK_SCALE, -_QPSK_SCALE),
    (0, 1): complex(-_QPSK_SCALE, _QPSK_SCALE),
    (1, 0): complex(_QPSK_SCALE, -_QPSK_SCALE),
    (1, 1): complex(_QPSK_SCALE, _QPSK_SCALE),
}

# Gray-coded 16-QAM: I and Q are two independent 4-level PAM axes (4 bits/
# symbol total, 2 bits/axis), same "independent axes" structure as QPSK's
# 2 bits/symbol - just each axis now carries 2 bits instead of 1. Standard
# Gray-coded level order for 2 bits -> one of {-3,-1,+1,+3}: (0,0)->-3,
# (0,1)->-1, (1,1)->+1, (1,0)->+3 - consecutive levels differ by exactly
# one bit, so (as with QPSK) a small perturbation across the NEAREST
# decision boundary flips only one bit, never two at once.
# Scaled by 1/sqrt(10): mean-square of one axis's 4 levels is
# (9+1+1+9)/4 = 5, so each axis contributes average power 5 - scaling
# both axes by 1/sqrt(10) brings total average symbol energy to 1, the
# same convention BPSK_POINTS/QPSK_POINTS use (see QPSK_POINTS' comment
# for why that consistency matters to fmlink_channel.py's SNR measure).
_PAM4_LEVELS = {(0, 0): -3, (0, 1): -1, (1, 1): 1, (1, 0): 3}
_QAM16_SCALE = 10 ** -0.5
QAM16_POINTS = {
    (bi0, bi1, bq0, bq1): complex(_PAM4_LEVELS[(bi0, bi1)], _PAM4_LEVELS[(bq0, bq1)]) * _QAM16_SCALE
    for bi0 in (0, 1) for bi1 in (0, 1) for bq0 in (0, 1) for bq1 in (0, 1)
}


# Flat point sets for estimate_snr_db()'s brute-force nearest-point search -
# keyed the same way modulate.py's MODULATORS/demodulate.py's DEMODULATORS
# are, by modulation name.
_CONSTELLATIONS = {
    'bpsk': BPSK_POINTS,
    'qpsk': tuple(QPSK_POINTS.values()),
    '16qam': tuple(QAM16_POINTS.values()),
}


def _pam4_slice(value):
    """One 4-PAM axis's received value -> its 2 Gray-coded bits (nearest-
    level hard decision - see QAM16_POINTS). Decision thresholds sit at
    the midpoints between adjacent scaled levels: -2*_QAM16_SCALE, 0,
    +2*_QAM16_SCALE."""
    if value < -2 * _QAM16_SCALE:
        return (0, 0)
    elif value < 0:
        return (0, 1)
    elif value < 2 * _QAM16_SCALE:
        return (1, 1)
    else:
        return (1, 0)


def bytes_to_bits(data):
    """bytes -> list of 0/1 ints, MSB-first per byte."""
    bits = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits


def bits_to_bytes(bits):
    """list of 0/1 ints -> bytes, MSB-first per byte. len(bits) must be a
    multiple of 8 - true for BPSK (1 bit/symbol) fed with whole input bytes,
    but not guaranteed in general for modulations with other bits/symbol
    counts, hence the explicit check rather than silently truncating."""
    if len(bits) % 8 != 0:
        raise ValueError(f'bit count {len(bits)} is not a multiple of 8')
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return bytes(out)


def modulate_bpsk(data):
    """bytes -> list of complex BPSK symbols (1 bit/symbol)."""
    return [BPSK_POINTS[b] for b in bytes_to_bits(data)]


def demodulate_bpsk(symbols):
    """list of complex symbols -> bytes: slice each to the nearest BPSK
    point (decision boundary is the imaginary axis - real part sign alone
    decides the bit), then pack 8 bits/byte. No noise/synchronization
    handling - see demodulate.py's docstring."""
    bits = [1 if s.real >= 0 else 0 for s in symbols]
    return bits_to_bytes(bits)


def modulate_qpsk(data):
    """bytes -> list of complex QPSK symbols (2 bits/symbol, MSB-first bit
    pairs - see QPSK_POINTS). Always an even number of bits for whole-byte
    input, so no padding is needed."""
    bits = bytes_to_bits(data)
    return [QPSK_POINTS[(bits[i], bits[i + 1])] for i in range(0, len(bits), 2)]


def demodulate_qpsk(symbols):
    """list of complex symbols -> bytes: each axis is an independent hard
    decision (real part sign -> first bit, imag part sign -> second bit,
    matching QPSK_POINTS/modulate_qpsk()'s per-axis Gray coding), then pack
    8 bits/byte. len(symbols) must be a multiple of 4 (2 bits/symbol into
    whole bytes) - true for symbols from modulate_qpsk() fed whole bytes.
    No noise/synchronization handling - see demodulate.py's docstring."""
    bits = []
    for s in symbols:
        bits.append(1 if s.real >= 0 else 0)
        bits.append(1 if s.imag >= 0 else 0)
    return bits_to_bytes(bits)


def modulate_qam16(data):
    """bytes -> list of complex 16-QAM symbols (4 bits/symbol, MSB-first
    bit quadruples - see QAM16_POINTS). Always a multiple of 4 bits for
    whole-byte input, so no padding is needed."""
    bits = bytes_to_bits(data)
    return [QAM16_POINTS[tuple(bits[i:i + 4])] for i in range(0, len(bits), 4)]


def demodulate_qam16(symbols):
    """list of complex symbols -> bytes: each axis is independently
    hard-decision sliced to its nearest of 4 levels (see _pam4_slice()/
    QAM16_POINTS's Gray coding), then pack 8 bits/byte. len(symbols) must
    be a multiple of 2 (4 bits/symbol into whole bytes) - true for symbols
    from modulate_qam16() fed whole bytes. No noise/synchronization
    handling - see demodulate.py's docstring."""
    bits = []
    for s in symbols:
        bits.extend(_pam4_slice(s.real))
        bits.extend(_pam4_slice(s.imag))
    return bits_to_bytes(bits)


def estimate_snr_db(symbols, modulation, floor_db=-30.0):
    """Blind EVM-based SNR estimate for a received symbol stream: normalizes
    average received symbol energy to match the constellation's unit-energy
    convention (see BPSK_POINTS/QPSK_POINTS/QAM16_POINTS), then measures
    each symbol's squared distance to its OWN nearest ideal point (the same
    hard decision demodulate_bpsk/qpsk/qam16 already make, just kept as a
    distance instead of only a bit) as a noise-power estimate.

    This is blind - no known pilot/preamble symbols involved, just the
    received data itself - so it's a coarse heuristic, not a calibrated
    measurement: "nearest own point" is biased optimistic at very low true
    SNR, since a symbol that's drifted past a decision boundary is, by
    definition, closest to the WRONG point, which this can't tell apart
    from genuinely being close to the right one. Good enough to drive mode-
    selection thresholds (see fmlink_link.py's MODE_SNR_TABLE); floor_db
    caps the returned value when measured noise is ~0 (e.g. synthetic
    noiseless symbols in a test), avoiding a math domain error/+inf.
    """
    points = _CONSTELLATIONS[modulation]
    n = len(symbols)
    if n == 0:
        return floor_db
    mean_energy = sum(abs(s) ** 2 for s in symbols) / n
    if mean_energy <= 0:
        return floor_db
    scale = mean_energy ** -0.5  # normalizes average received energy to 1
    noise_power = sum(min(abs(s * scale - p) ** 2 for p in points) for s in symbols) / n
    noise_power = max(noise_power, 10 ** (floor_db / 10))
    return 10 * math.log10(1.0 / noise_power)


def symbols_to_iq_bytes(symbols):
    """complex symbols -> raw interleaved float32 I/Q bytes (see module
    docstring for the file format)."""
    return b''.join(struct.pack('<ff', s.real, s.imag) for s in symbols)


def iq_bytes_to_symbols(data):
    """Inverse of symbols_to_iq_bytes()."""
    if len(data) % 8 != 0:
        raise ValueError(f'IQ byte count {len(data)} is not a multiple of 8 (4 bytes I + 4 bytes Q)')
    return [complex(re, im) for re, im in struct.iter_unpack('<ff', data)]
