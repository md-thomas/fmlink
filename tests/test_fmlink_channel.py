"""
Unit tests for fmlink_channel.py, AWGN channel simulation.

    python3 -m unittest tests.test_fmlink_channel -v
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fmlink_channel as channel
import fmlink_pulse as pulse
import fmlink_modem as modem


class AddAwgnTests(unittest.TestCase):
    def test_zero_noise_voltage_returns_unchanged(self):
        samples = np.array([1 + 0j, -1 + 0j, 0.5 - 0.5j])
        result = channel.add_awgn(samples, noise_voltage=0)
        np.testing.assert_array_equal(result, samples)

    def test_output_same_length(self):
        samples = np.array([1 + 0j] * 100)
        result = channel.add_awgn(samples, noise_voltage=0.1, seed=1)
        self.assertEqual(len(result), len(samples))

    def test_seed_is_reproducible(self):
        samples = np.array([1 + 0j] * 50)
        a = channel.add_awgn(samples, noise_voltage=0.2, seed=42)
        b = channel.add_awgn(samples, noise_voltage=0.2, seed=42)
        np.testing.assert_array_equal(a, b)

    def test_different_seeds_differ(self):
        samples = np.array([1 + 0j] * 50)
        a = channel.add_awgn(samples, noise_voltage=0.2, seed=1)
        b = channel.add_awgn(samples, noise_voltage=0.2, seed=2)
        self.assertFalse(np.array_equal(a, b))

    def test_noise_power_matches_requested_voltage(self):
        # measured noise power (variance of the added noise) should be close
        # to noise_voltage**2 per component, given enough samples
        samples = np.zeros(200000, dtype=complex)
        noisy = channel.add_awgn(samples, noise_voltage=0.3, seed=7)
        measured_std_real = np.std(noisy.real)
        measured_std_imag = np.std(noisy.imag)
        self.assertAlmostEqual(measured_std_real, 0.3, delta=0.01)
        self.assertAlmostEqual(measured_std_imag, 0.3, delta=0.01)


class SnrDbToNoiseVoltageTests(unittest.TestCase):
    def test_achieves_target_snr_empirically(self):
        rng = np.random.default_rng(0)
        symbols = np.array([1 + 0j if b else -1 + 0j for b in rng.integers(0, 2, 5000)])
        target_snr_db = 10.0
        noise_voltage = channel.snr_db_to_noise_voltage(symbols, target_snr_db)
        noisy = channel.add_awgn(symbols, noise_voltage, seed=99)

        signal_power = np.mean(np.abs(symbols) ** 2)
        noise_power = np.mean(np.abs(noisy - symbols) ** 2)
        measured_snr_db = 10 * np.log10(signal_power / noise_power)
        self.assertAlmostEqual(measured_snr_db, target_snr_db, delta=0.5)

    def test_higher_snr_means_less_noise_voltage(self):
        symbols = np.array([1 + 0j, -1 + 0j] * 100)
        low_snr_voltage = channel.snr_db_to_noise_voltage(symbols, snr_db=0)
        high_snr_voltage = channel.snr_db_to_noise_voltage(symbols, snr_db=20)
        self.assertGreater(low_snr_voltage, high_snr_voltage)


class PipelineIntegrationTests(unittest.TestCase):
    """Confirm noise actually degrades the signal in the expected way when
    inserted at its correct point in the pipeline (between pulseshape and
    matched_filter), and that a clean/high-SNR channel still round-trips."""

    def test_no_noise_still_round_trips(self):
        import fmlink_link as link
        frame = link.build_frame(b'no noise here', seq=1)
        symbols = modem.modulate_bpsk(frame)
        shaped = pulse.pulse_shape(symbols)
        noisy = channel.add_awgn(shaped, noise_voltage=0)
        num_symbols = pulse.expected_symbol_count(len(noisy))
        filtered = pulse.matched_filter(noisy)
        offset = pulse.group_delay()
        recovered_symbols = pulse.decimate(filtered, sps=4, offset=offset)[:num_symbols]
        recovered = modem.demodulate_bpsk([complex(s) for s in recovered_symbols])
        self.assertEqual(recovered, frame)

    def test_high_snr_still_round_trips(self):
        import fmlink_link as link
        frame = link.build_frame(b'high snr test message', seq=2)
        symbols = modem.modulate_bpsk(frame)
        shaped = pulse.pulse_shape(symbols)
        noise_voltage = channel.snr_db_to_noise_voltage(shaped, snr_db=30)
        noisy = channel.add_awgn(shaped, noise_voltage, seed=5)
        num_symbols = pulse.expected_symbol_count(len(noisy))
        filtered = pulse.matched_filter(noisy)
        offset = pulse.group_delay()
        recovered_symbols = pulse.decimate(filtered, sps=4, offset=offset)[:num_symbols]
        recovered = modem.demodulate_bpsk([complex(s) for s in recovered_symbols])
        self.assertEqual(recovered, frame)

    def test_very_low_snr_corrupts_the_frame(self):
        # sanity check that noise actually does something - at a low enough
        # SNR the frame should NOT decode identically anymore
        import fmlink_link as link
        frame = link.build_frame(b'this should get corrupted by heavy noise', seq=3)
        symbols = modem.modulate_bpsk(frame)
        shaped = pulse.pulse_shape(symbols)
        noise_voltage = channel.snr_db_to_noise_voltage(shaped, snr_db=-10)
        noisy = channel.add_awgn(shaped, noise_voltage, seed=5)
        num_symbols = pulse.expected_symbol_count(len(noisy))
        filtered = pulse.matched_filter(noisy)
        offset = pulse.group_delay()
        recovered_symbols = pulse.decimate(filtered, sps=4, offset=offset)[:num_symbols]
        recovered = modem.demodulate_bpsk([complex(s) for s in recovered_symbols])
        self.assertNotEqual(recovered, frame)


if __name__ == '__main__':
    unittest.main()
