"""
Tests for openwave_audio.py: the audio-subcarrier PHY (symbols <-> real
audio for a radio's audio port). The FM-channel tests at the bottom need
ota_sim installed (pip install -e ../ota_sim) and are skipped otherwise.
"""
import unittest

import numpy as np

import openwave_audio as audio_phy
import openwave_fec as fec
import openwave_link as link
import openwave_modem as modem
from demodulate import DEMODULATORS
from modulate import MODULATORS

try:
    from ota_sim import FMLink
    OTA_SIM_AVAILABLE = True
except ImportError:
    OTA_SIM_AVAILABLE = False

FS = audio_phy.AUDIO_SAMPLE_RATE
BITS_PER_SYMBOL = {'bpsk': 1, 'qpsk': 2, '16qam': 4}
MARKER = link.PREAMBLE + link.SYNC_WORD


def _rng_bytes(n, seed=0):
    return bytes(np.random.default_rng(seed).integers(0, 256, n, dtype=np.uint8))


def _burst(modulation, payload):
    """Unencoded frame-like byte stream: lead pad, marker, payload, end pad."""
    return b'\x00' * 24 + MARKER + payload + b'\x00' * 24


def _recover(audio, modulation, symbol_rate, carrier_hz, track=True):
    symbols, info = audio_phy.demodulate(
        audio, symbol_rate, carrier_hz, MODULATORS[modulation](MARKER),
        pre_symbols=24 * 8 // BITS_PER_SYMBOL[modulation],
        constellation=modem._CONSTELLATIONS[modulation] if track else None)
    return symbols, info


