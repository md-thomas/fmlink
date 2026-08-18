"""
Unit tests for openwave_modem.py, the PHY-layer bits<->symbols mapping.

Pure Python, no GNU Radio dependency - fast, run first.

    python3 -m unittest tests.test_openwave_modem -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import openwave_modem as modem
import openwave_link as link


class BitPackingTests(unittest.TestCase):
    def test_bytes_to_bits_msb_first(self):
        self.assertEqual(modem.bytes_to_bits(b'\xA5'), [1, 0, 1, 0, 0, 1, 0, 1])

    def test_bytes_to_bits_multiple_bytes(self):
        self.assertEqual(modem.bytes_to_bits(b'\x00\xFF'), [0] * 8 + [1] * 8)

    def test_bits_to_bytes_round_trip(self):
        data = b'hello world'
        self.assertEqual(modem.bits_to_bytes(modem.bytes_to_bits(data)), data)

    def test_bits_to_bytes_rejects_non_multiple_of_8(self):
        with self.assertRaises(ValueError):
            modem.bits_to_bytes([0, 1, 1])

    def test_empty_round_trips(self):
        self.assertEqual(modem.bits_to_bytes(modem.bytes_to_bits(b'')), b'')


class BpskTests(unittest.TestCase):
    def test_known_mapping(self):
        symbols = modem.modulate_bpsk(b'\x80')  # 1000_0000
        self.assertEqual(symbols[0], 1 + 0j)
        self.assertEqual(symbols[1], -1 + 0j)
        self.assertTrue(all(s == -1 + 0j for s in symbols[2:]))

    def test_one_symbol_per_bit(self):
        data = b'\x00\xFF\xA5'
        symbols = modem.modulate_bpsk(data)
        self.assertEqual(len(symbols), len(data) * 8)

    def test_round_trip(self):
        data = b'The quick brown fox 0123456789'
        symbols = modem.modulate_bpsk(data)
        recovered = modem.demodulate_bpsk(symbols)
        self.assertEqual(recovered, data)

    def test_round_trip_on_real_frame_bytes(self):
        frame = link.build_frame(b'hello', seq=1, mode_id=2, src_id=b'K0MDT', dst_id=b'W1AW')
        symbols = modem.modulate_bpsk(frame)
        recovered = modem.demodulate_bpsk(symbols)
        self.assertEqual(recovered, frame)

    def test_round_trip_empty(self):
        self.assertEqual(modem.demodulate_bpsk(modem.modulate_bpsk(b'')), b'')

    def test_demodulate_slices_toward_nearest_point_under_perturbation(self):
        # small real-axis perturbations shouldn't cross the decision boundary;
        # bits 1,0,1,0,0,0,0,0 -> 0xA0
        symbols = [1 + 0.1j, -1 - 0.1j, 0.9 + 0j, -0.9 + 0j,
                   -1 + 0.2j, -0.95 - 0.3j, -1.1 + 0j, -0.8 - 0.1j]
        self.assertEqual(modem.demodulate_bpsk(symbols), b'\xA0')


class IqSerializationTests(unittest.TestCase):
    def test_round_trip(self):
        symbols = [1 + 0j, -1 + 0j, 0.5 - 0.5j]
        data = modem.symbols_to_iq_bytes(symbols)
        self.assertEqual(len(data), len(symbols) * 8)
        recovered = modem.iq_bytes_to_symbols(data)
        for original, back in zip(symbols, recovered):
            self.assertAlmostEqual(original.real, back.real, places=5)
            self.assertAlmostEqual(original.imag, back.imag, places=5)

    def test_empty_round_trips(self):
        self.assertEqual(modem.iq_bytes_to_symbols(modem.symbols_to_iq_bytes([])), [])

    def test_rejects_length_not_multiple_of_8(self):
        with self.assertRaises(ValueError):
            modem.iq_bytes_to_symbols(b'\x00' * 7)


class FullPipelineTests(unittest.TestCase):
    """bytes -> symbols -> IQ bytes -> symbols -> bytes, exactly what
    modulate.py + demodulate.py do as two separate processes over a file."""

    def test_bpsk_full_round_trip(self):
        frame = link.build_frame(b'the full pipeline', seq=99, src_id=b'K0MDT', dst_id=b'ALL')
        symbols = modem.modulate_bpsk(frame)
        iq_bytes = modem.symbols_to_iq_bytes(symbols)
        recovered_symbols = modem.iq_bytes_to_symbols(iq_bytes)
        recovered = modem.demodulate_bpsk(recovered_symbols)
        self.assertEqual(recovered, frame)


if __name__ == '__main__':
    unittest.main()
