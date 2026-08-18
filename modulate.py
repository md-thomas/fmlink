#!/usr/bin/env python3
"""
Modulate a byte file (e.g. from openwave_cli.py send, or an rx_rtlsdr.grc
capture) into a raw complex symbol file, using openwave_modem.py's bits<->
symbols mapping. No pulse shaping and no sample rate involved - one complex
sample per symbol, written as raw interleaved float32 I/Q (GNU Radio's
.cfile convention). See openwave_modem.py's module docstring for why symbol
rate deliberately isn't a parameter here.

    python3 modulate.py -t bpsk -i test.bin -o test.cfile

See demodulate.py for the inverse.
"""
import argparse
import sys

import openwave_modem as modem

MODULATORS = {
    'bpsk': modem.modulate_bpsk,
}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-t', '--type', choices=sorted(MODULATORS), required=True,
                         help='Modulation scheme')
    parser.add_argument('-i', '--input', required=True, help='Input byte file')
    parser.add_argument('-o', '--output', required=True,
                         help='Output raw complex64 (.cfile) symbol file')
    args = parser.parse_args()

    with open(args.input, 'rb') as f:
        data = f.read()

    symbols = MODULATORS[args.type](data)
    iq_bytes = modem.symbols_to_iq_bytes(symbols)

    with open(args.output, 'wb') as f:
        f.write(iq_bytes)

    print(f"Modulated {len(data)} bytes -> {len(symbols)} {args.type} symbols "
          f"({len(iq_bytes)} bytes) -> {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
