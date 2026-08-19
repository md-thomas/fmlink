"""
OpenWave frame synchronization: find frame boundaries in a noisy/imperfect
bitstream via correlation against the known PREAMBLE+SYNC_WORD pattern,
tolerating some number of bit errors - unlike openwave_link.iter_frames()/
describe_frames(), which require an exact byte match and so gain nothing
from PREAMBLE being a PRN sequence under real noise (see
openwave_link.py's docstring: the whole point of a PRN preamble is a sharp
correlation peak that's still findable with some bits wrong - a byte-exact
search throws that property away, since a single flipped bit anywhere in
the 36-byte marker breaks an exact match completely).

This mirrors what digital_correlate_access_code_tag_bb does in the real GNU
Radio flowgraphs (rx_rtlsdr.grc/phy_sim_loopback.grc): a sliding Hamming-
distance correlator over the recovered bitstream, not a literal string
match. Use iter_frames_correlated()/describe_frames_correlated() here in
place of openwave_link.iter_frames()/describe_frames() when data may have
come through a noisy channel (e.g. channel.py) - see openwave_cli.py's
recv/dump --correlate flag.
"""
import numpy as np

import openwave_link as link
import openwave_modem as modem

MARKER_BITS = modem.bytes_to_bits(link.PREAMBLE + link.SYNC_WORD)
MARKER_LEN = len(MARKER_BITS)  # 288 bits (36 bytes: 32-byte preamble + 4-byte sync word)

DEFAULT_MAX_ERROR_FRACTION = 0.15  # tolerate up to ~15% of the marker's bits being wrong

# How far past the end of a just-decoded frame to look for the NEXT one's
# marker before treating it as genuinely missing, rather than letting an
# unbounded search happily lock onto whatever real marker comes next,
# however far away, silently skipping everything in between with no error
# at all. Found via a 3dB/100-iteration FEC sweep across presets: pass
# rates didn't track code strength the way they should have, and a direct
# repro showed decode_frames() yielding fragment seq 0, 2, 3, 4 - fragment
# 1's marker was too corrupted to clear the threshold, so the unbounded
# search just kept going and landed on fragment 2's marker instead,
# reporting nothing wrong at all. Default lead_pad+end_pad between frames
# is 48 bytes (384 bits) - this window is >10x that for margin against a
# shifted match, while staying far smaller than even the smallest realistic
# coded fragment (so it can't accidentally reach the NEXT real fragment).
FRAGMENT_GAP_SEARCH_BITS = 4096


def hamming_distance(a, b):
    """Count differing bits between two equal-length 0/1 sequences."""
    return int(np.count_nonzero(np.asarray(a) != np.asarray(b)))


def find_sync_offsets(bits, max_errors=None, max_error_fraction=DEFAULT_MAX_ERROR_FRACTION,
                       search_window=None):
    """Slide MARKER_BITS across bits and return a list of (offset, errors)
    for every position where the Hamming distance to the marker is <=
    max_errors (default: max_error_fraction * MARKER_LEN, rounded down).
    offset is the bit index where the marker begins - the header starts at
    offset + MARKER_LEN. Not deduplicated: a real correlation peak isn't
    infinitely sharp, so several nearby offsets may all clear the
    threshold - see best_sync_offset() for picking a single winner.

    search_window: if given, only scan the first search_window possible
    offsets rather than the entire buffer - see FRAGMENT_GAP_SEARCH_BITS
    and best_sync_offset()'s docstring for why a bounded, nearby search is
    what callers scanning for a KNOWN-nearby next frame should use."""
    bits_arr = np.asarray(bits, dtype=np.int8)
    marker_arr = np.asarray(MARKER_BITS, dtype=np.int8)
    n = len(bits_arr) - len(marker_arr) + 1
    if search_window is not None:
        n = min(n, search_window)
    if n <= 0:
        return []
    if max_errors is None:
        max_errors = int(len(marker_arr) * max_error_fraction)

    results = []
    for offset in range(n):
        errors = int(np.count_nonzero(bits_arr[offset:offset + len(marker_arr)] != marker_arr))
        if errors <= max_errors:
            results.append((offset, errors))
    return results