def _rotate_carrier(audio, radians):
    """Rotate a real passband signal's carrier phase (analytic-signal trick)."""
    spectrum = np.fft.fft(audio)
    h = np.zeros(len(audio))
    h[0] = 1
    h[1:(len(audio) + 1) // 2] = 2
    if len(audio) % 2 == 0:
        h[len(audio) // 2] = 1
    analytic = np.fft.ifft(spectrum * h)
    return np.real(analytic * np.exp(1j * radians))


class SamplesPerSymbolTests(unittest.TestCase):
    def test_every_waveform_mode_divides_48k(self):
        for mode in link.WAVEFORM_MODES.values():
            self.assertEqual(audio_phy.samples_per_symbol(mode['symbol_rate']) * mode['symbol_rate'], FS)

    def test_rejects_non_integer_or_too_fast(self):
        with self.assertRaises(ValueError):
            audio_phy.samples_per_symbol(1100)
        with self.assertRaises(ValueError):
            audio_phy.samples_per_symbol(48000)


class ModulateTests(unittest.TestCase):
    def test_level_lead_in_and_tail(self):
        symbols = MODULATORS['qpsk'](_rng_bytes(50))
        audio = audio_phy.modulate(symbols, 1200, 1650, lead_in_s=0.1, tail_s=0.02, level=0.5)
        self.assertAlmostEqual(np.max(np.abs(audio)), 0.5, places=6)
        self.assertEqual(np.max(np.abs(audio[:int(0.1 * FS)])), 0.0)
        self.assertEqual(np.max(np.abs(audio[-int(0.02 * FS):])), 0.0)

    def test_spectrum_sits_inside_each_ports_passband(self):
        for port, profile in audio_phy.PORT_PROFILES.items():
            with self.subTest(port=port):
                rate = profile['max_symbol_rate']
                fc = audio_phy.carrier_for(port, rate)
                audio = audio_phy.modulate(MODULATORS['qpsk'](_rng_bytes(400)), rate, fc)
                power = np.abs(np.fft.rfft(audio)) ** 2
                freqs = np.fft.rfftfreq(len(audio), 1 / FS)
                half = rate * (1 + audio_phy.ALPHA) / 2
                in_band = power[(freqs >= fc - half) & (freqs <= fc + half)].sum()
                self.assertGreater(in_band / power.sum(), 0.99)


class DemodulateTests(unittest.TestCase):
    def check_round_trip(self, modulation, rate, fc, transform, track=True):
        payload = _rng_bytes(120, seed=rate)
        data = _burst(modulation, payload)
        audio = transform(audio_phy.modulate(MODULATORS[modulation](data), rate, fc))
        symbols, info = _recover(audio, modulation, rate, fc, track=track)
        bps = BITS_PER_SYMBOL[modulation]
        n = len(data) * 8 // bps
        recovered = DEMODULATORS[modulation](symbols[:n])
        return recovered == data, info

    def test_clean_round_trip_every_modulation(self):
        for modulation in ('bpsk', 'qpsk', '16qam'):
            with self.subTest(modulation=modulation):
                ok, info = self.check_round_trip(modulation, 1200, 1650, lambda a: a)
                self.assertTrue(ok)
                self.assertGreater(info['quality'], 0.99)
                self.assertGreater(info['snr_db'], 30)

    def test_recovers_unknown_delay_level_and_inversion(self):
        # Arbitrary delay, 1/3 the level, and a 180-degree carrier flip -
        # BPSK's own ambiguity, resolved by correlating against the marker.
        def channel(audio):
            return -0.33 * np.concatenate([np.zeros(12345), audio])
        for modulation in ('bpsk', 'qpsk', '16qam'):
            with self.subTest(modulation=modulation):
                self.assertTrue(self.check_round_trip(modulation, 1200, 1650, channel)[0])

    def test_recovers_arbitrary_carrier_phase(self):
        for radians in (0.5, np.pi / 2, 2.8):
            with self.subTest(radians=radians):
                ok, _ = self.check_round_trip('qpsk', 1200, 1650,
                                              lambda a, r=radians: _rotate_carrier(a, r))
                self.assertTrue(ok)

    def test_tracks_sound_card_clock_offset(self):
        # 300 ppm over a ~4 s QPSK burst: well past a one-shot estimate's
        # tolerance (a few seconds at ~20 ppm), fine with tracking.
        def drift(audio, ppm=300):
            ratio = 1 + ppm * 1e-6
            n = int(len(audio) * ratio)
            return np.interp(np.arange(n) / ratio, np.arange(len(audio)), audio)

        payload = _rng_bytes(1200, seed=3)
        data = _burst('qpsk', payload)
        audio = drift(audio_phy.modulate(MODULATORS['qpsk'](data), 1200, 1650))
        n = len(data) * 8 // 2
        tracked, _ = _recover(audio, 'qpsk', 1200, 1650, track=True)
        fixed, _ = _recover(audio, 'qpsk', 1200, 1650, track=False)
        self.assertEqual(DEMODULATORS['qpsk'](tracked[:n]), data)
        self.assertNotEqual(DEMODULATORS['qpsk'](fixed[:n]), data)

    def test_data_aided_snr_estimate(self):
        rng = np.random.default_rng(5)
        data = _burst('qpsk', _rng_bytes(100))
        audio = audio_phy.modulate(MODULATORS['qpsk'](data), 1200, 1650)
        sps = audio_phy.samples_per_symbol(1200)
        # Noise in the 1200-baud matched filter's bandwidth: per-sample
        # passband SNR * sps / 2 (real passband) gives per-symbol SNR.
        signal_power = np.mean(audio[audio != 0] ** 2)
        for target_db in (10.0, 20.0):
            noise_power = signal_power * sps / 2 / 10 ** (target_db / 10)
            noisy = audio + rng.normal(0, np.sqrt(noise_power), len(audio))
            _, info = _recover(noisy, 'qpsk', 1200, 1650)
            self.assertAlmostEqual(info['snr_db'], target_db, delta=1.5)

    def test_snr_estimate_ignores_clock_offset(self):
        # A long 600-baud marker under 100 ppm of sound-card drift: its
        # carrier phase rotates ~28 degrees across the marker. Before the
        # marker was de-rotated, that read as noise (~5 dB low through FM).
        rng = np.random.default_rng(8)
        data = _burst('bpsk', _rng_bytes(40))
        clean = audio_phy.modulate(MODULATORS['bpsk'](data), 600, 1650)
        sps = audio_phy.samples_per_symbol(600)
        noise_power = np.mean(clean[clean != 0] ** 2) * sps / 2 / 10 ** (20 / 10)
        ratio = 1 + 100e-6
        drifted = np.interp(np.arange(int(len(clean) * ratio)) / ratio, np.arange(len(clean)), clean)
        estimates = []
        for audio in (clean, drifted):
            noisy = audio + rng.normal(0, np.sqrt(noise_power), len(audio))
            estimates.append(_recover(noisy, 'bpsk', 600, 1650)[1]['snr_db'])
        self.assertAlmostEqual(estimates[0], 20, delta=1.5)
        self.assertAlmostEqual(estimates[1], estimates[0], delta=1.0)

    def test_wrong_symbol_rate_correlates_poorly(self):
        audio = audio_phy.modulate(MODULATORS['bpsk'](_burst('bpsk', _rng_bytes(60))), 1200, 1650)
        _, right = _recover(audio, 'bpsk', 1200, 1650)
        _, wrong = _recover(audio, 'bpsk', 600, 1650)
        self.assertGreater(right['quality'], 0.95)
        self.assertLess(wrong['quality'], 0.5)

    def test_too_short_returns_empty(self):
        symbols, info = _recover(np.zeros(100), 'bpsk', 1200, 1650)
        self.assertEqual(len(symbols), 0)
        self.assertEqual(info['quality'], 0.0)


class MarkerUnderNoiseTests(unittest.TestCase):
    """A burst of noise over part of the marker (a fade): sample-domain
    correlation still finds it, and decoding at that position recovers a
    frame that a hard-bit marker search rejects."""

    def test_correlation_position_rescues_a_faded_marker(self):
        generators, k = fec.PRESETS['r1_2_k7']
        payload = b'faded marker'
        coded = fec.encode_frame(link.build_frame(payload, seq=1), generators, k)
        lead_in = audio_phy.DEFAULT_LEAD_IN_S
        audio = audio_phy.modulate(MODULATORS['bpsk'](coded), 1200, 1650, lead_in_s=lead_in)
        # Noise over the first 40% of the 288-symbol marker (after the
        # 24-byte lead pad) - a ~100 ms fade at 1200 baud.
        sps = audio_phy.samples_per_symbol(1200)
        start = int(lead_in * FS) + 24 * 8 * sps
        span = int(0.4 * 288) * sps
        rng = np.random.default_rng(1)
        audio[start:start + span] = rng.normal(0, 0.5, span)

        symbols, info = _recover(audio, 'bpsk', 1200, 1650)
        data = DEMODULATORS['bpsk'](symbols[:len(symbols) - len(symbols) % 8])
        searched = list(fec.decode_frames(data, generators, k))
        self.assertFalse(any(status == 'ok' for status, _, _ in searched))
        known = list(fec.decode_frames(data, generators, k, first_marker_bit=info['marker_index']))
        self.assertEqual([(s, p) for s, _, p in known][:1], [('ok', payload)])


class EqualizerTests(unittest.TestCase):
    """The receive equalizer, judged on the payload symbols (not the marker
    it was fitted to)."""

    def payload_snr_db(self, channel, equalize, rate=2400, fc=2000.0, seed=0, noise=0.003):
        payload = _rng_bytes(300, seed=seed)
        tx = MODULATORS['qpsk'](_burst('qpsk', payload))
        audio = channel(audio_phy.modulate(tx, rate, fc))
        audio = audio + np.random.default_rng(seed).normal(0, noise, len(audio))
        symbols, info = audio_phy.demodulate(
            audio, rate, fc, MODULATORS['qpsk'](MARKER), pre_symbols=24 * 4,
            constellation=modem._CONSTELLATIONS['qpsk'], equalize=equalize)
        start = info['marker_index'] + len(MARKER) * 4
        got, want = symbols[start:start + len(payload) * 4], tx[24 * 4 + len(MARKER) * 4:][:len(payload) * 4]
        return 10 * np.log10(1 / np.mean(np.abs(got - want) ** 2)), info['snr_db']

    def test_undoes_a_treble_rolloff(self):
        # ~6 dB/octave from 700 Hz, like the Baofeng -> TIDRADIO direction
        # measured on 2026-10-07 (-6 dB at 2 kHz, -13 dB at 3 kHz).
        from scipy import signal as sps
        b, a = sps.butter(1, 700, fs=FS)
        tilt = lambda x: sps.lfilter(b, a, x) * 3  # noqa: E731
        plain, _ = self.payload_snr_db(tilt, equalize=False)
        equalized, reported = self.payload_snr_db(tilt, equalize=True)
        self.assertGreater(equalized, plain + 6)
        self.assertLess(abs(reported - equalized), 3)  # the marker SNR stays honest

    def test_costs_almost_nothing_on_a_flat_channel(self):
        # At a realistic ~25 dB (a 9-tap fit to 64 marker symbols adds a
        # little noise of its own, which only shows on very clean links).
        plain, _ = self.payload_snr_db(lambda x: x, equalize=False, noise=0.03)
        equalized, _ = self.payload_snr_db(lambda x: x, equalize=True, noise=0.03)
        self.assertGreater(equalized, plain - 0.5)


class PcmTests(unittest.TestCase):
    def test_round_trip_and_clipping(self):
        audio = np.array([0.0, 0.5, -0.5, 0.999, -1.0, 1.7])
        back = audio_phy.pcm16_to_audio(audio_phy.audio_to_pcm16(audio))
        np.testing.assert_allclose(back[:5], audio[:5], atol=1e-4)
        self.assertLessEqual(back[5], 1.0)

    def test_odd_length_tolerated(self):
        self.assertEqual(len(audio_phy.pcm16_to_audio(b'\x01\x00\x02')), 1)


class BurstQualityTests(unittest.TestCase):
    def setUp(self):
        self.reference = np.asarray(MODULATORS['bpsk'](_rng_bytes(300)), dtype=complex)  # 2400 symbols
        self.rng = np.random.default_rng(9)

    def noisy(self, snr_db, n=None):
        n = len(self.reference) if n is None else n
        sigma = np.sqrt(10 ** (-snr_db / 10) / 2)
        return sigma * (self.rng.normal(size=n) + 1j * self.rng.normal(size=n))

    def test_steady_noise_is_not_fading(self):
        quality = audio_phy.burst_quality(self.reference + self.noisy(12), self.reference, 1200)
        self.assertAlmostEqual(quality['snr_db'], 12, delta=0.5)
        self.assertFalse(quality['fading'])
        self.assertLess(quality['fade_fraction'], 0.015)

    def test_short_deep_dropouts_are_fading(self):
        # 6-symbol (5 ms at 1200 baud) dropouts every 50 symbols: ~12% of
        # the burst, like fast mobile fades dipping below the FM threshold.
        received = self.reference + self.noisy(20)
        for start in range(0, len(received) - 6, 50):
            received[start:start + 6] += self.noisy(-3, 6)
        quality = audio_phy.burst_quality(received, self.reference, 1200)
        self.assertTrue(quality['fading'])
        self.assertGreater(quality['fade_fraction'], 0.06)

    def test_too_short_to_judge(self):
        quality = audio_phy.burst_quality(self.reference[:4], self.reference[:4], 1200)
        self.assertFalse(quality['fading'])


@unittest.skipUnless(OTA_SIM_AVAILABLE, 'ota_sim not installed (pip install -e ../ota_sim)')
class FMChannelTests(unittest.TestCase):
    """End to end through ota_sim's FM transceiver model, with FEC, the way
    ota_station.py --phy audio uses it."""

    def decode_through_fm(self, port, mode_id, cnr_db, seed=1, **fm_kwargs):
        info = link.WAVEFORM_MODES[mode_id]
        modulation, rate = info['modulation'], info['symbol_rate']
        fc = audio_phy.carrier_for(port, rate)
        generators, k = fec.PRESETS['r1_2_k7']
        payload = b'audio phy over FM ' * 4
        frame = link.build_frame(payload, seq=1, mode_id=mode_id)
        audio = audio_phy.modulate(MODULATORS[modulation](fec.encode_frame(frame, generators, k)),
                                   rate, fc)
        rx, _ = FMLink(port, base_cnr_db=cnr_db, seed=seed, **fm_kwargs).transmit(audio)
        symbols, _ = _recover(rx, modulation, rate, fc)
        bps = BITS_PER_SYMBOL[modulation]
        symbols = symbols[:len(symbols) - len(symbols) % (8 // bps)]
        results = list(fec.decode_frames(DEMODULATORS[modulation](symbols), generators, k))
        return any(status == 'ok' and p == payload for status, _, p in results)

    def test_every_mode_that_fits_each_port_decodes(self):
        for port, profile in audio_phy.PORT_PROFILES.items():
            for mode_id, info in link.WAVEFORM_MODES.items():
                if info['symbol_rate'] > profile['max_symbol_rate']:
                    continue
                with self.subTest(port=port, mode=mode_id):
                    self.assertTrue(self.decode_through_fm(port, mode_id, cnr_db=15))

    def test_survives_clock_offset_through_fm(self):
        self.assertTrue(self.decode_through_fm('mic', 2, cnr_db=20, clock_offset_ppm=100))


if __name__ == '__main__':
    unittest.main()
