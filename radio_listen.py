#!/usr/bin/env python3
"""Listen on this computer's radio interface and decode OpenWave bursts as
they arrive. Run it on the receiving station; run radio_send.py on the
transmitting one. The two stations need no connection to each other.

  ./radio_listen.py                  # the radio interface attached here
  ./radio_listen.py digirig -m 0 1   # name it / limit the modes tried
  ./radio_listen.py --wav rx.wav     # replay a recording instead

Prints one line per burst: its mode, SNR, sync quality, the loudest sample
in it (1.00 = clipping) and the message. Ctrl-C stops and prints a summary.
Open the radio's squelch and set its volume first (./level_meter.py).
"""
import argparse
import subprocess
import sys
import time
import wave

import numpy as np

import audio_wav_test as awt
import openwave_audio as audio
import openwave_fec as fec
import openwave_link as link
import radio_stations as stations_mod
from modulate import MODULATORS

RATE = audio.AUDIO_SAMPLE_RATE
CHUNK_S = 0.25    # audio read at a time
STEP_S = 1.0      # how often the buffer is searched for a burst
KEEP_S = 3.0      # audio kept while nothing has been heard (longer than a burst's marker)
HEARTBEAT_S = 15.0


def chunks_from_arecord(device, channels):
    cmd = ['arecord', '-q', '-D', device, '-f', 'S16_LE', '-r', str(RATE), '-c', str(channels), '-t', 'raw']
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    size = int(RATE * CHUNK_S) * channels * 2
    try:
        while True:
            data = proc.stdout.read(size)
            if len(data) < size:
                raise SystemExit(f"recording stopped: {proc.stderr.read().decode(errors='replace').strip() or 'no data'}")
            x = np.frombuffer(data, '<i2').astype(float) / 32768
            yield x.reshape(-1, channels).mean(axis=1) if channels > 1 else x
    finally:
        proc.terminate()


def chunks_from_wav(path):
    x = awt.read_wav(path)
    size = int(RATE * CHUNK_S)
    for i in range(0, len(x), size):
        yield x[i:i + size]
    yield np.zeros(int(RATE * 12))  # let a burst at the very end finish waiting


def burst_samples(mode, fec_preset, port, payload_len):
    """On-air length, in samples, of a frame with this payload length."""
    mod, rate, gens, k, _ = awt.mode_params(mode, fec_preset, port)
    frame = audio.frame_for_audio(link.build_frame(bytes(payload_len), mode_id=mode))
    return (len(MODULATORS[mod](fec.encode_frame(frame, gens, k, marker=link.AUDIO_MARKER)))
            * audio.samples_per_symbol(rate))


class Listener:
    def __init__(self, modes, port, fec_preset, threshold, max_burst_s):
        self.modes, self.port, self.fec = modes, port, fec_preset
        self.threshold, self.max_burst = threshold, int(max_burst_s * RATE)
        self.buf, self.base = np.zeros(0), 0   # buffer and its absolute sample index
        self.results = []

    def feed(self, x):
        self.buf = np.concatenate([self.buf, x])

    def end(self):
        return self.base + len(self.buf)

    def trim_to(self, absolute):
        cut = min(max(0, absolute - self.base), len(self.buf))
        self.buf, self.base = self.buf[cut:], self.base + cut

    def step(self):
        """Decode every complete burst in the buffer."""
        while len(self.buf) > RATE:
            tries = [dict(awt.decode_samples(self.buf, m, self.fec, self.port), mode=m) for m in self.modes]
            ok = [r for r in tries if r['status'] == 'ok']
            heard = [r for r in tries if r['quality'] >= self.threshold and r['timing'] is not None]
            if ok:
                r = max(ok, key=lambda r: r['quality'])
                length = burst_samples(r['mode'], self.fec, self.port, r['header']['payload_len'])
                self.emit(r, length)
                self.trim_to(self.base + r['timing'] + length + int(0.2 * RATE))
            elif heard:
                r = max(heard, key=lambda r: r['quality'])
                if self.end() - (self.base + r['timing']) < self.max_burst:
                    return  # the burst may still be arriving: wait for more audio
                self.emit(r, int(2 * RATE))
                self.trim_to(self.base + r['timing'] + int(0.5 * RATE))
            else:
                self.trim_to(self.end() - int(KEEP_S * RATE))
                return

    def emit(self, r, length):
        seg = self.buf[max(0, r['timing'] - int(0.1 * RATE)):r['timing'] + length]
        r['burst_peak'] = float(np.abs(seg).max()) if len(seg) else 0.0
        r['at_s'] = (self.base + r['timing']) / RATE
        self.results.append(r)
        m = link.WAVEFORM_MODES[r['mode']]
        if r['status'] == 'ok':
            src = r['header']['src_id'].rstrip(b'\x00').decode(errors='replace')
            text = r['payload'].decode(errors='replace')
            print(f"[{r['at_s']:7.1f} s] mode {r['mode']} ({m['modulation']} {m['symbol_rate']}) "
                  f"SNR {r['snr_db']:5.1f} dB  sync {r['quality']:.2f}  peak {r['burst_peak']:.2f}  "
                  f"from {src or '?'}: {text}", flush=True)
        else:
            print(f"[{r['at_s']:7.1f} s] mode {r['mode']}? heard a burst but could not decode it "
                  f"({r['status']}): SNR {r['snr_db']:.1f} dB  sync {r['quality']:.2f}  "
                  f"peak {r['burst_peak']:.2f}", flush=True)


