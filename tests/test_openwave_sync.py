"""
Unit tests for openwave_sync.py, tolerant preamble/sync-word correlation.

    python3 -m unittest tests.test_openwave_sync -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import openwave_sync as sync
import openwave_link as link
import openwave_modem as modem


def _corrupt_bits(bits, indices):
    bits = list(bits)
    for i in indices:
        bits[i] ^= 1
    return bits


class HammingDistanceTests(unittest.TestCase):
    def test_identical_is_zero(self):
        self.assertEqual(sync.hamming_distance([1, 0, 1, 1], [1, 0, 1, 1]), 0)

    def test_all_different(self):
        self.assertEqual(sync.hamming_distance([1, 1, 1], [0, 0, 0]), 3)

    def test_partial_difference(self):
        self.assertEqual(sync.hamming_distance([1, 0, 1, 0], [1, 1, 1, 1]), 2)


class FindSyncOffsetsTests(unittest.TestCase):
    def test_exact_marker_found_at_zero_errors(self):
        bits = list(sync.MARKER_BITS) + [0] * 16  # marker + a bit of header
        candidates = sync.find_sync_offsets(bits)
        self.assertIn((0, 0), candidates)

    def test_corrupted_marker_still_found_within_tolerance(self):
        # flip 10% of the marker bits - well within the 15% default tolerance
        num_errors = int(sync.MARKER_LEN * 0.10)
        indices = list(range(0, sync.MARKER_LEN, sync.MARKER_LEN // num_errors))[:num_errors]
        corrupted = _corrupt_bits(sync.MARKER_BITS, indices)
        bits = corrupted + [0] * 16
        candidates = sync.find_sync_offsets(bits)
        offsets = [c[0] for c in candidates]
        self.assertIn(0, offsets)

    def test_too_corrupted_marker_not_found(self):
        # flip 40% of the bits - well beyond the 15% default tolerance
        num_errors = int(sync.MARKER_LEN * 0.40)
        indices = list(range(0, sync.MARKER_LEN, max(1, sync.MARKER_LEN // num_errors)))[:num_errors]
        corrupted = _corrupt_bits(sync.MARKER_BITS, indices)
        bits = corrupted + [0] * 16
        candidates = sync.find_sync_offsets(bits)
        self.assertEqual(candidates, [])

    def test_no_marker_present(self):
        bits = [0, 1] * 200
        candidates = sync.find_sync_offsets(bits)
        self.assertEqual(candidates, [])

    def test_buffer_shorter_than_marker(self):
        self.assertEqual(sync.find_sync_offsets([0, 1, 1]), [])


class BestSyncOffsetTests(unittest.TestCase):
    def test_picks_the_lowest_error_candidate(self):
        # two near-matches: one with 5 errors, one with 2 - best should win
        marker = list(sync.MARKER_BITS)
        far = _corrupt_bits(marker, [0, 10, 20, 30, 40])
        near = _corrupt_bits(marker, [1, 15])
        bits = far + [0] * 50 + near
        best = sync.best_sync_offset(bits)
        self.assertEqual(best, (len(far) + 50, 2))

    def test_none_when_nothing_clears_threshold(self):
        bits = [0, 1] * 200
        self.assertIsNone(sync.best_sync_offset(bits))


class ExtractFrameAtTests(unittest.TestCase):
    def test_ok_frame(self):
        frame = link.build_frame(b'hello', seq=5, src_id=b'K0MDT', dst_id=b'W1AW', lead_pad=0, end_pad=0)
        bits = modem.bytes_to_bits(frame)
        header_start = sync.MARKER_LEN
        status, header, payload = sync.extract_frame_at(bits, header_start)
        self.assertEqual(status, 'ok')
        self.assertEqual(payload, b'hello')
        self.assertEqual(header['seq'], 5)

    def test_crc_fail(self):
        frame = bytearray(link.build_frame(b'hello', seq=1, lead_pad=0, end_pad=0))
        idx = frame.index(b'hello')
        frame[idx] ^= 0xFF
        bits = modem.bytes_to_bits(bytes(frame))
        status, header, payload = sync.extract_frame_at(bits, sync.MARKER_LEN)
        self.assertEqual(status, 'crc_fail')

    def test_bad_length(self):
        frame = link.build_frame_with_bad_length(b'x', declared_length=9999, lead_pad=0, end_pad=0)
        bits = modem.bytes_to_bits(frame)
        status, header, payload = sync.extract_frame_at(bits, sync.MARKER_LEN)
        self.assertEqual(status, 'bad_length')
        self.assertEqual(header['payload_len'], 9999)

    def test_truncated(self):
        bits = [0] * 100  # far short of a full header
        status, header, payload = sync.extract_frame_at(bits, 0)
        self.assertEqual(status, 'truncated')


class IterFramesCorrelatedTests(unittest.TestCase):
    def test_clean_frame_decodes(self):
        frame = link.build_frame(b'hello world', seq=1, src_id=b'K0MDT', dst_id=b'W1AW')
        results = list(sync.iter_frames_correlated(frame))
        self.assertEqual(len(results), 1)
        status, header, payload = results[0]
        self.assertEqual(status, 'ok')
        self.assertEqual(payload, b'hello world')

    def test_corrupted_preamble_still_decodes(self):
        # exact byte-match iter_frames() would fail on this - the whole point
        frame = bytearray(link.build_frame(b'still works', seq=1))
        preamble_start = frame.index(link.PREAMBLE)
        # flip a handful of bits within the preamble (well under 15% of 288 bits)
        for byte_idx in (preamble_start, preamble_start + 5, preamble_start + 15):
            frame[byte_idx] ^= 0x01

        # confirm the premise: exact-match iter_frames() genuinely fails here
        exact_results = list(link.iter_frames(bytes(frame)))
        self.assertEqual(exact_results, [])

        # but the tolerant correlator recovers it
        results = list(sync.iter_frames_correlated(bytes(frame)))
        self.assertEqual(len(results), 1)
        status, header, payload = results[0]
        self.assertEqual(status, 'ok')
        self.assertEqual(payload, b'still works')

    def test_multiple_frames(self):
        data = link.build_frame(b'first', seq=1) + link.build_frame(b'second', seq=2)
        results = list(sync.iter_frames_correlated(data))
        self.assertEqual([r[0] for r in results], ['ok', 'ok'])
        self.assertEqual([r[2] for r in results], [b'first', b'second'])

    def test_no_marker_yields_nothing(self):
        self.assertEqual(list(sync.iter_frames_correlated(b'nothing here at all' * 5)), [])

    def test_full_bpsk_pipeline_with_bit_errors(self):
        """End-to-end: modulate a frame to BPSK bits, flip a few bits in the
        preamble region (simulating channel-induced errors), demodulate back
        to bytes, and confirm the correlator still finds and decodes it."""
        lead_pad = 24
        frame = link.build_frame(b'over the air', seq=7, src_id=b'K0MDT', dst_id=b'ALL',
                                  lead_pad=lead_pad)
        symbols = modem.modulate_bpsk(frame)

        # find where the preamble bits actually are (after lead_pad's zero
        # bytes) and flip a few of them
        preamble_bit_start = lead_pad * 8
        corrupted_symbols = list(symbols)
        for offset in (2, 9, 20, 40, 100):
            bit_idx = preamble_bit_start + offset
            corrupted_symbols[bit_idx] = -corrupted_symbols[bit_idx]  # flip the decision

        recovered_bytes = modem.demodulate_bpsk(corrupted_symbols)

        exact_results = list(link.iter_frames(recovered_bytes))
        self.assertEqual(exact_results, [], "test premise: exact match should fail here")

        results = list(sync.iter_frames_correlated(recovered_bytes))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], 'ok')
        self.assertEqual(results[0][2], b'over the air')


class SyncGapTests(unittest.TestCase):
    """Same bug/fix as openwave_fec.decode_frames()'s SyncGapTests, for the
    non-FEC --correlate path: obliterating a middle frame's marker must be
    reported as an explicit 'gap', not silently skipped by locking onto a
    later frame's marker with no indication anything was lost."""

    def _three_frames_with_second_marker_destroyed(self):
        frame1 = link.build_frame(b'first', seq=1)
        # frame2's payload must be large enough that frame3's marker falls
        # OUTSIDE FRAGMENT_GAP_SEARCH_BITS once frame2's own marker is
        # destroyed - otherwise frame3 is still "nearby" and gets found
        # without ever needing (or reporting) the gap-fallback path at all
        frame2 = link.build_frame(b'second' * 100, seq=2)
        frame3 = link.build_frame(b'third', seq=3)
        data = bytearray(frame1 + frame2 + frame3)
        second_marker_start = data.index(link.PREAMBLE, len(frame1))
        marker_len = len(link.PREAMBLE) + len(link.SYNC_WORD)
        for i in range(second_marker_start, second_marker_start + marker_len):
            data[i] ^= 0xFF
        return bytes(data)

    def test_iter_frames_correlated_reports_gap(self):
        data = self._three_frames_with_second_marker_destroyed()
        results = list(sync.iter_frames_correlated(data))
        statuses = [r[0] for r in results]
        self.assertEqual(statuses, ['ok', 'gap', 'ok'])
        self.assertEqual(results[0][2], b'first')
        self.assertEqual(results[2][2], b'third')

    def test_describe_frames_correlated_reports_gap(self):
        data = self._three_frames_with_second_marker_destroyed()
        results = list(sync.describe_frames_correlated(data))
        statuses = [r['status'] for r in results]
        self.assertEqual(statuses, ['ok', 'gap', 'ok'])


