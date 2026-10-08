#!/usr/bin/env python3
"""Over-the-air burst test between any two stations, in either direction.

Stations are radio interfaces (AIOC, Digirig) auto-detected on this
computer and on the hosts in stations.toml (see radio_stations.py). One
station transmits: PTT is keyed over its serial line and a burst is played
through its sound card. The other records its radio's audio, and each
recording is decoded here. Prints a pass/fail and SNR per burst. Steps run
locally or over ssh depending on where each station is.

  ./radio_link_test.py --src K0MDT --tx aioc --rx digirig -m 0 1 2 --count 3
  ./radio_link_test.py --src K0MDT --tx digirig --rx aioc -m 2
  ./radio_link_test.py --src K0MDT -m 2        # exactly two detected: tx = first
  ./radio_link_test.py --src K0MDT -m 2 --swap # ... and the other way round

THIS TRANSMITS. It asks for confirmation before the first burst (--yes
skips that). Set the transmitting radio to a clear frequency and low power.
Press Ctrl-C to stop; PTT is always released.

The summary warns when a recording reaches full scale; see stations.toml
for the receiver setup that avoids it.
"""
import argparse
import math
import os
import shlex
import subprocess
import sys
import tempfile
import time
import wave

import audio_wav_test as awt
import openwave_audio as audio
import openwave_link as link
import ptt_play
import radio_stations as stations_mod

HERE = os.path.dirname(os.path.abspath(__file__))
is_local = lambda st: stations_mod.is_local(st['host'])


def remote_cmd(st, argv):
    """argv to run a command on the station's computer (ssh unless local)."""
    if is_local(st):
        return list(argv)
    return ['ssh', '-o', 'BatchMode=yes', st['host'], shlex.join(argv)]


def copy(st, src, dst, to_station):
    """scp between this machine and the station's (no-op if local)."""
    if is_local(st):
        if os.path.abspath(src) != os.path.abspath(dst):
            subprocess.run(['cp', src, dst], check=True)
        return
    remote = f"{st['host']}:{dst if to_station else src}"
    pair = [src, remote] if to_station else [remote, dst]
    subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', *pair], check=True, timeout=60,
                   stdin=subprocess.DEVNULL)


def write_wav(path, samples):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(audio.AUDIO_SAMPLE_RATE)
        w.writeframes(audio.audio_to_pcm16(samples))


def transmit(tx, wav_path, key_delay):
    """Key the tx station, play the WAV, release (the helper always releases)."""
    if is_local(tx):
        ptt_play.key_and_play(wav_path, tx['tx_device'], tx['ptt_port'], tx['ptt_line'], key_delay)
        return
    tag = f'radio_link_test_{os.getpid()}'
    remote_wav, remote_helper = f'/tmp/{tag}_tx.wav', f'/tmp/{tag}_ptt_play.py'
    copy(tx, wav_path, remote_wav, True)
    copy(tx, os.path.join(HERE, 'ptt_play.py'), remote_helper, True)
    subprocess.run(remote_cmd(tx, ['python3', remote_helper, '--wav', remote_wav,
                                   '--device', tx['tx_device'], '--ptt-port', tx['ptt_port'],
                                   '--ptt-line', tx['ptt_line'], '--key-delay', str(key_delay)]),
                   check=True, timeout=90, stdin=subprocess.DEVNULL)
    subprocess.run(remote_cmd(tx, ['rm', '-f', remote_wav, remote_helper]), check=False, timeout=30, stdin=subprocess.DEVNULL)


def default_post(tx):
    """Seconds to keep recording after the burst. A remote transmitter
    starts late (ssh, copying the WAV, Python start-up), which pushed bursts
    past a 2 s tail on 2026-10-07."""
    return 2.0 if is_local(tx) else 5.0


