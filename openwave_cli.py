#!/usr/bin/env python3
"""
Command-line, file-based testing for OpenWave's link layer - build and parse
real over-the-air frame bytes using openwave_link.py directly, with no GNU
Radio dependency and no hardware. Useful for verifying framing/CRC/ACK/
fragmentation/addressing logic end-to-end before it ever touches a modem.

Payload is application-layer content and opaque to the link layer - `send`
accepts either plain text or a JSON value (encoded to UTF-8 either way);
`recv` tries to json.loads() each decoded message and pretty-prints it if it
parses, falling back to plain text otherwise. The link layer itself doesn't
know or care which it was - see openwave_link.py's docstring.

Examples:
    python3 openwave_cli.py send "hello world" -o burst.bin
    python3 openwave_cli.py send --json '{"cmd": "ping", "n": 1}' -o burst.bin --append
    python3 openwave_cli.py recv -i burst.bin

    # simulate two stations exchanging addressed traffic in one file:
    python3 openwave_cli.py send "for w1aw" -o net.bin --src K0MDT --dst W1AW
    python3 openwave_cli.py send "for all"  -o net.bin --src K0MDT --dst ALL --append
    python3 openwave_cli.py recv -i net.bin --station-id W1AW

See `send -h` / `recv -h` for all options.
"""
import argparse
import json
import sys

import openwave_link as link


def _encode_payload(args):
    if args.json is not None:
        return json.dumps(json.loads(args.json)).encode('utf-8')
    return args.message.encode('utf-8')


def _locate_regions(frame_bytes):
    """Return (header_start, payload_start, crc_start) byte offsets within a
    single frame produced by build_frame()/build_fragmented_frames()."""
    marker = link.PREAMBLE + link.SYNC_WORD
    header_start = frame_bytes.index(marker) + len(marker)
    header = link.parse_header(bytes(frame_bytes[header_start:header_start + link.HEADER_LEN]))
    payload_start = header_start + link.HEADER_LEN
    crc_start = payload_start + header['payload_len']
    return header_start, payload_start, crc_start


def _corrupt_frame(frame_bytes, corrupt_header, corrupt_payload, corrupt_crc):
    """Flip one byte in each requested region, after the frame is otherwise
    built correctly - the point is to inject exactly the kind of damage a
    real noisy/lossy channel could cause, so FrameParser/LinkReceiver's
    rejection paths (crc_fail) have something real to reject."""
    frame_bytes = bytearray(frame_bytes)
    header_start, payload_start, crc_start = _locate_regions(frame_bytes)
    if corrupt_header:
        frame_bytes[header_start] ^= 0xFF  # Version byte
    if corrupt_payload and payload_start < crc_start:
        frame_bytes[payload_start] ^= 0xFF
    if corrupt_crc:
        frame_bytes[crc_start] ^= 0xFF
    return bytes(frame_bytes)


def cmd_send(args):
    payload = _encode_payload(args)
    flags = link.FLAG_ACK_REQUESTED if args.ack else 0

    if args.bad_length is not None:
        # A genuinely invalid Payload Length can't come from
        # build_fragmented_frames() (it always writes the real length) - use
        # the dedicated test helper instead, and skip fragmentation entirely
        # since there's no "real" chunk size to honor here.
        frame = link.build_frame_with_bad_length(
            payload, declared_length=args.bad_length, flags=flags, seq=args.seq,
            mode_id=args.mode, src_id=args.src.encode('ascii'), dst_id=args.dst.encode('ascii'),
        )
        frames, next_seq = [frame], (args.seq + 1) % link.SEQ_MODULUS
    else:
        frames, next_seq = link.build_fragmented_frames(
            payload, seq_start=args.seq, chunk_size=args.chunk_size, flags=flags,
            mode_id=args.mode, src_id=args.src.encode('ascii'), dst_id=args.dst.encode('ascii'),
        )

    if args.corrupt_header or args.corrupt_payload or args.corrupt_crc:
        frames = [_corrupt_frame(f, args.corrupt_header, args.corrupt_payload, args.corrupt_crc)
                  for f in frames]

    with open(args.output, 'ab' if args.append else 'wb') as f:
        for frame in frames:
            f.write(frame)

    total_bytes = sum(len(fr) for fr in frames)
    plural = 's' if len(frames) != 1 else ''
    corrupted = [name for name, on in (('header', args.corrupt_header),
                                        ('payload', args.corrupt_payload),
                                        ('crc', args.corrupt_crc)) if on]
    note = ''
    if args.bad_length is not None:
        note = f' [bad_length={args.bad_length}]'
    elif corrupted:
        note = f" [corrupted: {', '.join(corrupted)}]"
    print(f"Wrote {len(frames)} frame{plural} ({total_bytes} bytes) to {args.output}; "
          f"next seq would be {next_seq}{note}", file=sys.stderr)


