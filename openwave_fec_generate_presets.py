#!/usr/bin/env python3
"""
Regenerate/extend openwave_fec.py's PRESETS table using find_generators()'s
free-distance search, instead of hand-picking or half-remembering generator
polynomials (see openwave_fec.py's PRESETS comment for why that's
error-prone - an earlier hand-picked preset had out-of-range values that
were silently truncated).

    python3 openwave_fec_generate_presets.py
    python3 openwave_fec_generate_presets.py --rates 2 3 4 5 6 7 --k 3 7 9 11
    python3 openwave_fec_generate_presets.py --num-candidates 10000

This only prints Python source for a new PRESETS dict (and each preset's
free_distance) - it does NOT edit openwave_fec.py for you. Review the
output and paste it in by hand; searches are randomized (though seeded, so
reproducible per --seed) and worth a human sanity check before committing,
same as the original set was.
"""
import argparse
import sys

import openwave_fec as fec


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rates', type=int, nargs='+', default=[2, 3, 4, 5, 6],
                         help='Rate denominators to search (default: 2 3 4 5 6)')
    parser.add_argument('--k', type=int, nargs='+', default=[3, 7, 9],
                         help='Constraint lengths to search (default: 3 7 9)')
    parser.add_argument('--num-candidates', type=int, default=3000,
                         help='Random candidates tried per (rate, K) pair (default: 3000)')
    parser.add_argument('--seed', type=int, default=42, help='RNG seed (default: 42)')
    args = parser.parse_args()

    print("PRESETS = {")
    for k in args.k:
        for rate_n in args.rates:
            name = f'r1_{rate_n}_k{k}'
            try:
                generators, dist = fec.find_generators(
                    rate_n, k, num_candidates=args.num_candidates, seed=args.seed)
            except ValueError as e:
                print(f"    # {name}: SKIPPED - {e}", file=sys.stderr)
                continue
            octal = ', '.join(f'0o{g:o}' for g in generators)
            print(f"    '{name}': (({octal}), {k}),  # free_distance={dist}")
    print("}")


if __name__ == '__main__':
    main()
