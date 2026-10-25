#!/usr/bin/env python3
"""Transmit OpenWave test bursts from this computer's radio interface. Run it
on the transmitting station; run radio_listen.py on the receiving one. The
two stations need no connection to each other.

  ./radio_send.py --src K0MDT -m 0 1 2 --count 3
  ./radio_send.py digirig --src K0MDT -m 2        # name the interface
  ./radio_send.py --src K0MDT -m 2 --ptt-line dtr # override the PTT line

THIS TRANSMITS. It asks for confirmation first (--yes skips that), waits a
few seconds so you can start the other station's listener, then sends the
bursts with a pause between. Set the radio to a clear frequency and low
power. Ctrl-C stops; PTT is always released.
"""
import argparse
import os
import sys
import tempfile
import time

import audio_wav_test as awt
import openwave_audio as audio
import openwave_link as link
import radio_link_test as rlt
import radio_stations as stations_mod


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('station', nargs='?', help='interface on this computer (default: the only one)')
    p.add_argument('--src', required=True, help='your callsign: goes in each payload and header')
    p.add_argument('-m', '--modes', type=int, nargs='+', default=[0], choices=sorted(link.WAVEFORM_MODES))
    p.add_argument('--count', type=int, default=1, help='bursts per mode (default 1)')
    p.add_argument('--text', help="message to send (default: '<src> OpenWave test mode M #N')")
    p.add_argument('--fec', choices=sorted(awt.fec.PRESETS), help="default: each mode's own preset")
    p.add_argument('--port', choices=sorted(audio.PORT_PROFILES), default='mic',
                   help='audio port profile (carrier / max symbol rate); default mic')
    p.add_argument('--level', type=float, help="TX audio level, 0-1 (default: the station's, else 0.5)")
    p.add_argument('--ptt-line', choices=('dtr', 'rts', 'vox'), help="override the station's PTT line")
    p.add_argument('--lead-in', type=float, default=0.5, help='silence before the burst, s')
    p.add_argument('--key-delay', type=float, default=0.2, help='seconds between PTT and audio')
    p.add_argument('--gap', type=float, default=3.0, help='pause between bursts, s')
    p.add_argument('--start-delay', type=float, default=3.0, help='wait after confirming, s')
    p.add_argument('--yes', action='store_true', help='do not ask before transmitting')
    p.add_argument('--stations', default=stations_mod.DEFAULT_CONFIG)
    args = p.parse_args()

    try:
        st = dict(stations_mod.pick_local(args.station, args.stations))
    except ValueError as exc:
        p.error(str(exc))
    if args.ptt_line:
        st['ptt_line'] = args.ptt_line
    if not st['ptt_port'] and st['ptt_line'] != 'vox':
        p.error(f"{st['name']} has no PTT serial port detected, so it can't transmit")
    level = args.level if args.level is not None else st.get('level', 0.5)

    total = len(args.modes) * args.count
    print(f"About to TRANSMIT {total} burst(s) as {args.src}: modes {args.modes} x {args.count}.")
    port_desc = st['ptt_port'] or 'audio-triggered, no PTT line'
    print(f"  {st['name']}: PTT {st['ptt_line'].upper()} on {port_desc}, audio {st['tx_device']}, "
          f"level {level}")
    print("Radio on a clear frequency at low power? Ctrl-C stops (PTT is always released).")
    if not args.yes:
        try:
            answer = input("Type 'yes' to transmit: ").strip().lower()
        except EOFError:
            answer = ''
        if answer != 'yes':
            print("not transmitting")
            return 1

    print(f"starting in {args.start_delay:g} s - start the listener on the other station now", flush=True)
    sent = 0
    try:
        time.sleep(args.start_delay)
        with tempfile.TemporaryDirectory() as tmp:
            for mode in args.modes:
                for i in range(args.count):
                    if sent:
                        time.sleep(args.gap)
                    text = args.text or f"{args.src} OpenWave test mode {mode} #{i + 1}"
                    samples = awt.build_samples(text, mode, args.fec, args.port, args.src, level,
                                                args.lead_in)
                    wav = os.path.join(tmp, 'tx.wav')
                    rlt.write_wav(wav, samples)
                    rlt.transmit(st, wav, args.key_delay)
                    sent += 1
                    m = link.WAVEFORM_MODES[mode]
                    print(f"  sent mode {mode} ({m['modulation']} {m['symbol_rate']}) #{i + 1}: "
                          f"{text!r}, {len(samples) / audio.AUDIO_SAMPLE_RATE:.2f} s on air", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted")
    print(f"sent {sent} of {total} burst(s)")
    return 0 if sent == total else 2


if __name__ == '__main__':
    sys.exit(main())