def cmd_dump(args):
    with open(args.input, 'rb') as f:
        data = f.read()

    frame_num = 0
    for frame in link.describe_frames(data):
        frame_num += 1
        print(f"=== Frame {frame_num}  (offset {frame['offset']}, status: {frame['status']}) ===")
        print(f"  Preamble        ({len(frame['preamble'])}B): {frame['preamble'].hex(' ')}")
        print(f"  Sync Word       ({len(frame['sync_word'])}B): {frame['sync_word'].hex(' ')}")

        header = frame['header']
        if header is None:
            print("  (not enough bytes remain for a complete header)")
            print()
            continue

        print(f"  Version         : {header['version']}")
        print(f"  Packet Type     : 0x{header['packet_type']:02X} "
              f"({link.format_packet_type(header['packet_type'])})")
        print(f"  Waveform Mode ID: {header['mode_id']} ({link.format_mode(header['mode_id'])})")
        print(f"  Flags           : 0x{header['flags']:02X} ({link.format_flags(header['flags'])})")
        print(f"  Sequence Number : {header['seq']}")
        print(f"  Source ID       : {header['src_id'].hex(' ')}  \"{link.decode_station_id(header['src_id'])}\"")
        print(f"  Destination ID  : {header['dst_id'].hex(' ')}  \"{link.decode_station_id(header['dst_id'])}\"")
        print(f"  Payload Length  : {header['payload_len']}")

        if frame['status'] == 'bad_length':
            print(f"  (Payload Length {header['payload_len']} out of range "
                  f"0..{link.MAX_PAYLOAD_LEN} - frame abandoned)")
            print()
            continue
        if frame['payload'] is None:
            print("  (not enough bytes remain for the full payload+CRC)")
            print()
            continue

        payload = frame['payload']
        print(f"  Payload         ({len(payload)}B): {payload.hex(' ')}")
        try:
            print(f"                   text: {payload.decode('utf-8')!r}")
        except UnicodeDecodeError:
            pass
        crc_status = 'OK' if frame['status'] == 'ok' else 'MISMATCH'
        print(f"  CRC32           : {frame['crc_bytes'].hex(' ')}  ({crc_status})")
        print(f"  Total frame size: {frame['total_len']} bytes")
        print()

    if frame_num == 0:
        print("No frames found (no preamble+sync-word marker in file).", file=sys.stderr)


def cmd_recv(args):
    with open(args.input, 'rb') as f:
        data = f.read()

    messages = []
    receiver = link.LinkReceiver(
        station_id=args.station_id,
        on_message=messages.append,
        on_ack_needed=lambda seq: print(f"[ack needed for seq {seq} - no TX wired up in file mode]", file=sys.stderr),
        on_ack_received=lambda seq: print(f"[ack received for seq {seq}]", file=sys.stderr),
    )

    for status, header, payload in link.iter_frames(data):
        if status == 'ok':
            receiver.feed_frame(header, payload)
        else:
            receiver.feed_crc_failure(status)

    for msg in messages:
        try:
            print(json.dumps(json.loads(msg), indent=2))
        except json.JSONDecodeError:
            print(msg)

    print(receiver.stats_line(), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Not required: a bare `openwave_cli.py` with no command prints help
    # (see the args.command is None check below) instead of an argparse error.
    sub = parser.add_subparsers(dest='command')

    p_help = sub.add_parser('help', help='Show this help message and exit')
    p_help.set_defaults(func=lambda args: parser.print_help())

    p_send = sub.add_parser('send', help='Build a frame (or fragments) and write it to a file')
    payload_group = p_send.add_mutually_exclusive_group(required=True)
    payload_group.add_argument('message', nargs='?', help='Plain text payload to send')
    payload_group.add_argument('--json', help='JSON payload to send (validated and re-serialized)')
    p_send.add_argument('-o', '--output', required=True, help='Output file path')
    p_send.add_argument('--append', action='store_true',
                         help='Append to output instead of overwriting (for building a multi-frame test file)')
    p_send.add_argument('--mode', type=int, default=0, help='Waveform Mode ID (default: 0)')
    p_send.add_argument('--src', default='', help='Source callsign, max 8 chars (default: none)')
    p_send.add_argument('--dst', default='',
                         help='Destination callsign, max 8 chars, or ALL for broadcast (default: none)')
    p_send.add_argument('--seq', type=int, default=0, help='Starting Sequence Number (default: 0)')
    p_send.add_argument('--ack', action='store_true', help='Set the ACK Requested flag')
    p_send.add_argument('--chunk-size', type=int, default=link.MAX_PAYLOAD_LEN,
                         help='Force fragmentation below MAX_PAYLOAD_LEN, to test FragmentReassembler '
                              '(default: no forced fragmentation)')
    p_send.add_argument('--corrupt-header', action='store_true',
                         help='Flip a byte in the header after building, to test CRC rejection '
                              '(recv/dump should report crc_fail)')
    p_send.add_argument('--corrupt-payload', action='store_true',
                         help='Flip a byte in the payload after building, to test CRC rejection')
    p_send.add_argument('--corrupt-crc', action='store_true',
                         help='Flip a byte in the CRC32 trailer after building, to test CRC rejection '
                              '(header and payload both stay intact and valid)')
    p_send.add_argument('--bad-length', type=int, default=None, metavar='N',
                         help='Write N into the header Payload Length field instead of the real '
                              'length, to test bad_length rejection. Ignores --chunk-size and always '
                              'produces exactly one frame.')
    p_send.set_defaults(func=cmd_send)

    p_dump = sub.add_parser('dump', help='Hex-dump and label every field of each frame in a file')
    p_dump.add_argument('-i', '--input', required=True, help='Input file path')
    p_dump.set_defaults(func=cmd_dump)

    p_recv = sub.add_parser('recv', help='Scan a file for frames and decode them')
    p_recv.add_argument('-i', '--input', required=True, help='Input file path')
    p_recv.add_argument('--station-id', default='',
                         help="This receiver's callsign, for Destination ID filtering "
                              "(default: accept everything)")
    p_recv.set_defaults(func=cmd_recv)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    args.func(args)


if __name__ == '__main__':
    main()
