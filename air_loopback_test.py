#!/usr/bin/env python3
"""Two complete stations talking over SimulatedAir (fmlink_radio), in real
time and in one process: the radio transport end to end - burst detection,
half-duplex muting, PTT queueing, reply timeouts - with no hardware.

  ./air_loopback_test.py             # modes 0 and 2, with ACKs
  ./air_loopback_test.py --auto      # negotiate the mode first
  ./air_loopback_test.py --noise 0.1 # a noisier "air"

Takes about a minute. Transmits nothing: the "radios" are in memory.
"""
import argparse
import asyncio
import sys
import time

import ota_station as ota
import fmlink_radio as radio


async def run(args):
    air = radio.SimulatedAir(noise_rms=args.noise)
    got = {'A': [], 'B': []}
    logs = []

    def make(name, peer):
        return ota.Station(
            name, peer, mode_id=args.mode, use_fec=True, use_ack=True, auto=args.auto,
            phy='audio', audio_port='mic', on_message=lambda t, n=name: got[n].append(t),
            on_log=lambda m, n=name: logs.append(f"[{n}] {m}"))

    a, b = make('K0AAA', 'K0BBB'), make('K0BBB', 'K0AAA')
    if args.log_dir:
        for st in (a, b):
            st.open_log_file(f"{args.log_dir}/{st.station_id}.log")
    got = {'K0AAA': [], 'K0BBB': []}
    for st, port in ((a, air.port()), (b, air.port())):
        await st.connect_radio({'rx_device': None, 'rx_channels': 1}, capture=port.capture,
                               player=port.player)
    ok = True
    try:
        for who, text in ((a, 'hello from A'), (b, 'hello back from B'), (a, 'and one more')):
            peer = b if who is a else a
            before = len(got[peer.station_id])
            t0 = time.monotonic()
            await who.send_text(text)
            for _ in range(80):   # up to 8 s for delivery after send_text returns
                if len(got[peer.station_id]) > before:
                    break
                await asyncio.sleep(0.1)
            delivered = len(got[peer.station_id]) > before and got[peer.station_id][-1] == text
            ok &= delivered
            print(f"{who.station_id} -> {peer.station_id}: {text!r}: "
                  f"{'delivered' if delivered else 'NOT delivered'} in {time.monotonic() - t0:.1f} s",
                  flush=True)
    finally:
        a.close()
        b.close()
    if args.verbose or not ok:
        print("\n".join(logs))
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--mode', type=int, default=0)
    p.add_argument('--auto', action='store_true')
    p.add_argument('--noise', type=float, default=0.02, help='noise rms on the simulated air')
    p.add_argument('--log-dir', help='write each station\'s --log-file style log here')
    p.add_argument('--verbose', '-v', action='store_true')
    sys.exit(asyncio.run(run(p.parse_args())))


if __name__ == '__main__':
    main()
