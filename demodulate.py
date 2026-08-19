#!/usr/bin/env python3
"""
Demodulate a raw complex symbol file (e.g. from modulate.py) back into
bytes, using openwave_modem.py's bits<->symbols mapping. This assumes a
clean, already-aligned symbol stream - exactly what modulate.py produces -
with no noise, timing, or carrier recovery involved. It's a round-trip
correctness check for the bits<->symbols mapping, not a receiver for a real
noisy/unsynchronized signal (that's what rx_rtlsdr.grc's Symbol Sync/Costas
Loop chain is for).

    python3 demodulate.py -t bpsk -i test.cfile -o out.bin

See modulate.py for the inverse.
"""
import argparse
import sys

import openwave_modem as modem

DEMODULATORS = {
    'bpsk': modem.demodulate_bpsk,
    'qpsk': modem.demodulate_qpsk,
    '16qam': modem.demodulate_qam16,
}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-t', '--type', choices=sorted(DEMODULATORS), required=True,
                         help='Modulation scheme')
    parser.add_argument('-i', '--input', required=True,
                         help='Input raw complex64 (.cfile) symbol file')
    parser.add_argument('-o', '--output', required=True, help='Output byte file')
    args = parser.parse_args()

    with open(args.input, 'rb') as f:
        iq_bytes = f.read()

    symbols = modem.iq_bytes_to_symbols(iq_bytes)
    data = DEMODULATORS[args.type](symbols)

    with open(args.output, 'wb') as f:
        f.write(data)

    print(f"Demodulated {len(symbols)} {args.type} symbols -> {len(data)} bytes "
          f"-> {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
