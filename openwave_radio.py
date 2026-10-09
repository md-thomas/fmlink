"""Real-radio transport for ota_station.py: the same interface as ota_sim's
SimClient (send() a burst, a callback receives each incoming burst), but
over a radio's audio interface instead of a TCP relay.

Receiving: a continuous capture feeds a BurstDetector, which looks for the
station's current candidate bursts (modulation / symbol rate / FEC) in a
rolling buffer, decodes them to find where each ends, and hands the
station that burst's audio - the same PCM16 bytes the simulator delivers.
Transmitting: send() queues a burst; a worker keys PTT, plays it, releases.
Radios are half duplex, so receiving is paused while transmitting.

Audio in/out and PTT are pluggable (capture / player), so SimulatedAir can
connect two stations in one process for testing without radios.
"""
import os
import queue
import subprocess
import tempfile
import threading
import time
import wave

import numpy as np

import openwave_audio as audio
import openwave_fec as fec
import openwave_link as link
import openwave_modem as modem
import openwave_sync as sync
import ptt_play
from demodulate import DEMODULATORS
from modulate import MODULATORS

RATE = audio.AUDIO_SAMPLE_RATE
BITS_PER_SYMBOL = {'bpsk': 1, 'qpsk': 2, '16qam': 4}
CHUNK_S = 0.25        # audio read at a time
STEP_S = 0.5          # how often the buffer is searched for a burst
KEEP_S = 2.5          # audio kept while nothing has been heard
LEAD_S = 0.5          # audio kept ahead of a burst's marker when handing it over
TAIL_S = 0.04         # ... and after its end: enough for the filters' ringing, short of
                      # the next burst when several share a transmission (50 ms apart)
GUARD_S = 0.05        # audio left out ahead of a marker when looking for an earlier burst
MIN_QUALITY = 0.15    # below this, "no burst" (see openwave_audio.demodulate)


