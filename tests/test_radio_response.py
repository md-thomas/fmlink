"""
Tests for radio_response.py's analysis (no radio needed) and
radio_link_test.py's station picking.
"""
import unittest

import numpy as np
from scipy import signal as sps

import radio_link_test as rlt
import radio_response as rr

RATE = rr.RATE


def recorded(x, delay_s=1.3, noise=0.005, seed=0):
    """x as a receiver would record it: some lead-in, then a little noise."""
    y = np.concatenate([np.zeros(int(delay_s * RATE)), x, np.zeros(RATE)])
    return y + np.random.default_rng(seed).normal(0, noise, len(y))


class AnalyzeTests(unittest.TestCase):
    def test_a_flat_channel_reads_flat(self):
        result = rr.analyze(recorded(0.5 * rr.sweep_signal(6.0, 0.3)), 6.0)
        self.assertAlmostEqual(result['start_s'], 1.3 + rr.LEAD_S, places=2)
        levels = [rel for _, rel, _ in result['rows']]
        self.assertGreater(min(levels), -1.0)
        self.assertGreater(min(snr for _, _, snr in result['rows']), 20)

    def test_a_lowpass_shows_up_where_it_cuts(self):
        b, a = sps.butter(6, 3000, fs=RATE)
        result = rr.analyze(recorded(sps.lfilter(b, a, rr.sweep_signal(6.0, 0.3))), 6.0)
        self.assertGreater(rr.level_at(result['rows'], 1000), -1.0)
        self.assertLess(rr.level_at(result['rows'], 3000), -2.0)
        self.assertLess(rr.level_at(result['rows'], 5000), -20.0)


class PickStationsTests(unittest.TestCase):
    a = {'name': 'aioc', 'host': 'local'}
    d = {'name': 'digirig', 'host': 'lenovo'}

    def test_auto_pick_and_swap(self):
        self.assertEqual(rlt.pick_stations([self.a, self.d], None, None), (self.a, self.d))
        self.assertEqual(rlt.pick_stations([self.a, self.d], None, None, swap=True), (self.d, self.a))

    def test_needs_both_or_neither_and_two_different(self):
        with self.assertRaises(ValueError):
            rlt.pick_stations([self.a, self.d], 'aioc', None)
        with self.assertRaises(ValueError):
            rlt.pick_stations([self.a], None, None)
        with self.assertRaises(ValueError):
            rlt.pick_stations([self.a, self.d], 'aioc', 'aioc')


if __name__ == '__main__':
    unittest.main()
