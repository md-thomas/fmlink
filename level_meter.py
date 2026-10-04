#!/usr/bin/env python3
"""Live input level meter for a station's receive audio.

Plugging a cable into an HT's speaker jack mutes its speaker, so you can't
set the volume by ear. Run this, open the radio's squelch (or tune to hiss)
and turn the volume knob until the bar sits in the middle with no clipping.

  ./level_meter.py                 # auto-detect: the interface on this computer
  ./level_meter.py aioc            # a detected station by name
  ./level_meter.py digirig@macbookair2   # on another computer: recorded over ssh,
                                         # meter shown here
  ./level_meter.py --device hw:1,0 # or name an ALSA device directly

Target for receiving: open-squelch hiss around -20 to -10 dBFS RMS with
peaks under -2 dBFS. Clipping (full scale) ruins decoding. Ctrl-C to stop.
"""
import argparse
import subprocess
import sys

import numpy as np

import radio_stations as stations_mod

RATE = 48000
UPDATE_S = 0.2
BAR_W = 40
FLOOR_DB = -60.0
# RMS zone marked on the bar, dBFS
GOOD_LO, GOOD_HI = -26.0, -10.0


def remote_cmd(st, argv):
    if stations_mod.is_local(st['host']):
        return list(argv)
    return ['ssh', '-o', 'BatchMode=yes', st['host'], ' '.join(argv)]


def db(x):
    return 20 * np.log10(max(x, 1e-6))


def bar(rms_db, peak_db):
    pos = lambda d: int(round(np.clip((d - FLOOR_DB) / -FLOOR_DB, 0, 1) * (BAR_W - 1)))
    cells = [' '] * BAR_W
    for i in range(pos(GOOD_LO), pos(GOOD_HI) + 1):
        cells[i] = '·'
    for i in range(pos(rms_db) + 1):
        cells[i] = '█'
    cells[pos(peak_db)] = '|'
    return ''.join(cells)


def verdict(rms_db, peak_db):
    if peak_db >= -0.5:
        return 'CLIPPING - turn the volume down'
    if rms_db < GOOD_LO - 14:
        return 'very low - turn the volume up'
    if rms_db < GOOD_LO:
        return 'low - turn up a little'
    if rms_db > GOOD_HI:
        return 'high - turn down a little'
    if peak_db > -2:
        return 'peaks near full scale - turn down a little'
    return 'good'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('station', nargs='?', help='a detected station, e.g. aioc (default: auto-detect)')
    p.add_argument('--stations', default=stations_mod.DEFAULT_CONFIG)
    p.add_argument('--device', help='ALSA capture device (instead of a station)')
    p.add_argument('--channels', type=int, choices=(1, 2), help='default: the station\'s, else 1')
    args = p.parse_args()

    if args.device:
        st = {'host': stations_mod.LOCAL, 'name': args.device, 'rx_device': args.device, 'rx_channels': 1}
    else:
        try:
            if args.station:
                st = stations_mod.resolve(args.station, stations_mod.find_stations(args.stations))
            else:
                found = stations_mod.find_stations(args.stations, local_only=True)
                if len(found) != 1:  # none here, or several: look everywhere and ask
                    found = stations_mod.find_stations(args.stations)
                    names = ', '.join(stations_mod.label(x) for x in found) or 'nothing detected'
                    raise ValueError(f"name the station to meter ({names})")
                st = found[0]
        except ValueError as exc:
            p.error(str(exc))
    device = args.device or st['rx_device']
    channels = args.channels or st['rx_channels']

    cmd = ['arecord', '-q', '-D', device, '-f', 'S16_LE', '-r', str(RATE), '-c', str(channels), '-t', 'raw']
    proc = subprocess.Popen(remote_cmd(st, cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    block = int(RATE * UPDATE_S) * channels * 2
    where = stations_mod.label(st) if 'ptt_line' in st else device
    print(f"metering {where} ({device}); Ctrl-C to stop\n"
          f"  bar: -60 dBFS ... 0 dBFS; '·' = target zone for RMS, '|' = peak", flush=True)
    max_peak = FLOOR_DB
    try:
        while True:
            data = proc.stdout.read(block)
            if len(data) < block:
                err = proc.stderr.read().decode(errors='replace').strip()
                sys.exit(f"\nrecording stopped: {err or 'no data'}")
            x = np.frombuffer(data, '<i2').astype(float) / 32768
            if channels > 1:
                x = x.reshape(-1, channels).mean(axis=1)
            rms_db, peak_db = db(x.std()), db(np.abs(x).max())
            max_peak = max(max_peak, peak_db)
            print(f"\r[{bar(rms_db, peak_db)}] rms {rms_db:6.1f}  peak {peak_db:6.1f} dBFS  "
                  f"{verdict(rms_db, peak_db):<44}", end='', flush=True)
    except KeyboardInterrupt:
        print(f"\nhighest peak {max_peak:.1f} dBFS")
    finally:
        proc.terminate()


if __name__ == '__main__':
    main()
