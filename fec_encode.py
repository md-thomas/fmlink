#!/usr/bin/env python3
"""
FEC-encode a frame file (e.g. from fmlink_cli.py send) - convolutionally
encodes the Header and Payload+CRC blocks (leaving the preamble/sync-word
untouched, see fmlink_fec.py's module docstring), producing a longer
frame ready for modulate.py.

    python3 fec_encode.py -i test.bin -o fec_test.bin --preset r1_2_k7
    python3 fec_encode.py -i test.bin -o fec_test.bin --generators 0o171,0o133 --k 7

Available presets: r1_2_k3, r1_2_k7 (NASA/Voyager standard), r1_2_k9,
r1_3_k3, r1_3_k7, r1_3_k9, r1_4_k3, r1_4_k7, r1_4_k9, r1_5_k7, r1_5_k9,
r1_6_k7, r1_6_k9 - see fmlink_fec.py's PRESETS table (and its
free_distance comment) for what each trades off, or run
fmlink_fec_generate_presets.py to search for/add others.

See fec_decode.py for the inverse - --preset (or --generators/--k) must
match exactly between encode and decode.
"""
import argparse
import sys

import fmlink_fec as fec


def _resolve_config(args):
    if args.preset:
        return fec.PRESETS[args.preset]
    if args.generators and args.k:
        generators = tuple(int(g, 0) for g in args.generators.split(','))
        return generators, args.k
    raise SystemExit('error: specify either --preset or both --generators and --k')


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-i', '--input', required=True, help='Input frame file')
    parser.add_argument('-o', '--output', required=True, help='Output FEC-encoded frame file')
    parser.add_argument('--preset', choices=sorted(fec.PRESETS), default='r1_2_k7',
                         help='Named FEC configuration (default: r1_2_k7)')
    parser.add_argument('--generators', default=None,
                         help='Comma-separated generator polynomials (e.g. "0o171,0o133"), '
                              'overrides --preset')
    parser.add_argument('--k', type=int, default=None,
                         help='Constraint length, required with --generators')
    parser.add_argument('--interleaver', choices=fec.INTERLEAVERS, default=fec.DEFAULT_INTERLEAVER,
                         help='Coded-bit interleaving within each FEC block - fec_decode.py must '
                              f'use the same (default: {fec.DEFAULT_INTERLEAVER})')
    args = parser.parse_args()

    generators, k = _resolve_config(args)

    with open(args.input, 'rb') as f:
        data = f.read()

    encoded = fec.encode_frame(data, generators, k, interleaver=args.interleaver)

    with open(args.output, 'wb') as f:
        f.write(encoded)

    rate = f'1/{len(generators)}'
    print(f"FEC-encoded {len(data)} bytes -> {len(encoded)} bytes "
          f"(rate {rate}, K={k}, {args.interleaver} interleaving) -> {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