def best_sync_offset(bits, max_errors=None, max_error_fraction=DEFAULT_MAX_ERROR_FRACTION,
                      search_window=None):
    """Return the (offset, errors) pair with the fewest bit errors among
    find_sync_offsets()'s candidates, or None if none cleared the
    threshold. Ties broken by earliest offset.

    search_window: bound the search to the first search_window bit
    positions (see FRAGMENT_GAP_SEARCH_BITS). Pass this whenever the next
    marker is expected nearby (e.g. right after a just-decoded frame) -
    an unbounded search doesn't fail loudly when the nearby marker didn't
    survive noise, it just locks onto whatever real marker comes next,
    however far away, silently skipping everything in between."""
    candidates = find_sync_offsets(bits, max_errors, max_error_fraction, search_window=search_window)
    if not candidates:
        return None
    return min(candidates, key=lambda c: (c[1], c[0]))


def extract_frame_at(bits, header_start):
    """Given a full bit array and the bit offset where a header should
    start (i.e. right after a preamble+sync-word match - see
    best_sync_offset()), pack the remaining bits into bytes and run them
    through openwave_link.FrameParser exactly as the byte-oriented path
    does, so header parsing/CRC checking can't drift between the two.

    Returns (status, header, payload):
      'ok' / 'crc_fail' / 'bad_length' - see FrameParser.feed()
      'truncated' - not enough bits remain for a complete header
    header is FrameParser.last_header (None only if truncated before the
    header itself finished parsing)."""
    remaining = bits[header_start:]
    usable_len = len(remaining) - (len(remaining) % 8)
    frame_bytes = modem.bits_to_bytes(remaining[:usable_len])

    parser = link.FrameParser()
    for byte in frame_bytes:
        status, payload = parser.feed(byte)
        if status != 'incomplete':
            return status, parser.last_header, payload
    return 'truncated', parser.last_header, None


def iter_frames_correlated(data, max_error_fraction=DEFAULT_MAX_ERROR_FRACTION):
    """Like openwave_link.iter_frames(), but finds each frame's start via
    Hamming-distance correlation instead of an exact byte match - tolerates
    bit errors in the marker itself. Yields (status, header, payload)
    tuples, status in ('ok', 'crc_fail', 'bad_length', 'gap') - stops
    (instead of yielding a 'truncated' status) once no further sync
    candidate clears the threshold or a match runs out of bits, mirroring
    iter_frames()'s behavior at the end of a buffer.

    'gap': every marker, including the first, is searched for within
    FRAGMENT_GAP_SEARCH_BITS of where the previous one ended (or of
    pos=0, for the first). If nothing clears the threshold that nearby,
    this yields ('gap', None, None) and falls back to an unbounded search
    so later frames can still be found - without this, an unbounded
    search would just silently lock onto whatever real marker comes next,
    however far away, with no indication a frame in between was lost (see
    FRAGMENT_GAP_SEARCH_BITS)."""
    bits = modem.bytes_to_bits(data)
    pos = 0
    while True:
        match = best_sync_offset(bits[pos:], max_error_fraction=max_error_fraction,
                                  search_window=FRAGMENT_GAP_SEARCH_BITS)
        if match is None:
            match = best_sync_offset(bits[pos:], max_error_fraction=max_error_fraction)
            if match is None:
                return
            yield 'gap', None, None

        rel_offset, _errors = match
        marker_start = pos + rel_offset
        header_start = marker_start + MARKER_LEN

        status, header, payload = extract_frame_at(bits, header_start)
        if status == 'truncated':
            return

        yield status, header, payload

        if status == 'bad_length' or header is None:
            # don't know the real frame length - nudge forward past this
            # match and keep searching, same spirit as iter_frames()'s
            # pos = idx + 1 (allows re-search rather than skipping blindly)
            pos = marker_start + 8
            continue

        total_bits = (link.HEADER_LEN + header['payload_len'] + link.CRC_LEN) * 8
        pos = header_start + total_bits


