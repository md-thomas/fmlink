"""
Unit tests for openwave_pulse.py, RRC pulse shaping (symbols <-> samples).

    python3 -m unittest tests.test_openwave_pulse -v
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import openwave_pulse as pulse
import openwave_modem as modem


class RrcTapsTests(unittest.TestCase):
    def test_odd_length(self):
        taps = pulse.rrc_taps(alpha=0.35, sps=4, span_symbols=11)
        self.assertEqual(len(taps) % 2, 1)
        self.assertEqual(len(taps), 45)  # 11*4=44 -> forced odd -> 45

    def test_forces_odd_when_span_times_sps_is_odd(self):
        taps = pulse.rrc_taps(alpha=0.35, sps=3, span_symbols=11)  # 11*3=33, already odd
        self.assertEqual(len(taps), 33)

    def test_unit_energy(self):
        taps = pulse.rrc_taps(alpha=0.35, sps=4, span_symbols=11)
        self.assertAlmostEqual(np.sum(taps ** 2), 1.0, places=10)

    def test_symmetric_linear_phase(self):
        taps = pulse.rrc_taps(alpha=0.35, sps=4, span_symbols=11)
        np.testing.assert_allclose(taps, taps[::-1], atol=1e-10)

    def test_peak_at_center(self):
        taps = pulse.rrc_taps(alpha=0.35, sps=4, span_symbols=11)
        center = len(taps) // 2
        self.assertEqual(np.argmax(np.abs(taps)), center)


class GroupDelayTests(unittest.TestCase):
    def test_matches_ntaps_minus_one(self):
        taps = pulse.rrc_taps(alpha=0.35, sps=4, span_symbols=11)
        self.assertEqual(pulse.group_delay(alpha=0.35, sps=4, span_symbols=11), len(taps) - 1)


class PulseShapeTests(unittest.TestCase):
    def test_output_length(self):
        symbols = [1 + 0j, -1 + 0j, 1 + 0j]
        sps = 4
        ntaps = len(pulse.rrc_taps(sps=sps))
        shaped = pulse.pulse_shape(symbols, sps=sps)
        self.assertEqual(len(shaped), len(symbols) * sps + ntaps - 1)

    def test_empty_input(self):
        shaped = pulse.pulse_shape([], sps=4)
        self.assertEqual(len(shaped), len(pulse.rrc_taps(sps=4)) - 1)


class DecimateTests(unittest.TestCase):
    def test_picks_every_nth_sample(self):
        samples = np.arange(20)
        self.assertTrue(np.array_equal(pulse.decimate(samples, sps=4, offset=0), [0, 4, 8, 12, 16]))
        self.assertTrue(np.array_equal(pulse.decimate(samples, sps=4, offset=2), [2, 6, 10, 14, 18]))


class ExpectedSymbolCountTests(unittest.TestCase):
    def test_inverts_pulse_shape_length(self):
        symbols = [1 + 0j, -1 + 0j, 1 + 0j, -1 + 0j, 1 + 0j]
        shaped = pulse.pulse_shape(symbols, alpha=0.35, sps=4, span_symbols=11)
        self.assertEqual(
            pulse.expected_symbol_count(len(shaped), alpha=0.35, sps=4, span_symbols=11),
            len(symbols))

    def test_matches_across_several_lengths(self):
        for n in (1, 2, 10, 47, 200):
            symbols = [1 + 0j] * n
            shaped = pulse.pulse_shape(symbols, sps=8)
            self.assertEqual(pulse.expected_symbol_count(len(shaped), sps=8), n)


class RoundTripTests(unittest.TestCase):
    """The property that actually matters: pulse_shape() -> matched_filter()
    -> decimate() at group_delay() recovers the original symbol values -
    this is what makes plot_constellation.py show clean points instead of a
    smeared blob, and what demodulate.py will eventually build on once noise
    is added on top of this."""

    def _round_trip(self, symbols, alpha=0.35, sps=4, span_symbols=11):
        shaped = pulse.pulse_shape(symbols, alpha, sps, span_symbols)
        num_symbols = pulse.expected_symbol_count(len(shaped), alpha, sps, span_symbols)
        filtered = pulse.matched_filter(shaped, alpha, sps, span_symbols)
        offset = pulse.group_delay(alpha, sps, span_symbols)
        decimated = pulse.decimate(filtered, sps, offset)[:num_symbols]
        return decimated

    def test_bpsk_symbols_recovered(self):
        symbols = [1 + 0j, -1 + 0j, -1 + 0j, 1 + 0j, 1 + 0j, -1 + 0j]
        recovered = self._round_trip(symbols)
        for original, back in zip(symbols, recovered):
            self.assertAlmostEqual(original.real, back.real, delta=0.01)
            self.assertAlmostEqual(original.imag, back.imag, delta=0.01)

    def test_first_and_last_symbols_recovered_too(self):
        # full convolution shouldn't lose/corrupt edge symbols - a truncated
        # (finite-span) RRC filter has small but real residual error vs. an
        # ideal infinite one, hence delta rather than exact equality
        symbols = [1 + 0j] + [-1 + 0j] * 8 + [1 + 0j]
        recovered = self._round_trip(symbols)
        self.assertAlmostEqual(recovered[0].real, 1.0, delta=0.01)
        self.assertAlmostEqual(recovered[-1].real, 1.0, delta=0.01)

    def test_real_frame_bytes_survive_full_pipeline(self):
        import openwave_link as link
        frame = link.build_frame(b'pulse shaping test', seq=3, src_id=b'K0MDT', dst_id=b'W1AW')
        symbols = modem.modulate_bpsk(frame)
        recovered_symbols = self._round_trip(symbols)
        recovered_bytes = modem.demodulate_bpsk([complex(s) for s in recovered_symbols])
        self.assertEqual(recovered_bytes, frame)

    def test_different_sps_still_round_trips(self):
        symbols = [1 + 0j, -1 + 0j, 1 + 0j, 1 + 0j]
        recovered = self._round_trip(symbols, sps=8)
        for original, back in zip(symbols, recovered):
            self.assertAlmostEqual(original.real, back.real, delta=0.01)


if __name__ == '__main__':
    unittest.main()
