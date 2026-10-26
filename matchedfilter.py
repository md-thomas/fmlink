#!/usr/bin/env python3
"""
Apply the RX-side RRC matched filter to an oversampled complex sample file
(e.g. from pulseshape.py) and decimate back down to one sample per symbol,
producing a raw complex symbol file compatible with demodulate.py. --alpha/
--sps/--span must match what pulseshape.py used to build the input.

    python3 matchedfilter.py -i shaped.cfile -o symbols.cfile

This is the other half of the pulse_shape()/matched_filter() round trip -
see fmlink_pulse.py's module docstring for why a single RRC filter alone
isn't zero-ISI, and why both halves are needed for a clean constellation.
"""
import argparse
import sys

import fmlink_pulse as pulse
import fmlink_modem as modem


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-i', '--input', required=True,
                         help='Input raw complex64 (.cfile) oversampled sample file')
    parser.add_argument('-o', '--output', required=True,
                         help='Output raw complex64 (.cfile) symbol file (one sample/symbol)')
    parser.add_argument('--alpha', type=float, default=0.35,
                         help='RRC roll-off - must match pulseshape.py (default: 0.35)')
    parser.add_argument('--sps', type=int, default=4,
                         help='Samples per symbol - must match pulseshape.py (default: 4)')
    parser.add_argument('--span', type=int, default=11,
                         help='Filter span in symbols - must match pulseshape.py (default: 11)')
    args = parser.parse_args()

    with open(args.input, 'rb') as f:
        samples = modem.iq_bytes_to_symbols(f.read())

    num_symbols = pulse.expected_symbol_count(len(samples), args.alpha, args.sps, args.span)
    filtered = pulse.matched_filter(samples, alpha=args.alpha, sps=args.sps, span_symbols=args.span)
    offset = pulse.group_delay(alpha=args.alpha, sps=args.sps, span_symbols=args.span)
    symbols = pulse.decimate(filtered, args.sps, offset)[:num_symbols]
    out_bytes = modem.symbols_to_iq_bytes(symbols)

    with open(args.output, 'wb') as f:
        f.write(out_bytes)

    print(f"Matched-filtered and decimated {len(samples)} samples -> {len(symbols)} symbols "
          f"(sps={args.sps}, alpha={args.alpha}) -> {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