def summary(results):
    ok = [r for r in results if r['status'] == 'ok']
    print(f"\n{len(ok)} decoded, {len(results) - len(ok)} heard but not decoded")
    for mode in sorted({r['mode'] for r in ok}):
        rs = [r for r in ok if r['mode'] == mode]
        print(f"  mode {mode}: {len(rs)} decoded, mean SNR {sum(r['snr_db'] for r in rs) / len(rs):.1f} dB")
    if any(r['burst_peak'] >= 0.99 for r in results):
        print("  note: some bursts reached full scale - lower the radio volume (./level_meter.py)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('station', nargs='?', help='interface on this computer (default: the only one)')
    p.add_argument('-m', '--modes', type=int, nargs='+', choices=sorted(link.WAVEFORM_MODES),
                   help='modes to try (default: all that fit the port)')
    p.add_argument('--port', choices=sorted(audio.PORT_PROFILES), default='mic')
    p.add_argument('--fec', choices=sorted(fec.PRESETS), help="default: each mode's own preset")
    p.add_argument('--threshold', type=float, default=0.4, help='sync quality that counts as a burst')
    p.add_argument('--max-burst', type=float, default=6.0,
                   help='seconds to wait for a burst to finish before giving up on it')
    p.add_argument('--wav', help='replay this 48 kHz recording instead of listening')
    p.add_argument('--channels', type=int, choices=(1, 2), help="default: the station's, else 1")
    p.add_argument('--stations', default=stations_mod.DEFAULT_CONFIG)
    args = p.parse_args()

    cap = audio.PORT_PROFILES[args.port]['max_symbol_rate']
    modes = args.modes or [m for m, v in sorted(link.WAVEFORM_MODES.items()) if v['symbol_rate'] <= cap]
    listener = Listener(modes, args.port, args.fec, args.threshold, args.max_burst)

    if args.wav:
        source, where = chunks_from_wav(args.wav), f"replaying {args.wav}"
    else:
        try:
            st = stations_mod.pick_local(args.station, args.stations)
        except ValueError as exc:
            p.error(str(exc))
        source = chunks_from_arecord(st['rx_device'], args.channels or st['rx_channels'])
        where = f"{st['name']} ({st['rx_device']})"
    print(f"listening on {where}, modes {modes}, {args.port} port. Ctrl-C to stop.", flush=True)

    last_beat, since_step = time.time(), 0.0
    try:
        for chunk in source:
            listener.feed(chunk)
            since_step += len(chunk) / RATE
            if since_step >= STEP_S:
                since_step = 0.0
                listener.step()
            if not args.wav and time.time() - last_beat > HEARTBEAT_S:
                last_beat = time.time()
                recent = listener.buf[-RATE:]
                if len(recent):
                    print(f"  ... listening (input rms {20 * np.log10(max(recent.std(), 1e-6)):.0f} dBFS)",
                          flush=True)
        listener.step()
    except KeyboardInterrupt:
        pass
    summary(listener.results)


if __name__ == '__main__':
    main()
