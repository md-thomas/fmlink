#!/usr/bin/env python3
"""
Simulate a noisy channel: add complex AWGN to a raw sample file (e.g. from
pulseshape.py), producing a degraded sample file for testing the rest of
the pipeline (matchedfilter.py, demodulate.py, openwave_cli.py recv) under
realistic conditions instead of a clean round trip.

    python3 channel.py -i shaped.cfile -o noisy.cfile --snr-db 10
    python3 channel.py -i shaped.cfile -o noisy.cfile --noise-voltage 0.05

This belongs between pulseshape.py and matchedfilter.py in the pipeline -
noise is a sample-level phenomenon, so it doesn't make sense before pulse
shaping. Matches phy_sim_loopback.grc's channels_channel_model_0 block,
which also only simulates AWGN today (frequency/timing offset left at
none) - see openwave_channel.py for the implementation.
"""
import argparse
import sys

import openwave_channel as channel
import openwave_modem as modem


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-i', '--input', required=True,
                         help='Input raw complex64 (.cfile) sample file')
    parser.add_argument('-o', '--output', required=True,
                         help='Output raw complex64 (.cfile) noisy sample file')
    noise_group = parser.add_mutually_exclusive_group(required=True)
    noise_group.add_argument('--snr-db', type=float,
                              help="Target SNR in dB, measured against this file's own "
                                   "average signal power")
    noise_group.add_argument('--noise-voltage', type=float,
                              help='Direct per-component (I/Q) noise standard deviation, '
                                   "matching channels_channel_model's noise_voltage parameter")
    parser.add_argument('--seed', type=int, default=None,
                         help='RNG seed for reproducible noise (default: random each run)')
    args = parser.parse_args()

    with open(args.input, 'rb') as f:
        samples = modem.iq_bytes_to_symbols(f.read())

    if args.snr_db is not None:
        noise_voltage = channel.snr_db_to_noise_voltage(samples, args.snr_db)
    else:
        noise_voltage = args.noise_voltage

    noisy = channel.add_awgn(samples, noise_voltage, seed=args.seed)
    out_bytes = modem.symbols_to_iq_bytes(noisy)

    with open(args.output, 'wb') as f:
        f.write(out_bytes)

    snr_note = f"snr_db={args.snr_db}" if args.snr_db is not None else "snr_db=n/a"
    print(f"Added AWGN (noise_voltage={noise_voltage:.5f}, {snr_note}) to "
          f"{len(samples)} samples -> {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
