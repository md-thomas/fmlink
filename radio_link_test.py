#!/usr/bin/env python3
"""Over-the-air burst test between two radios, in one command.

This machine transmits: it keys PTT over a serial line (an AIOC or Digirig)
and plays a burst through that interface's sound card. A receiver records
the other radio's audio - on a remote host over ssh, or on this machine -
and each recording is decoded. Prints a pass/fail and SNR per burst.

  ./radio_link_test.py --src K0MDT -m 0 1 2 --count 3 --rx-host macbookair2

THIS TRANSMITS. It asks for confirmation before the first burst (--yes
skips that). Set the transmitting radio to a clear frequency and low power.
Press Ctrl-C to stop; PTT is always released.

Receiver setup that matters: automatic gain control OFF (a CM108's AGC
pumps and clips), capture gain low, the radio's squelch open and its volume
moderate so the recording doesn't clip. The summary warns when it does.
"""
import argparse
import math
import os
import subprocess
import sys
import tempfile
import time
import wave

import audio_wav_test as awt
import openwave_audio as audio
import openwave_link as link


def write_wav(path, samples):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(audio.AUDIO_SAMPLE_RATE)
        w.writeframes(audio.audio_to_pcm16(samples))


def transmit(args, wav_path):
    """Key PTT, play the WAV, release PTT (always, even on error/Ctrl-C)."""
    import serial
    ptt = serial.Serial(args.ptt_port, 9600, dsrdtr=False, rtscts=False)
    ptt.dtr = False
    ptt.rts = False
    try:
        setattr(ptt, args.ptt_line, True)
        time.sleep(args.key_delay)
        subprocess.run(['aplay', '-q', '-D', args.tx_device, wav_path], check=True, timeout=60)
        time.sleep(0.1)
    finally:
        ptt.dtr = False
        ptt.rts = False
        ptt.close()


def start_recorder(args, seconds, remote_path):
    cmd = ['arecord', '-q', '-D', args.rx_device, '-f', 'S16_LE', '-r', str(audio.AUDIO_SAMPLE_RATE),
           '-c', str(args.rx_channels), '-d', str(seconds), remote_path]
    if args.rx_host:
        cmd = ['ssh', '-o', 'BatchMode=yes', args.rx_host, ' '.join(cmd)]
    return subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)


def fetch(args, remote_path, local_path):
    if args.rx_host:
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', f'{args.rx_host}:{remote_path}', local_path],
                       check=True, timeout=60)
    else:
        os.replace(remote_path, local_path)


def run_burst(args, mode, index, workdir):
    text = f"{args.src} OpenWave test mode {mode} #{index + 1}"
    samples = awt.build_samples(text, mode, args.fec, args.port, args.src, args.level, args.lead_in)
    airtime = len(samples) / audio.AUDIO_SAMPLE_RATE
    tx_wav = os.path.join(workdir, f'tx_m{mode}_{index}.wav')
    rx_wav = os.path.join(workdir, f'rx_m{mode}_{index}.wav')
    write_wav(tx_wav, samples)

    record_s = math.ceil(args.pre + airtime + args.post)
    remote_path = rx_wav if not args.rx_host else f'/tmp/radio_link_test_{os.getpid()}.wav'
    rec = start_recorder(args, record_s, remote_path)
    result = {'mode': mode, 'index': index, 'airtime': airtime, 'text': text}
    try:
        time.sleep(args.pre)
        if rec.poll() is not None:
            raise RuntimeError(f"recorder exited early: {rec.stderr.read().strip()}")
        transmit(args, tx_wav)
        rec.wait(timeout=record_s + 30)
    except BaseException:
        rec.terminate()
        raise
    if rec.returncode != 0:
        raise RuntimeError(f"recorder failed: {rec.stderr.read().strip()}")
    fetch(args, remote_path, rx_wav)
    r = awt.decode_samples(awt.read_wav(rx_wav), mode, args.fec, args.port)
    result.update(r)
    result['delivered'] = r['payload'] == text.encode()
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
    p.add_argument('-m', '--modes', type=int, nargs='+', default=[0], choices=sorted(link.WAVEFORM_MODES))
    p.add_argument('--count', type=int, default=1, help='bursts per mode (default 1)')
    p.add_argument('--fec', choices=sorted(awt.fec.PRESETS), help="default: each mode's own preset")
    p.add_argument('--port', choices=sorted(audio.PORT_PROFILES), default='mic',
                   help='audio port profile (carrier / max symbol rate); default mic')
    p.add_argument('--level', type=float, default=0.5, help='TX audio level, 0-1 (default 0.5)')
    p.add_argument('--lead-in', type=float, default=0.5, help='silence before the burst, s')
    tx = p.add_argument_group('transmit side')
    tx.add_argument('--tx-device', default='hw:AllInOneCable,0', help='ALSA playback device')
    tx.add_argument('--ptt-port', default='/dev/ttyACM0', help='serial port for PTT')
    tx.add_argument('--ptt-line', choices=('dtr', 'rts'), default='dtr')
    tx.add_argument('--key-delay', type=float, default=0.2, help='seconds between PTT and audio')
    rx = p.add_argument_group('receive side')
    rx.add_argument('--rx-host', help='ssh host that records (omit to record on this machine)')
    rx.add_argument('--rx-device', default='hw:1,0', help='ALSA capture device on the receiver')
    rx.add_argument('--rx-channels', type=int, default=1, choices=(1, 2),
                    help='2 for sound cards that only record stereo')
    rx.add_argument('--pre', type=float, default=3.0, help='record this long before keying, s')
    rx.add_argument('--post', type=float, default=2.0, help='record this long after the burst, s')
    p.add_argument('--gap', type=float, default=2.0, help='pause between bursts, s')
    p.add_argument('--keep', metavar='DIR', help='keep the TX/RX WAVs in DIR (default: discard)')
    p.add_argument('--yes', action='store_true', help='do not ask before transmitting')
    args = p.parse_args()

    total = len(args.modes) * args.count
    print(f"About to TRANSMIT {total} burst(s) as {args.src}: modes {args.modes} x {args.count}, "
          f"PTT {args.ptt_line.upper()} on {args.ptt_port}, audio {args.tx_device}.")
    print(f"Receiver: {args.rx_host or 'this machine'} {args.rx_device}.")
    print("Radio on a clear frequency at low power? Ctrl-C stops (PTT is always released).")
    if not args.yes and input("Type 'yes' to transmit: ").strip().lower() != 'yes':
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
                    r = run_burst(args, mode, i, workdir)
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