def pick_stations(detected, tx_name, rx_name, swap=False):
    """(tx, rx) from the detected stations: named, or with neither named
    exactly two detected (swap reverses them). Raises ValueError."""
    if tx_name is None and rx_name is None:
        if len(detected) != 2:
            raise ValueError("auto-pick needs exactly two detected stations, found: "
                             + (', '.join(stations_mod.label(x) for x in detected) or 'none'))
        tx, rx = detected[::-1] if swap else detected
    elif tx_name is None or rx_name is None:
        raise ValueError("give both --tx and --rx, or neither to auto-pick")
    else:
        tx, rx = (stations_mod.resolve(n, detected) for n in (tx_name, rx_name))
    if tx is rx:
        raise ValueError("--tx and --rx must be different stations")
    return tx, rx


def start_recorder(rx, seconds, path):
    cmd = ['arecord', '-q', '-D', rx['rx_device'], '-f', 'S16_LE', '-r', str(audio.AUDIO_SAMPLE_RATE),
           '-c', str(rx['rx_channels']), '-d', str(seconds), path]
    return subprocess.Popen(remote_cmd(rx, cmd), stdin=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def run_burst(args, tx, rx, mode, index, workdir):
    text = f"{args.src} OpenWave test mode {mode} #{index + 1}"
    level = args.level if args.level is not None else tx.get('level', 0.5)
    samples = awt.build_samples(text, mode, args.fec, args.port, args.src, level, args.lead_in)
    airtime = len(samples) / audio.AUDIO_SAMPLE_RATE
    tx_wav = os.path.join(workdir, f'tx_m{mode}_{index}.wav')
    rx_wav = os.path.join(workdir, f'rx_m{mode}_{index}.wav')
    write_wav(tx_wav, samples)

    post = args.post if args.post is not None else default_post(tx)
    record_s = math.ceil(args.pre + airtime + post)
    rec_path = rx_wav if is_local(rx) else f'/tmp/radio_link_test_{os.getpid()}_rx.wav'
    rec = start_recorder(rx, record_s, rec_path)
    result = {'mode': mode, 'index': index, 'airtime': airtime, 'text': text}
    try:
        time.sleep(args.pre)
        if rec.poll() is not None:
            raise RuntimeError(f"recorder exited early: {rec.stderr.read().strip()}")
        transmit(tx, tx_wav, args.key_delay)
        rec.wait(timeout=record_s + 30)
    except BaseException:
        rec.terminate()
        raise
    if rec.returncode != 0:
        raise RuntimeError(f"recorder failed: {rec.stderr.read().strip()}")
    copy(rx, rec_path, rx_wav, False)
    if not is_local(rx):
        subprocess.run(remote_cmd(rx, ['rm', '-f', rec_path]), check=False, timeout=30, stdin=subprocess.DEVNULL)
    result.update(awt.decode_samples(awt.read_wav(rx_wav), mode, args.fec, args.port))
    result['delivered'] = result['payload'] == text.encode()
    return result


def print_row(r):
    snr = f"{r['snr_db']:6.1f}" if r['quality'] >= 0.15 else '     -'
    print(f"  mode {r['mode']}  #{r['index'] + 1}  {'PASS' if r['delivered'] else 'FAIL':4}  "
          f"status {r['status']:<10} SNR {snr} dB  sync {r['quality']:.2f}  "
          f"air {r['airtime']:.2f} s  rec peak {r['peak']:.2f}", flush=True)


def summary(results, modes):
    print("\nsummary:")
    for mode in modes:
        rs = [r for r in results if r['mode'] == mode]
        if not rs:
            continue
        ok = [r for r in rs if r['delivered']]
        snrs = [r['snr_db'] for r in rs if r['quality'] >= 0.15]
        m = link.WAVEFORM_MODES[mode]
        mean = f"{sum(snrs) / len(snrs):.1f} dB" if snrs else '-'
        print(f"  mode {mode} ({m['modulation']} {m['symbol_rate']} baud): "
              f"{len(ok)}/{len(rs)} delivered, mean SNR {mean}")
    if any(r['peak'] >= 0.99 for r in results):
        print("  note: a recording reached full scale. If that was the burst, not just open-squelch "
              "hiss,\n  lower the receiver's capture gain or radio volume - clipping hurts decoding.")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--src', required=True, help='your callsign: goes in each payload and header')
    p.add_argument('--tx', help="transmitting station, e.g. aioc or digirig@macbookair2 "
                                "(see ./radio_stations.py); omit both --tx and --rx to auto-pick")
    p.add_argument('--rx', help='receiving station')
    p.add_argument('--swap', action='store_true', help='with auto-pick: reverse the direction')
    p.add_argument('--stations', default=stations_mod.DEFAULT_CONFIG, help='station config file')
    p.add_argument('-m', '--modes', type=int, nargs='+', default=[0], choices=sorted(link.WAVEFORM_MODES))
    p.add_argument('--count', type=int, default=1, help='bursts per mode (default 1)')
    p.add_argument('--fec', choices=sorted(awt.fec.PRESETS), help="default: each mode's own preset")
    p.add_argument('--port', choices=sorted(audio.PORT_PROFILES), default='mic',
                   help='audio port profile (carrier / max symbol rate); default mic')
    p.add_argument('--level', type=float, help="TX audio level, 0-1 (default: the station's, else 0.5)")
    p.add_argument('--ptt-line', choices=('dtr', 'rts'), help="override the tx station's PTT line")
    p.add_argument('--lead-in', type=float, default=0.5, help='silence before the burst, s')
    p.add_argument('--key-delay', type=float, default=0.2, help='seconds between PTT and audio')
    p.add_argument('--pre', type=float, default=3.0, help='record this long before keying, s')
    p.add_argument('--post', type=float,
                   help='record this long after the burst, s (default 2, or 5 when the '
                        'transmitting station is remote)')
    p.add_argument('--gap', type=float, default=2.0, help='pause between bursts, s')
    p.add_argument('--keep', metavar='DIR', help='keep the TX/RX WAVs in DIR (default: discard)')
    p.add_argument('--yes', action='store_true', help='do not ask before transmitting')
    args = p.parse_args()

    try:
        tx, rx = pick_stations(stations_mod.find_stations(args.stations), args.tx, args.rx, args.swap)
    except ValueError as exc:
        p.error(str(exc))
    tx = dict(tx)
    if args.ptt_line:
        tx['ptt_line'] = args.ptt_line
    if not tx['ptt_port']:
        p.error(f"{stations_mod.label(tx)} has no PTT serial port detected, so it can't transmit")

    total = len(args.modes) * args.count
    print(f"About to TRANSMIT {total} burst(s) as {args.src}: modes {args.modes} x {args.count}.")
    print(f"  from {stations_mod.label(tx)}: PTT {tx['ptt_line'].upper()} on {tx['ptt_port']}, "
          f"audio {tx['tx_device']}")
    print(f"  to   {stations_mod.label(rx)}: recording {rx['rx_device']}")
    print("Transmitting radio on a clear frequency at low power? Ctrl-C stops (PTT is always released).")
    if not args.yes:
        try:
            answer = input("Type 'yes' to transmit: ").strip().lower()
        except EOFError:
            answer = ''
        if answer != 'yes':
            print("not transmitting")
            return 1

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = args.keep or tmp
        if args.keep:
            os.makedirs(workdir, exist_ok=True)
        try:
            for mode in args.modes:
                for i in range(args.count):
                    if results:
                        time.sleep(args.gap)
                    r = run_burst(args, tx, rx, mode, i, workdir)
                    results.append(r)
                    print_row(r)
        except KeyboardInterrupt:
            print("\ninterrupted")
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            print(f"\nerror: {exc}", file=sys.stderr)
    summary(results, args.modes)
    return 0 if results and all(r['delivered'] for r in results) else 2


if __name__ == '__main__':
    sys.exit(main())
