#!/usr/bin/env python3
"""Audio frequency response of a radio link, station to station.

One station transmits a tone sweep (100 Hz - 8 kHz, logarithmic); the other
records its radio's audio, and the recording is analysed here: level at
each frequency relative to the strongest, the SNR against the quiet carrier
before the sweep, and what that means for each mode's band. Stations are
picked like radio_link_test.py's (auto-detected here and on the hosts in
stations.toml; steps run over ssh when a station is elsewhere).

  ./radio_response.py --tx aioc --rx digirig@lenovo
  ./radio_response.py                # exactly two detected: tx = first
  ./radio_response.py --swap         # ... and the other way round

THIS TRANSMITS for about 7 s. It asks first (--yes skips that). Low power,
clear frequency, open squelch on the receiver.

Measured 2026-10-07 (AIOC + TIDRADIO HT, Digirig + Baofeng, mic/speaker
jacks): flat ~350-2300 Hz, -5 to -13 dB at 3 kHz, nothing above ~4 kHz.
See RADIO_SETUP.md "Frequency response and faster modes".
"""
import argparse
import math
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

import audio_wav_test as awt
import openwave_audio as audio
import openwave_link as link
import radio_link_test as rlt
import radio_stations as stations_mod

RATE = audio.AUDIO_SAMPLE_RATE
F0, F1 = 100.0, 8000.0
LEAD_S, TAIL_S = 0.6, 0.2      # silence around the sweep; the lead is the noise reference
WINDOW_S = 0.04                # analysis window per frequency point


def sweep_phase(duration):
    t = np.arange(int(duration * RATE)) / RATE
    k = np.log(F1 / F0)
    return 2 * np.pi * F0 * duration / k * (np.exp(t / duration * k) - 1)


def sweep(duration, level):
    s = np.sin(sweep_phase(duration))
    ramp = int(0.01 * RATE)
    s[:ramp] *= np.linspace(0, 1, ramp)
    s[-ramp:] *= np.linspace(1, 0, ramp)
    return level * s


def sweep_signal(duration, level):
    """What is transmitted: lead silence, the sweep, tail silence."""
    return np.concatenate([np.zeros(int(LEAD_S * RATE)), sweep(duration, level),
                           np.zeros(int(TAIL_S * RATE))])