def describe_frames_correlated(data, max_error_fraction=DEFAULT_MAX_ERROR_FRACTION):
    """Like openwave_link.describe_frames(), but using the same tolerant
    correlation-based sync search as iter_frames_correlated(). Yields one
    dict per frame found:
      {'offset': int, 'sync_errors': int, 'status': 'ok'|'crc_fail'|'bad_length'|'gap',
       'preamble': bytes, 'sync_word': bytes, 'header_bytes': bytes or None,
       'header': dict or None, 'payload': bytes or None,
       'crc_bytes': bytes or None, 'total_len': int or None}
    'offset'/'preamble'/'sync_word' reflect the ACTUAL received bytes at the
    matched position (which may contain bit errors - that's the point of
    'sync_errors', the Hamming distance found there), not the ideal
    PREAMBLE/SYNC_WORD constants.

    'gap' entries (all other fields None) mean no marker cleared the
    threshold within FRAGMENT_GAP_SEARCH_BITS of the previous frame - see
    iter_frames_correlated()'s docstring for why this is reported
    explicitly instead of silently locking onto a later, farther-away
    frame with no indication anything was skipped."""
    bits = modem.bytes_to_bits(data)
    pos = 0
    while True:
        match = best_sync_offset(bits[pos:], max_error_fraction=max_error_fraction,
                                  search_window=FRAGMENT_GAP_SEARCH_BITS)
        if match is None:
            match = best_sync_offset(bits[pos:], max_error_fraction=max_error_fraction)
            if match is None:
                return
            yield {'offset': pos // 8, 'sync_errors': None, 'status': 'gap',
                   'preamble': None, 'sync_word': None, 'header_bytes': None,
                   'header': None, 'payload': None, 'crc_bytes': None, 'total_len': None}

        rel_offset, errors = match
        marker_start = pos + rel_offset
        header_start = marker_start + MARKER_LEN

        marker_bits = bits[marker_start:header_start]
        marker_bytes = modem.bits_to_bytes(marker_bits) if len(marker_bits) == MARKER_LEN else None
        preamble = marker_bytes[:len(link.PREAMBLE)] if marker_bytes else None
        sync_word = marker_bytes[len(link.PREAMBLE):] if marker_bytes else None

        status, header, payload = extract_frame_at(bits, header_start)
        if status == 'truncated':
            yield {'offset': marker_start // 8, 'sync_errors': errors, 'status': 'truncated',
                   'preamble': preamble, 'sync_word': sync_word, 'header_bytes': None,
                   'header': header, 'payload': None, 'crc_bytes': None, 'total_len': None}
            return

        # Extracted directly from the received bits (not rebuilt from the
        # parsed header dict) so any bit corruption is actually visible here
        # - a diagnostic tool should show what was really received, not a
        # reconstructed clean version (the parsed dict doesn't even retain
        # the 'reserved' field, for instance).
        header_end_bit = header_start + link.HEADER_LEN * 8
        header_bits = bits[header_start:header_end_bit]
        header_bytes = (modem.bits_to_bytes(header_bits)
                         if len(header_bits) == link.HEADER_LEN * 8 else None)

        if status == 'bad_length':
            yield {'offset': marker_start // 8, 'sync_errors': errors, 'status': status,
                   'preamble': preamble, 'sync_word': sync_word, 'header_bytes': header_bytes,
                   'header': header, 'payload': None, 'crc_bytes': None, 'total_len': None}
            pos = marker_start + 8
            continue

        crc_bytes = None
        total_len = None
        if header is not None:
            crc_start_bit = header_start + (link.HEADER_LEN + header['payload_len']) * 8
            crc_end_bit = crc_start_bit + link.CRC_LEN * 8
            crc_bits = bits[crc_start_bit:crc_end_bit]
            if len(crc_bits) == link.CRC_LEN * 8:
                crc_bytes = modem.bits_to_bytes(crc_bits)
                total_len = (crc_end_bit - marker_start) // 8

        yield {'offset': marker_start // 8, 'sync_errors': errors, 'status': status,
               'preamble': preamble, 'sync_word': sync_word, 'header_bytes': header_bytes,
               'header': header, 'payload': payload, 'crc_bytes': crc_bytes, 'total_len': total_len}

        total_bits = (link.HEADER_LEN + header['payload_len'] + link.CRC_LEN) * 8
        pos = header_start + total_bits
