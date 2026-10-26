#!/usr/bin/env python3
"""
Apply RRC pulse shaping to a raw complex symbol file (e.g. from modulate.py),
producing an oversampled complex sample file - this is where symbol_rate/sps
actually enter the pipeline (see fmlink_pulse.py's module docstring).

    python3 pulseshape.py -i test.cfile -o shaped.cfile
    python3 pulseshape.py -i test.cfile -o shaped.cfile --sps 8 --alpha 0.5

Note: pulse_shape() alone is NOT zero-ISI - plotting this output directly
will NOT show a clean constellation. See matchedfilter.py for the other
half of the round trip that does.
"""
import argparse
import sys

import fmlink_pulse as pulse
import fmlink_modem as modem


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-i', '--input', required=True,
                         help='Input raw complex64 (.cfile) symbol file')
    parser.add_argument('-o', '--output', required=True,
                         help='Output raw complex64 (.cfile) oversampled sample file')
    parser.add_argument('--alpha', type=float, default=0.35, help='RRC roll-off (default: 0.35)')
    parser.add_argument('--sps', type=int, default=4, help='Samples per symbol (default: 4)')
    parser.add_argument('--span', type=int, default=11,
                         help='Filter span in symbols (default: 11)')
    args = parser.parse_args()

    with open(args.input, 'rb') as f:
        symbols = modem.iq_bytes_to_symbols(f.read())

    shaped = pulse.pulse_shape(symbols, alpha=args.alpha, sps=args.sps, span_symbols=args.span)
    out_bytes = modem.symbols_to_iq_bytes(shaped)

    with open(args.output, 'wb') as f:
        f.write(out_bytes)

    print(f"Pulse-shaped {len(symbols)} symbols -> {len(shaped)} samples "
          f"(sps={args.sps}, alpha={args.alpha}) -> {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
