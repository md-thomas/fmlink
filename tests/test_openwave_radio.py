"""
Tests for openwave_radio.BurstDetector: finding bursts in a stream of
audio as a radio's receive path hands it over.
"""
import unittest

import numpy as np

import openwave_audio as audio
import openwave_fec as fec
import openwave_link as link
import openwave_radio as radio
from modulate import MODULATORS

RATE = audio.AUDIO_SAMPLE_RATE
GENERATORS, K = (0o171, 0o133), 7
ATTEMPT = ('qpsk', 1200, True, GENERATORS, K)   # mode 2, r1_2_k7


def burst(payload, level):
    frame = audio.frame_for_audio(link.build_frame(payload))
    symbols = MODULATORS['qpsk'](fec.encode_frame(frame, GENERATORS, K, marker=link.AUDIO_MARKER))
    return audio.modulate(symbols, 1200, 1650) * level


def run_detector(x, step_s):
    got = []
    detector = radio.BurstDetector(lambda: [ATTEMPT], 1650, lambda segment, r: got.append(r))
    step = int(step_s * RATE)
    for i in range(0, len(x), step):
        detector.feed(x[i:i + step])
        detector.step()
    return [r['payload'] for r in got if r['status'] == 'ok'], got


class BurstDetectorTests(unittest.TestCase):
    def test_back_to_back_bursts_come_out_in_order(self):
        # Bursts sent back to back (an AX.25 window of I-frames), each a
        # little stronger than the last, all in the buffer at one step: the
        # strongest marker is the last, but the first must not be lost.
        gap = np.zeros(int(0.4 * RATE))
        payloads = [b'A' * 73, b'B' * 45, b'C' * 15]
        x = np.concatenate([gap, burst(payloads[0], 0.3), gap, burst(payloads[1], 0.4),
                            gap, burst(payloads[2], 0.5), np.zeros(3 * RATE)])
        x += np.random.default_rng(1).normal(0, 0.01, len(x))
        for step_s in (radio.STEP_S, 1.0, 3.0, 6.0):
            with self.subTest(step_s=step_s):
                delivered, _ = run_detector(x, step_s)
                self.assertEqual(delivered, payloads)

    def test_noise_alone_delivers_nothing(self):
        x = np.random.default_rng(2).normal(0, 0.05, 6 * RATE)
        self.assertEqual(run_detector(x, radio.STEP_S)[1], [])


class TurnaroundTests(unittest.TestCase):
    def test_longer_turnaround_extends_the_reply_allowance(self):
        def make(**kw):
            return radio.RadioTransport(lambda pcm: None, lambda: [ATTEMPT], 1650, None, None, None, **kw)
        default = make()
        self.assertEqual(default.turnaround_s, radio.RadioTransport.DEFAULT_TURNAROUND_S)
        slow = make(turnaround_s=3.0)
        self.assertAlmostEqual(slow.overhead_s - default.overhead_s, 3.0 - default.turnaround_s)


if __name__ == '__main__':
    unittest.main()
