#!/usr/bin/env python3
"""
FEC-decode a received (possibly noisy) frame file - finds sync via
openwave_sync's tolerant correlator, then Viterbi-decodes the Header and
Payload+CRC blocks, correcting bit errors within the code's capability.
Handles multi-fragment messages (as build_fragmented_frames() produces):
every fragment found is decoded independently and, on success, rebuilt as
a standard, uncoded frame (no lead/end-pad) - the concatenation of all
successfully-recovered fragments is written out, ready for
openwave_cli.py dump/recv to read directly (no --correlate needed -
decode_frames() already found sync and CRC-verified each corrected
fragment).

    python3 fec_decode.py -i noisy_fec.bin -o corrected.bin --preset r1_2_k7

--preset (or --generators/--k) must exactly match what fec_encode.py used.
Prints each fragment's status (ok/crc_fail/bad_length/truncated) to
stderr as it's decoded, plus a final summary; exits non-zero unless every
fragment found was recovered ('ok') - a partial recovery is still written
to --output (useful for debugging), but openwave_cli.py recv's
FragmentReassembler will correctly refuse to reassemble an incomplete run
into a fake "complete" message (see openwave_link.py's FragmentReassembler
docstring), so a partial file on its own isn't usable as-is.
"""
import argparse
import sys

import openwave_fec as fec
import openwave_link as link


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
    parser.add_argument('-i', '--input', required=True, help='Input received frame file')
    parser.add_argument('-o', '--output', required=True,
                         help='Output corrected, uncoded frame stream (written even if incomplete)')
    parser.add_argument('--preset', choices=sorted(fec.PRESETS), default='r1_2_k7',
                         help='Named FEC configuration - must match fec_encode.py (default: r1_2_k7)')
    parser.add_argument('--generators', default=None,
                         help='Comma-separated generator polynomials, overrides --preset')
    parser.add_argument('--k', type=int, default=None,
                         help='Constraint length, required with --generators')
    parser.add_argument('--interleaver', choices=fec.INTERLEAVERS, default=fec.DEFAULT_INTERLEAVER,
                         help=f'Must match fec_encode.py (default: {fec.DEFAULT_INTERLEAVER})')
    parser.add_argument('--max-error-fraction', type=float, default=None,
                         help='Sync correlator tolerance (default: openwave_sync default)')
    args = parser.parse_args()

    generators, k = _resolve_config(args)

    with open(args.input, 'rb') as f:
        data = f.read()

    kwargs = {'interleaver': args.interleaver}
    if args.max_error_fraction is not None:
        kwargs['max_error_fraction'] = args.max_error_fraction

    output = bytearray()
    num_ok = 0
    num_failed = 0
    for status, header, payload in fec.decode_frames(data, generators, k, **kwargs):
        seq_note = f" seq={header['seq']}" if header is not None else ""
        print(f"FEC decode: fragment status={status}{seq_note}", file=sys.stderr)
        if status == 'ok':
            num_ok += 1
            output += link.build_frame(
                payload, lead_pad=0, end_pad=0, version=header['version'],
                packet_type=header['packet_type'], mode_id=header['mode_id'],
                flags=header['flags'], seq=header['seq'], fragment_index=header['fragment_index'],
                src_id=header['src_id'].rstrip(b'\x00'), dst_id=header['dst_id'].rstrip(b'\x00'),
            )
        else:
            num_failed += 1

    if num_ok == 0:
        print("No fragments recovered.", file=sys.stderr)
        sys.exit(1)

    with open(args.output, 'wb') as f:
        f.write(output)

    if num_failed > 0:
        print(f"Recovered {num_ok} fragment(s), but {num_failed} failed - message is "
              f"INCOMPLETE -> {args.output} (partial)", file=sys.stderr)
        sys.exit(1)

    print(f"Recovered {num_ok} fragment(s) -> {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
