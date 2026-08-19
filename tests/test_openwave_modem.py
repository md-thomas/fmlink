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


class QpskTests(unittest.TestCase):
    def test_known_mapping(self):
        # 0xD8 = 1101_1000 -> bit pairs (1,1) (0,1) (1,0) (0,0)
        symbols = modem.modulate_qpsk(b'\xD8')
        self.assertEqual(symbols, [
            modem.QPSK_POINTS[(1, 1)],
            modem.QPSK_POINTS[(0, 1)],
            modem.QPSK_POINTS[(1, 0)],
            modem.QPSK_POINTS[(0, 0)],
        ])

    def test_all_four_points_are_gray_coded_unit_energy(self):
        # each point differs from its axis-neighbors by exactly one bit,
        # and average energy across all four points is 1 (same convention
        # as BPSK_POINTS, so channel.py's SNR-vs-signal-power measurement
        # treats both schemes comparably)
        points = modem.QPSK_POINTS
        self.assertEqual(len(set(points.values())), 4)
        energies = [abs(p) ** 2 for p in points.values()]
        for e in energies:
            self.assertAlmostEqual(e, 1.0, places=9)

    def test_two_bits_per_symbol(self):
        data = b'\x00\xFF\xA5'
        symbols = modem.modulate_qpsk(data)
        self.assertEqual(len(symbols), len(data) * 4)

    def test_round_trip(self):
        data = b'The quick brown fox 0123456789'
        symbols = modem.modulate_qpsk(data)
        recovered = modem.demodulate_qpsk(symbols)
        self.assertEqual(recovered, data)

    def test_round_trip_on_real_frame_bytes(self):
        frame = link.build_frame(b'hello', seq=1, mode_id=2, src_id=b'K0MDT', dst_id=b'W1AW')
        symbols = modem.modulate_qpsk(frame)
        recovered = modem.demodulate_qpsk(symbols)
        self.assertEqual(recovered, frame)

    def test_round_trip_empty(self):
        self.assertEqual(modem.demodulate_qpsk(modem.modulate_qpsk(b'')), b'')

    def test_demodulate_slices_toward_nearest_point_under_perturbation(self):
        # small per-axis perturbations shouldn't cross either decision
        # boundary; bit pairs (1,1) (0,0) (0,0) (0,0) -> 0xC0 (1100_0000)
        s = modem.QPSK_POINTS
        symbols = [s[(1, 1)] + (0.1 + 0.1j), s[(0, 0)] + (-0.05 - 0.05j),
                   s[(0, 0)], s[(0, 0)]]
        self.assertEqual(modem.demodulate_qpsk(symbols), b'\xC0')

    def test_single_bit_error_flips_only_one_bit_of_the_pair(self):
        """The whole point of Gray coding: perturbing only the real axis
        past its decision boundary must flip only the first bit of the
        pair, leaving the second bit (imag axis) untouched."""
        clean = modem.QPSK_POINTS[(0, 0)]
        real_axis_flipped = complex(-clean.real, clean.imag)  # push real past 0
        # pad to a full byte (4 symbols) with clean (0,0) symbols - only
        # the first pair should come out different: (0,0) -> (1,0)
        symbols = [real_axis_flipped, clean, clean, clean]
        recovered = modem.demodulate_qpsk(symbols)
        self.assertEqual(recovered, modem.bits_to_bytes([1, 0, 0, 0, 0, 0, 0, 0]))


