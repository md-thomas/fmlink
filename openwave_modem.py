"""
OpenWave PHY-layer modulation: bits <-> symbols for supported schemes.

Currently BPSK only (see tasklist.txt Phase 6 for the eventual multi-mode
plan) - intentionally the simplest possible mapping, both to validate the
modulate.py/demodulate.py round trip and as the first building block before
pulse-shaping/sample-rate concerns enter the picture. This module operates
entirely at the symbol level: one complex value per symbol, no timing/
sample-rate axis - there's no "symbol rate" parameter anywhere here on
purpose. See modulate.py/demodulate.py's docstrings for why that's a
deliberate scope boundary for now (RRC pulse shaping/upsampling to a real
sample rate is a separate future step).

Bit order: MSB-first within each byte (bit 7 down to bit 0), matching GNU
Radio's blocks_unpack_k_bits_bb/blocks_pack_k_bits_bb convention, so this
stays consistent with the existing .grc flowgraphs' TX/RX chains if it's
ever wired in alongside them.

Symbol file format: raw interleaved float32 I/Q (real, imag, real, imag,
...), little-endian - GNU Radio's ".cfile" convention (numpy's complex64
layout). No header, so this reads directly into numpy as
`numpy.fromfile(path, dtype=numpy.complex64)` or GNU Radio's File Source
block if that's ever useful, even though nothing here depends on numpy or
GNU Radio itself.
"""
import struct

BPSK_POINTS = (-1 + 0j, 1 + 0j)  # index = bit value: 0 -> -1, 1 -> +1


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


def symbols_to_iq_bytes(symbols):
    """complex symbols -> raw interleaved float32 I/Q bytes (see module
    docstring for the file format)."""
    return b''.join(struct.pack('<ff', s.real, s.imag) for s in symbols)


def iq_bytes_to_symbols(data):
    """Inverse of symbols_to_iq_bytes()."""
    if len(data) % 8 != 0:
        raise ValueError(f'IQ byte count {len(data)} is not a multiple of 8 (4 bytes I + 4 bytes Q)')
    return [complex(re, im) for re, im in struct.iter_unpack('<ff', data)]