class DescribeFramesCorrelatedTests(unittest.TestCase):
    def test_ok_frame_reports_sync_errors_and_fields(self):
        frame = link.build_frame(b'hello', seq=3, mode_id=1, src_id=b'K0MDT', dst_id=b'W1AW')
        [result] = list(sync.describe_frames_correlated(frame))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['sync_errors'], 0)
        self.assertEqual(result['header']['seq'], 3)
        self.assertEqual(result['payload'], b'hello')
        self.assertIsNotNone(result['crc_bytes'])

    def test_corrupted_preamble_reports_nonzero_sync_errors_but_still_decodes(self):
        frame = bytearray(link.build_frame(b'hi', seq=1))
        preamble_start = frame.index(link.PREAMBLE)
        frame[preamble_start] ^= 0x01
        frame[preamble_start + 3] ^= 0x01
        [result] = list(sync.describe_frames_correlated(bytes(frame)))
        self.assertEqual(result['status'], 'ok')
        self.assertGreater(result['sync_errors'], 0)
        self.assertEqual(result['payload'], b'hi')
        # actual received (corrupted) preamble bytes should differ from the ideal constant
        self.assertNotEqual(result['preamble'], link.PREAMBLE)

    def test_bad_length_reports_header_no_payload(self):
        frame = link.build_frame_with_bad_length(b'x', declared_length=9999)
        [result] = list(sync.describe_frames_correlated(frame))
        self.assertEqual(result['status'], 'bad_length')
        self.assertEqual(result['header']['payload_len'], 9999)
        self.assertIsNone(result['payload'])

    def test_no_marker_yields_nothing(self):
        self.assertEqual(list(sync.describe_frames_correlated(b'x' * 100)), [])

    def test_multiple_frames(self):
        data = link.build_frame(b'first', seq=1) + link.build_frame(b'second', seq=2)
        results = list(sync.describe_frames_correlated(data))
        self.assertEqual([r['status'] for r in results], ['ok', 'ok'])
        self.assertEqual([r['payload'] for r in results], [b'first', b'second'])


if __name__ == '__main__':
    unittest.main()