class Qam16Tests(unittest.TestCase):
    def test_known_mapping(self):
        # 0xD8 = 1101_1000 -> bit quadruples (1,1,0,1) (1,0,0,0)
        symbols = modem.modulate_qam16(b'\xD8')
        self.assertEqual(symbols, [
            modem.QAM16_POINTS[(1, 1, 0, 1)],
            modem.QAM16_POINTS[(1, 0, 0, 0)],
        ])

    def test_all_sixteen_points_are_gray_coded_unit_energy(self):
        points = modem.QAM16_POINTS
        self.assertEqual(len(points), 16)
        self.assertEqual(len(set(points.values())), 16)
        avg_energy = sum(abs(p) ** 2 for p in points.values()) / len(points)
        self.assertAlmostEqual(avg_energy, 1.0, places=9)

    def test_four_bits_per_symbol(self):
        data = b'\x00\xFF\xA5\x11'
        symbols = modem.modulate_qam16(data)
        self.assertEqual(len(symbols), len(data) * 2)

    def test_round_trip(self):
        data = b'The quick brown fox 0123456789'
        symbols = modem.modulate_qam16(data)
        recovered = modem.demodulate_qam16(symbols)
        self.assertEqual(recovered, data)

    def test_round_trip_on_real_frame_bytes(self):
        frame = link.build_frame(b'hello', seq=1, mode_id=2, src_id=b'K0MDT', dst_id=b'W1AW')
        symbols = modem.modulate_qam16(frame)
        recovered = modem.demodulate_qam16(symbols)
        self.assertEqual(recovered, frame)

    def test_round_trip_empty(self):
        self.assertEqual(modem.demodulate_qam16(modem.modulate_qam16(b'')), b'')

    def test_demodulate_slices_toward_nearest_point_under_perturbation(self):
        # small perturbations shouldn't cross the nearest decision boundary;
        # bit quadruples (1,0,1,0) (0,0,0,0) -> 0xA0 (1010_0000)
        s = modem.QAM16_POINTS
        symbols = [s[(1, 0, 1, 0)] + (0.05 + 0.05j), s[(0, 0, 0, 0)] + (-0.05 - 0.05j)]
        self.assertEqual(modem.demodulate_qam16(symbols), b'\xA0')

    def test_perturbation_past_the_outer_level_stays_at_the_outer_level(self):
        # pushing the outermost level (+3) even further out must still
        # decide the same bits - there's no boundary beyond the outer level
        s = modem.QAM16_POINTS[(1, 0, 1, 0)] + (1.0 + 1.0j)
        symbols = [s, modem.QAM16_POINTS[(0, 0, 0, 0)]]
        self.assertEqual(modem.demodulate_qam16(symbols), b'\xA0')

    def test_single_bit_error_flips_only_one_bit_of_the_quadruple(self):
        """Gray coding within one axis: nudging just past the NEAREST
        decision boundary (e.g. level +1 -> +3, bits (1,1) -> (1,0)) must
        flip only one bit, not both."""
        clean = modem.QAM16_POINTS[(0, 0, 1, 1)]  # I=-3, Q=+1
        # push Q from level +1 past the next boundary (2*scale) toward +3
        nudged = complex(clean.real, clean.imag + 3 * modem._QAM16_SCALE)
        symbols = [nudged, modem.QAM16_POINTS[(0, 0, 0, 0)]]
        recovered = modem.demodulate_qam16(symbols)
        self.assertEqual(recovered, modem.bits_to_bytes([0, 0, 1, 0] + [0, 0, 0, 0]))


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

    def test_qpsk_full_round_trip(self):
        frame = link.build_frame(b'the full pipeline', seq=99, src_id=b'K0MDT', dst_id=b'ALL')
        symbols = modem.modulate_qpsk(frame)
        iq_bytes = modem.symbols_to_iq_bytes(symbols)
        recovered_symbols = modem.iq_bytes_to_symbols(iq_bytes)
        recovered = modem.demodulate_qpsk(recovered_symbols)
        self.assertEqual(recovered, frame)

    def test_qam16_full_round_trip(self):
        frame = link.build_frame(b'the full pipeline', seq=99, src_id=b'K0MDT', dst_id=b'ALL')
        symbols = modem.modulate_qam16(frame)
        iq_bytes = modem.symbols_to_iq_bytes(symbols)
        recovered_symbols = modem.iq_bytes_to_symbols(iq_bytes)
        recovered = modem.demodulate_qam16(recovered_symbols)
        self.assertEqual(recovered, frame)


if __name__ == '__main__':
    unittest.main()
