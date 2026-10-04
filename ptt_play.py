#!/usr/bin/env python3
"""Key a radio's PTT over a serial line, play a WAV through its audio
interface, release PTT. Standalone (stdlib + pyserial) so radio_link_test.py
can copy it to a remote station and run it there.

  ptt_play.py --wav tx.wav --device hw:AllInOneCable,0 --ptt-port /dev/ttyACM0 --ptt-line dtr

THIS TRANSMITS. PTT is always released, even on error or Ctrl-C.
"""
import argparse
import subprocess
import time


def key_and_play(wav, device, ptt_port, ptt_line, key_delay=0.2):
    import serial
    ptt = serial.Serial(ptt_port, 9600, dsrdtr=False, rtscts=False)
    ptt.dtr = False
    ptt.rts = False
    try:
        setattr(ptt, ptt_line, True)
        time.sleep(key_delay)
        subprocess.run(['aplay', '-q', '-D', device, wav], check=True, timeout=60)
        time.sleep(0.1)
    finally:
        ptt.dtr = False
        ptt.rts = False
        ptt.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--wav', required=True)
    p.add_argument('--device', required=True, help='ALSA playback device')
    p.add_argument('--ptt-port', required=True)
    p.add_argument('--ptt-line', choices=('dtr', 'rts'), required=True)
    p.add_argument('--key-delay', type=float, default=0.2, help='seconds between PTT and audio')
    a = p.parse_args()
    key_and_play(a.wav, a.device, a.ptt_port, a.ptt_line, a.key_delay)


if __name__ == '__main__':
    main()