def analyze(x, duration, points=40):
    """Recording -> dict: start (s), peak, and rows of (Hz, dB re strongest,
    SNR dB). The sweep is found by correlation; each point is measured by
    following the sweep's own phase across the window."""
    ph = sweep_phase(duration)
    s = np.sin(ph)
    n = 1 << int(np.ceil(np.log2(len(x) + len(s))))
    corr = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(s, n)), n)[:len(x)]
    start = int(np.argmax(np.abs(corr)))
    noise = x[max(0, start - int((LEAD_S - 0.15) * RATE)):max(0, start - int(0.05 * RATE))]
    win = int(WINDOW_S * RATE)
    noise_segs = [noise[j:j + win] for j in range(0, len(noise) - win, win // 2)]
    rows = []
    for f in np.geomspace(150, 7500, points):
        ti = int(duration * np.log(f / F0) / np.log(F1 / F0) * RATE) - win // 2
        seg = x[start + ti:start + ti + win]
        if len(seg) < win:
            continue  # the recording ended early
        ref = np.exp(-1j * ph[ti:ti + win])
        sig = np.abs(seg @ ref) ** 2
        nz = np.mean([np.abs(ns @ ref) ** 2 for ns in noise_segs]) if noise_segs else 0.0
        rows.append((f, sig, nz))
    peak = max(r[1] for r in rows) if rows else 1.0
    return {'start_s': start / RATE, 'peak': float(np.abs(x).max()) if len(x) else 0.0,
            'rows': [(f, 10 * np.log10(max(sig, 1e-30) / peak),
                      10 * np.log10(max(sig, 1e-30) / nz) if nz > 0 else float('inf'))
                     for f, sig, nz in rows]}


def level_at(rows, f):
    """Response (dB re strongest) at f, interpolated on a log-frequency axis."""
    fs = np.log([r[0] for r in rows])
    return float(np.interp(np.log(f), fs, [r[1] for r in rows]))


def report(result, port):
    rows = result['rows']
    print(f"\nsweep found {result['start_s']:.2f} s into the recording; recording peak "
          f"{result['peak']:.2f}" + ("  (CLIPPED - lower the capture gain or volume)"
                                     if result['peak'] >= 0.99 else ''))
    print(f"\n{'Hz':>6}  {'dB':>6}  {'SNR':>5}")
    for f, rel, snr in rows:
        bar = '#' * max(0, int(rel + 40))
        print(f"{f:6.0f}  {rel:6.1f}  {snr:5.1f}  {bar}")
    for drop in (3, 6, 10):
        ok = [f for f, rel, _ in rows if rel >= -drop]
        if ok:
            print(f"within {drop:2d} dB of the strongest: {min(ok):.0f} - {max(ok):.0f} Hz")
    print(f"\nEach mode's band on the {port} port (carrier +- symbol_rate * (1 + alpha) / 2), "
          f"and the response at its edges:")
    profile = audio.PORT_PROFILES[port]
    for mode_id, m in link.WAVEFORM_MODES.items():
        rate = m['symbol_rate']
        carrier = audio.carrier_for(port, rate)
        half = rate * (1 + audio.ALPHA) / 2
        lo, hi = carrier - half, carrier + half
        allowed = rate <= profile['max_symbol_rate']
        if lo <= 0:
            print(f"  mode {mode_id} ({m['modulation']} {rate} baud): needs ~{2 * half:.0f} Hz of audio "
                  f"- wider than any carrier on this port can place")
            continue
        print(f"  mode {mode_id} ({m['modulation']} {rate} baud): {lo:.0f}-{hi:.0f} Hz on {carrier:g} Hz, "
              f"edges {level_at(rows, lo):.0f} / {level_at(rows, hi):.0f} dB"
              + ('' if allowed else f"  (not allowed on the {port} port)"))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--tx', help="transmitting station, e.g. aioc or digirig@lenovo; omit both to auto-pick")
    p.add_argument('--rx', help='receiving station')
    p.add_argument('--swap', action='store_true', help='with auto-pick: reverse the direction')
    p.add_argument('--stations', default=stations_mod.DEFAULT_CONFIG, help='station config file')
    p.add_argument('--port', choices=sorted(audio.PORT_PROFILES), default='mic',
                   help="port profile for the per-mode summary (default mic)")
    p.add_argument('--level', type=float, default=0.3,
                   help='sweep level, 0-1 (default 0.3: below the radio\'s deviation limiter)')
    p.add_argument('--duration', type=float, default=6.0, help='sweep length, s (default 6)')
    p.add_argument('--ptt-line', choices=('dtr', 'rts'), help="override the tx station's PTT line")
    p.add_argument('--key-delay', type=float, default=0.3, help='seconds between PTT and audio')
    p.add_argument('--keep', metavar='DIR', help='keep the TX/RX WAVs in DIR')
    p.add_argument('--yes', action='store_true', help='do not ask before transmitting')
    args = p.parse_args()

    try:
        tx, rx = rlt.pick_stations(stations_mod.find_stations(args.stations), args.tx, args.rx, args.swap)
    except ValueError as exc:
        p.error(str(exc))
    tx = dict(tx)
    if args.ptt_line:
        tx['ptt_line'] = args.ptt_line
    if not tx['ptt_port']:
        p.error(f"{stations_mod.label(tx)} has no PTT serial port detected, so it can't transmit")

    signal = sweep_signal(args.duration, args.level)
    airtime = len(signal) / RATE
    print(f"About to TRANSMIT a {args.duration:g} s tone sweep ({F0:g}-{F1:g} Hz, level {args.level:g}).")
    print(f"  from {stations_mod.label(tx)}: PTT {tx['ptt_line'].upper()} on {tx['ptt_port']}, "
          f"audio {tx['tx_device']}")
    print(f"  to   {stations_mod.label(rx)}: recording {rx['rx_device']}")
    print("Low power, clear frequency, receiver's squelch open?")
    if not args.yes:
        try:
            answer = input("Type 'yes' to transmit: ").strip().lower()
        except EOFError:
            answer = ''
        if answer != 'yes':
            print("not transmitting")
            return 1

    with tempfile.TemporaryDirectory() as tmp:
        workdir = args.keep or tmp
        os.makedirs(workdir, exist_ok=True)
        tx_wav, rx_wav = os.path.join(workdir, 'tx_sweep.wav'), os.path.join(workdir, 'rx_sweep.wav')
        rlt.write_wav(tx_wav, signal)
        pre, post = 2.0, rlt.default_post(tx)
        record_s = math.ceil(pre + args.key_delay + airtime + post)
        rec_path = rx_wav if rlt.is_local(rx) else f'/tmp/radio_response_{os.getpid()}_rx.wav'
        rec = rlt.start_recorder(rx, record_s, rec_path)
        try:
            time.sleep(pre)
            if rec.poll() is not None:
                raise RuntimeError(f"recorder exited early: {rec.stderr.read().strip()}")
            rlt.transmit(tx, tx_wav, args.key_delay)
            rec.wait(timeout=record_s + 30)
        except BaseException:
            rec.terminate()
            raise
        if rec.returncode != 0:
            print(f"error: recorder failed: {rec.stderr.read().strip()}", file=sys.stderr)
            return 2
        rlt.copy(rx, rec_path, rx_wav, False)
        if not rlt.is_local(rx):
            subprocess.run(rlt.remote_cmd(rx, ['rm', '-f', rec_path]), check=False, timeout=30,
                           stdin=subprocess.DEVNULL)
        result = analyze(awt.read_wav(rx_wav), args.duration)
    if not result['rows']:
        print("error: the sweep wasn't found in the recording", file=sys.stderr)
        return 2
    report(result, args.port)
    return 0


if __name__ == '__main__':
    sys.exit(main())