def decode_burst(x, modulation, symbol_rate, use_fec, generators, k, carrier_hz):
    """Audio -> dict: quality, snr_db, timing (sample index of the burst's
    marker, or None), status ('ok', a decode-failure status, 'no_frame' or
    'no_burst'), payload and header of the first good frame, rms and peak."""
    out = {'rms': float(x.std()) if len(x) else 0.0, 'peak': float(np.abs(x).max()) if len(x) else 0.0,
           'quality': 0.0, 'snr_db': float('-inf'), 'timing': None, 'status': 'no_burst',
           'payload': None, 'header': None}
    if len(x) < RATE // 4:
        return out
    try:
        symbols, info = audio.demodulate(
            x, symbol_rate, carrier_hz, MODULATORS[modulation](link.AUDIO_MARKER),
            constellation=modem._CONSTELLATIONS[modulation])
        out['quality'], out['snr_db'], out['timing'] = float(info['quality']), float(info['snr_db']), info['timing']
        if info['quality'] < MIN_QUALITY:
            return out
        bps = BITS_PER_SYMBOL[modulation]
        symbols = symbols[:len(symbols) - len(symbols) % (8 // bps)]
        bits = DEMODULATORS[modulation](symbols)
        frames = list(fec.decode_frames(bits, generators, k, first_marker_bit=info['marker_index'] * bps,
                                        marker=link.AUDIO_MARKER)
                      if use_fec else sync.iter_frames_correlated(bits, marker=link.AUDIO_MARKER))
    except Exception:
        out['status'] = 'error'
        return out
    out['status'] = 'no_frame'
    for status, header, payload in frames:
        if out['status'] == 'no_frame':
            out['status'] = status
        if status == 'ok':
            out.update(status='ok', payload=payload, header=header)
            break
    return out


def burst_air_samples(modulation, symbol_rate, use_fec, generators, k, payload_len):
    """On-air length, in samples, of a frame with this payload length."""
    frame = audio.frame_for_audio(link.build_frame(bytes(payload_len)))
    encoded = fec.encode_frame(frame, generators, k, marker=link.AUDIO_MARKER) if use_fec else frame
    return len(MODULATORS[modulation](encoded)) * audio.samples_per_symbol(symbol_rate)


class BurstDetector:
    """Finds and delimits bursts in a stream of audio. attempts_fn() returns
    the (modulation, symbol_rate, use_fec, generators, k) candidates to try
    (re-read every step: a station's mode changes during a session). on_burst
    (segment, result) gets each burst's audio, including a little silence
    around it."""

    def __init__(self, attempts_fn, carrier_hz, on_burst, threshold=0.4, max_burst_s=8.0):
        # carrier_hz: Hz, or a function symbol_rate -> Hz (a port whose
        # carrier depends on the rate - see openwave_audio.carrier_for)
        self.attempts_fn, self.carrier_hz, self.on_burst = attempts_fn, carrier_hz, on_burst
        self.threshold, self.max_burst = threshold, int(max_burst_s * RATE)
        self.reset()

    def reset(self):
        self.buf, self.base = np.zeros(0), 0   # buffer and its absolute sample index

    def feed(self, x):
        self.buf = np.concatenate([self.buf, x])

    def end(self):
        return self.base + len(self.buf)

    def _trim_to(self, absolute):
        cut = min(max(0, absolute - self.base), len(self.buf))
        self.buf, self.base = self.buf[cut:], self.base + cut

    def _search(self, x):
        """The best burst in x: (result, decoded) or (None, False)."""
        tries = []
        for attempt in self.attempts_fn():
            carrier = self.carrier_hz(attempt[1]) if callable(self.carrier_hz) else self.carrier_hz
            r = decode_burst(x, *attempt, carrier)
            r['attempt'] = attempt
            tries.append(r)
        ok = [r for r in tries if r['status'] == 'ok']
        heard = [r for r in tries if r['quality'] >= self.threshold and r['timing'] is not None]
        if ok:
            return max(ok, key=lambda r: r['quality']), True
        if heard:
            return max(heard, key=lambda r: r['quality']), False
        return None, False

    def step(self):
        """Hand over every complete burst in the buffer, in order."""
        while len(self.buf) > RATE:
            r, decoded = self._search(self.buf)
            # The search finds the strongest marker, not the first: with two
            # bursts back to back in the buffer it can pick the second, and
            # trimming past it would drop the first. So decode the audio
            # before it too, until there's no earlier burst that decodes.
            while r is not None:
                before, before_decoded = self._search(self.buf[:max(0, r['timing'] - int(GUARD_S * RATE))])
                if not before_decoded:
                    break
                r, decoded = before, True
            if r is None:
                self._trim_to(self.end() - int(KEEP_S * RATE))
                return
            if decoded:
                length = burst_air_samples(*r['attempt'], r['header']['payload_len'])
                self._deliver(r, length)
                # Trim only to the burst's end: another burst may follow
                # within ~50 ms in the same transmission (batching).
                self._trim_to(self.base + r['timing'] + length)
            else:
                if self.end() - (self.base + r['timing']) < self.max_burst:
                    return  # it may still be arriving: wait for more audio
                self._deliver(r, 2 * RATE)   # heard but never decoded: let the station report it
                self._trim_to(self.base + r['timing'] + int(0.5 * RATE))

    def _deliver(self, result, length):
        start = max(0, result['timing'] - int(LEAD_S * RATE))
        segment = self.buf[start:result['timing'] + length + int(TAIL_S * RATE)]
        result['burst_peak'] = float(np.abs(segment).max()) if len(segment) else 0.0
        # How long ago the burst finished: the delivery lags its end by the
        # detection step and decode time.
        result['lag_s'] = max(0, self.end() - (self.base + result['timing'] + length)) / RATE
        self.on_burst(segment, result)


# -- audio in / out ----------------------------------------------------
def arecord_chunks(device, channels=1):
    """Generator of float audio chunks from an ALSA capture device."""
    cmd = ['arecord', '-q', '-D', device, '-f', 'S16_LE', '-r', str(RATE), '-c', str(channels), '-t', 'raw']
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    size = int(RATE * CHUNK_S) * channels * 2
    try:
        while True:
            data = proc.stdout.read(size)
            if len(data) < size:
                raise RuntimeError(f"recording stopped: "
                                   f"{proc.stderr.read().decode(errors='replace').strip() or 'no data'}")
            x = np.frombuffer(data, '<i2').astype(float) / 32768
            yield x.reshape(-1, channels).mean(axis=1) if channels > 1 else x
    finally:
        proc.terminate()


def make_player(station):
    """player(pcm16 bytes): key the station's PTT, play, release."""
    def play(pcm):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'tx.wav')
            with wave.open(path, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
                w.writeframes(pcm)
            ptt_play.key_and_play(path, station['tx_device'], station['ptt_port'], station['ptt_line'],
                                  station.get('key_delay', 0.2))
    return play


class RadioTransport:
    """SimClient look-alike over a radio. on_burst(pcm16 bytes) is called on
    loop's thread for each incoming burst."""

    # Allowance, per direction, for what the simulator doesn't have: PTT
    # key-up and tail, the receive mute, detector step and decode time, plus
    # any turnaround beyond the default. ota_station adds it to its reply
    # timeouts.
    DEFAULT_TURNAROUND_S = 0.6

    @property
    def overhead_s(self):
        return 2.0 + max(0.0, self.turnaround_s - self.DEFAULT_TURNAROUND_S)

    def __init__(self, on_burst, attempts_fn, carrier_hz, capture, player, loop, tx_level=0.5,
                 log=print, settle_s=0.4, turnaround_s=DEFAULT_TURNAROUND_S, trace=None):
        self._on_burst, self._loop, self._player = on_burst, loop, player
        self._capture, self._log = capture, log
        self._trace = trace or (lambda msg: None)   # detail for a log file, not the terminal
        self.tx_level, self.settle_s, self.turnaround_s = tx_level, settle_s, turnaround_s
        self._last_rx_end = 0.0   # when the last received burst finished airing (monotonic)
        self._detector = BurstDetector(attempts_fn, carrier_hz, self._burst)
        self._tx_queue = queue.Queue()
        self._stop = threading.Event()
        self._transmitting = False
        self._mute_until = 0.0
        self._threads = []
        self.connected = False

    async def connect(self):
        self.connected = True
        for target in (self._rx_loop, self._tx_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def send(self, pcm):
        self._tx_queue.put(pcm)

    def close(self):
        self.connected = False
        self._stop.set()
        self._tx_queue.put(None)

    def _burst(self, segment, result):
        self._last_rx_end = time.monotonic() - result['lag_s']
        mod, rate = result['attempt'][0], result['attempt'][1]
        self._trace(f"[radio rx] burst {mod} {rate} baud: {result['status']}, "
                    f"SNR {result['snr_db']:.1f} dB, sync {result['quality']:.2f}, "
                    f"peak {result['burst_peak']:.2f}, {len(segment) / RATE:.2f} s of audio")
        pcm = audio.audio_to_pcm16(segment)
        self._loop.call_soon_threadsafe(self._on_burst, pcm)

    def _rx_loop(self):
        since_step = 0.0
        try:
            for chunk in self._capture():
                if self._stop.is_set():
                    return
                if self._transmitting or time.monotonic() < self._mute_until:
                    self._detector.reset()   # our own transmission isn't something to decode
                    since_step = 0.0
                    continue
                self._detector.feed(chunk)
                since_step += len(chunk) / RATE
                if since_step >= STEP_S:
                    since_step = 0.0
                    self._detector.step()
        except Exception as exc:
            self.connected = False
            self._log(f"[radio rx error] {exc!r}")

    def _tx_loop(self):
        while not self._stop.is_set():
            pcm = self._tx_queue.get()
            if pcm is None:
                return
            x = audio.pcm16_to_audio(pcm)
            peak = np.abs(x).max()
            if peak > 0:
                pcm = audio.audio_to_pcm16(x * (self.tx_level / peak))
            # Turnaround: the far station has just finished transmitting and
            # is briefly deaf (PTT release, its receive mute) - don't key up
            # on top of that. Some radios need longer: a TIDRADIO HT sends a
            # carrier without its mic audio for 2-3 s after receiving
            # (--turnaround-ms).
            time.sleep(min(max(2.0, self.turnaround_s),
                           max(0.0, self._last_rx_end + self.turnaround_s - time.monotonic())))
            self._transmitting = True
            started = time.monotonic()
            self._trace(f"[radio tx] keying, {len(pcm) / 2 / RATE:.2f} s of audio at peak "
                        f"{self.tx_level:g}")
            try:
                self._player(pcm)
                self._trace(f"[radio tx] released after {time.monotonic() - started:.2f} s")
            except Exception as exc:
                self._log(f"[radio tx error] {exc!r} - burst NOT sent")
            finally:
                self._mute_until = time.monotonic() + self.settle_s
                self._transmitting = False


class SimulatedAir:
    """Connects radios in one process, in real time, for testing without
    hardware: each port hears the other ports' transmissions over a little
    noise. Use port().capture and port().player with RadioTransport."""

    def __init__(self, noise_rms=0.02, seed=0):
        self.noise_rms, self.rng = noise_rms, np.random.default_rng(seed)
        self.ports = []

    def port(self):
        p = _AirPort(self)
        self.ports.append(p)
        return p


class _AirPort:
    def __init__(self, air):
        self.air, self.pending, self.lock = air, np.zeros(0), threading.Lock()

    def capture(self):
        n = int(RATE * CHUNK_S)
        next_t = time.monotonic()
        while True:
            next_t += CHUNK_S
            time.sleep(max(0.0, next_t - time.monotonic()))
            with self.lock:
                heard, self.pending = self.pending[:n], self.pending[n:]
            chunk = self.air.rng.normal(0, self.air.noise_rms, n)
            chunk[:len(heard)] += heard
            yield chunk

    def player(self, pcm):
        x = audio.pcm16_to_audio(pcm)
        time.sleep(0.2)   # PTT key-up before any audio goes out
        for other in self.air.ports:
            if other is not self:
                with other.lock:
                    other.pending = np.concatenate([other.pending, x])
        time.sleep(len(x) / RATE + 0.1)   # the audio, then PTT release
