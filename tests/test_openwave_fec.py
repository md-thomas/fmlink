"""
Unit tests for openwave_fec.py: convolutional encoding + Viterbi decoding.

    python3 -m unittest tests.test_openwave_fec -v
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import openwave_fec as fec
import openwave_link as link
import openwave_modem as modem
import openwave_sync as sync


def _flip_bits(bits, indices):
    bits = list(bits)
    for i in indices:
        bits[i] ^= 1
    return bits


def _random_bits(n, seed):
    rng = np.random.default_rng(seed)
    return [int(b) for b in rng.integers(0, 2, n)]


def _decode_first(data, generators, k, **kwargs):
    """Consume fec.decode_frames() and return just its first
    (status, header, payload) result, or ('no_sync', None, None) if it
    yielded nothing - for tests that only care about a single frame."""
    for result in fec.decode_frames(data, generators, k, **kwargs):
        return result
    return 'no_sync', None, None


class KnownFreeDistanceTests(unittest.TestCase):
    """free_distance() validated against published, independently-known
    values for real reference codes - if this function were wrong, wrong
    values here would be the tell (as opposed to trusting an empirical
    correction-rate test, which can look fine even with a buggy metric)."""

    def test_r1_2_k3_textbook_code(self):
        self.assertEqual(fec.free_distance((0o7, 0o5), 3), 5)

    def test_r1_2_k7_nasa_voyager_standard(self):
        self.assertEqual(fec.free_distance((0o171, 0o133), 7), 10)

    def test_r1_2_k9_published_code(self):
        self.assertEqual(fec.free_distance((0o753, 0o561), 9), 12)


class FindGeneratorsTests(unittest.TestCase):
    def test_finds_the_known_k7_optimum(self):
        # the search should independently rediscover free_distance=10 for
        # rate 1/2 K=7 - the actual known-optimal value, not just "a" value
        generators, dist = fec.find_generators(2, 7, num_candidates=1000, seed=1)
        self.assertEqual(dist, 10)

    def test_returned_generators_actually_achieve_reported_distance(self):
        generators, dist = fec.find_generators(3, 7, num_candidates=500, seed=2)
        self.assertEqual(fec.free_distance(generators, 7), dist)

    def test_generators_are_distinct_and_valid(self):
        generators, _ = fec.find_generators(4, 7, num_candidates=500, seed=3)
        self.assertEqual(len(set(generators)), len(generators))
        for g in generators:
            self.assertTrue(1 <= g < (1 << 7))
            self.assertEqual(g & 1, 1, "generator must have bit0=1")

    def test_rate_exceeding_k3_capacity_raises(self):
        with self.assertRaises(ValueError):
            fec.find_generators(5, 3, num_candidates=100)

    def test_rate_at_k3_capacity_limit_succeeds(self):
        # k=3 has exactly 4 valid distinct generators - rate 1/4 is the
        # boundary case that should just barely work
        generators, dist = fec.find_generators(4, 3, num_candidates=100)
        self.assertEqual(len(generators), 4)


class PresetFamilyIntegrityTests(unittest.TestCase):
    """Sanity checks on the whole PRESETS table as a family, not just
    individual entries - catches e.g. a copy-paste mistake that duplicated
    one preset's generators into another's slot."""

    def test_all_presets_generators_valid_for_their_k(self):
        for name, (generators, k) in fec.PRESETS.items():
            for g in generators:
                self.assertTrue(1 <= g < (1 << k), f"{name}: generator {oct(g)} invalid for K={k}")
                self.assertEqual(g & 1, 1, f"{name}: generator {oct(g)} has bit0=0")
            self.assertEqual(len(set(generators)), len(generators), f"{name}: duplicate generators")

    def test_free_distance_increases_with_rate_for_fixed_k(self):
        for k in (7, 9):
            distances = []
            for rate_n in range(2, 7):
                name = f'r1_{rate_n}_k{k}'
                if name not in fec.PRESETS:
                    continue
                generators, _ = fec.PRESETS[name]
                distances.append(fec.free_distance(generators, k))
            self.assertEqual(distances, sorted(distances),
                              f"free distance should increase with rate at K={k}")

    def test_free_distance_increases_with_k_for_fixed_rate(self):
        for rate_n in (2, 3, 4):
            distances = []
            for k in (3, 7, 9):
                name = f'r1_{rate_n}_k{k}'
                generators, _ = fec.PRESETS[name]
                distances.append(fec.free_distance(generators, k))
            self.assertEqual(distances, sorted(distances),
                              f"free distance should increase with K at rate 1/{rate_n}")


class CodedBitLengthTests(unittest.TestCase):
    def test_matches_actual_encode_output_length(self):
        for name, (generators, k) in fec.PRESETS.items():
            bits = _random_bits(100, seed=1)
            encoded = fec.convolutional_encode(bits, generators, k)
            self.assertEqual(len(encoded), fec.coded_bit_length(len(bits), generators, k),
                              f"mismatch for preset {name}")


class RoundTripNoErrorsTests(unittest.TestCase):
    def test_all_presets_round_trip_random_data(self):
        for name, (generators, k) in fec.PRESETS.items():
            bits = _random_bits(200, seed=2)
            encoded = fec.convolutional_encode(bits, generators, k)
            decoded = fec.viterbi_decode(encoded, generators, k, len(bits))
            self.assertEqual(decoded, bits, f"round trip failed for preset {name}")

    def test_empty_input(self):
        generators, k = fec.PRESETS['r1_2_k7']
        encoded = fec.convolutional_encode([], generators, k)
        decoded = fec.viterbi_decode(encoded, generators, k, 0)
        self.assertEqual(decoded, [])

    def test_all_zeros(self):
        generators, k = fec.PRESETS['r1_2_k7']
        bits = [0] * 64
        encoded = fec.convolutional_encode(bits, generators, k)
        decoded = fec.viterbi_decode(encoded, generators, k, len(bits))
        self.assertEqual(decoded, bits)

    def test_all_ones(self):
        generators, k = fec.PRESETS['r1_2_k7']
        bits = [1] * 64
        encoded = fec.convolutional_encode(bits, generators, k)
        decoded = fec.viterbi_decode(encoded, generators, k, len(bits))
        self.assertEqual(decoded, bits)


class ViterbiDecodeValidationTests(unittest.TestCase):
    def test_rejects_wrong_length_input(self):
        generators, k = fec.PRESETS['r1_2_k7']
        with self.assertRaises(ValueError):
            fec.viterbi_decode([0, 1, 1], generators, k, num_input_bits=10)


class ErrorCorrectionTests(unittest.TestCase):
    """The actual point of FEC: verify each preset can correct some number
    of bit errors introduced after encoding, and that correction capability
    scales with code strength (stronger presets correct more)."""

    def _try_correct(self, name, num_errors, num_trials=20, seed=0):
        generators, k = fec.PRESETS[name]
        successes = 0
        rng = np.random.default_rng(seed)
        for trial in range(num_trials):
            bits = [int(b) for b in rng.integers(0, 2, 150)]
            encoded = fec.convolutional_encode(bits, generators, k)
            error_positions = rng.choice(len(encoded), size=num_errors, replace=False)
            corrupted = _flip_bits(encoded, error_positions)
            decoded = fec.viterbi_decode(corrupted, generators, k, len(bits))
            if decoded == bits:
                successes += 1
        return successes / num_trials

    def test_r1_2_k3_corrects_single_isolated_errors(self):
        rate = self._try_correct('r1_2_k3', num_errors=1)
        self.assertEqual(rate, 1.0, "a K=3 code should always correct a single isolated error")

    def test_r1_2_k7_corrects_more_errors_than_k3(self):
        # NASA-standard K=7 should reliably handle more simultaneous errors
        # than the weak textbook K=3 code (calibrated empirically - 16
        # scattered errors in 150 random bits clearly separates the two:
        # k3 ~0.67 success rate, k7 ~1.00, at seed=5)
        k3_rate = self._try_correct('r1_2_k3', num_errors=16, seed=5)
        k7_rate = self._try_correct('r1_2_k7', num_errors=16, seed=5)
        self.assertGreater(k7_rate, k3_rate)

    def test_r1_2_k9_corrects_more_than_k7(self):
        # calibrated empirically - at 25 scattered errors in 150 random
        # bits (seed=9), k7 ~0.63 success rate, k9 ~0.83
        k7_rate = self._try_correct('r1_2_k7', num_errors=25, seed=9)
        k9_rate = self._try_correct('r1_2_k9', num_errors=25, seed=9)
        self.assertGreater(k9_rate, k7_rate)

    def test_r1_3_k7_corrects_more_than_r1_2_k7(self):
        # more redundancy (rate 1/3 vs 1/2) at the same K should tolerate
        # more simultaneous errors - calibrated empirically: at 25 scattered
        # errors in 150 random bits (seed=3), r1_2 ~0.70, r1_3 ~1.00
        r1_2_rate = self._try_correct('r1_2_k7', num_errors=25, seed=3)
        r1_3_rate = self._try_correct('r1_3_k7', num_errors=25, seed=3)
        self.assertGreater(r1_3_rate, r1_2_rate)

    def test_overwhelming_errors_eventually_fail(self):
        # sanity check that FEC isn't magic - flip nearly every bit and
        # confirm decode does NOT recover the original (with this much
        # damage, "success" would indicate a test/implementation bug)
        generators, k = fec.PRESETS['r1_2_k7']
        bits = _random_bits(100, seed=11)
        encoded = fec.convolutional_encode(bits, generators, k)
        # flip 45% of coded bits - far beyond any reasonable code's capability
        rng = np.random.default_rng(11)
        num_errors = int(len(encoded) * 0.45)
        error_positions = rng.choice(len(encoded), size=num_errors, replace=False)
        corrupted = _flip_bits(encoded, error_positions)
        decoded = fec.viterbi_decode(corrupted, generators, k, len(bits))
        self.assertNotEqual(decoded, bits)


class InterleaverTests(unittest.TestCase):
    def test_every_scheme_is_a_permutation(self):
        for scheme in fec.INTERLEAVERS:
            for n in (0, 1, 2, 7, 64, 524, 1000, 1023, 4104, 16392):
                with self.subTest(scheme=scheme, n=n):
                    perm = fec.interleave_permutation(n, scheme)
                    self.assertEqual(sorted(perm.tolist()), list(range(n)))

    def test_none_is_identity_and_unknown_rejected(self):
        self.assertEqual(fec.interleave_permutation(10, 'none').tolist(), list(range(10)))
        with self.assertRaises(ValueError):
            fec.interleave_permutation(10, 'random')

    def test_round_trip(self):
        bits = np.random.default_rng(3).integers(0, 2, 1501).tolist()
        for scheme in fec.INTERLEAVERS:
            with self.subTest(scheme=scheme):
                self.assertEqual(fec.deinterleave_bits(fec.interleave_bits(bits, scheme), scheme), bits)

    def test_golden_spreads_channel_bursts(self):
        # Any 8 consecutive channel bits come from code positions far apart -
        # here at least 25 for frame-sized blocks (a K=7 code's memory spans
        # ~14 coded bits at rate 1/2).
        for n in (524, 1032, 4104):
            perm = fec.interleave_permutation(n, 'golden')
            spacing = min(abs(int(perm[j + a]) - int(perm[j + b]))
                          for j in range(n - 8) for a in range(8) for b in range(a + 1, 8))
            self.assertGreaterEqual(spacing, 25, n)

    def _burst_frame(self, interleaver, burst_bits=48):
        """Encode a frame, then flip a contiguous run of burst_bits channel
        bits in the middle of its payload block - an FM fade's worth."""
        generators, k = fec.PRESETS['r1_2_k7']
        payload = bytes(range(64))
        frame = link.build_frame(payload, seq=4)
        encoded = fec.encode_frame(frame, generators, k, interleaver=interleaver)
        bits = modem.bytes_to_bits(encoded)
        prefix_bits = (24 + len(link.PREAMBLE) + len(link.SYNC_WORD)) * 8
        header_block_bits = fec._coded_byte_length(link.HEADER_LEN * 8, generators, k) * 8
        start = prefix_bits + header_block_bits + 200
        for i in range(start, start + burst_bits):
            bits[i] ^= 1
        return modem.bits_to_bytes(bits), generators, k, payload

    def test_golden_corrects_a_burst_that_defeats_no_interleaving(self):
        corrupted, generators, k, payload = self._burst_frame('golden')
        status, _, decoded = _decode_first(corrupted, generators, k, interleaver='golden')
        self.assertEqual((status, decoded), ('ok', payload))

        corrupted, generators, k, _ = self._burst_frame('none')
        status, _, _ = _decode_first(corrupted, generators, k, interleaver='none')
        self.assertEqual(status, 'crc_fail')

    def test_mismatched_interleaver_fails_cleanly(self):
        generators, k = fec.PRESETS['r1_2_k7']
        frame = link.build_frame(b'mismatch', seq=1)
        encoded = fec.encode_frame(frame, generators, k, interleaver='golden')
        status, _, _ = _decode_first(encoded, generators, k, interleaver='none')
        self.assertNotEqual(status, 'ok')

    def test_golden_is_the_default(self):
        self.assertEqual(fec.DEFAULT_INTERLEAVER, 'golden')
        generators, k = fec.PRESETS['r1_2_k7']
        frame = link.build_frame(b'default', seq=1)
        self.assertEqual(fec.encode_frame(frame, generators, k),
                         fec.encode_frame(frame, generators, k, interleaver='golden'))


class KnownMarkerPositionTests(unittest.TestCase):
    """decode_frames(first_marker_bit=...): decode at a marker position the
    caller already found (e.g. by sample-domain correlation) instead of
    re-searching for it in hard bits."""

    def setUp(self):
        self.generators, self.k = fec.PRESETS['r1_2_k7']
        self.payload = b'marker under a fade'
        frame = link.build_frame(self.payload, seq=2)
        self.bits = modem.bytes_to_bits(fec.encode_frame(frame, self.generators, self.k))
        self.marker_bit = 24 * 8  # build_frame()'s default lead pad

    def damaged(self, fraction):
        """Flip a contiguous run covering `fraction` of the marker's bits."""
        bits = list(self.bits)
        for i in range(self.marker_bit, self.marker_bit + int(fraction * sync.MARKER_LEN)):
            bits[i] ^= 1
        return modem.bits_to_bytes(bits)

    def test_recovers_frame_whose_marker_is_too_damaged_to_find(self):
        data = self.damaged(0.3)  # well past DEFAULT_MAX_ERROR_FRACTION (15%)
        searched = list(fec.decode_frames(data, self.generators, self.k))
        self.assertFalse(any(status == 'ok' for status, _, _ in searched))
        status, _, payload = _decode_first(data, self.generators, self.k,
                                           first_marker_bit=self.marker_bit)
        self.assertEqual((status, payload), ('ok', self.payload))

    def test_same_result_as_search_on_a_clean_frame(self):
        data = modem.bits_to_bytes(self.bits)
        self.assertEqual(list(fec.decode_frames(data, self.generators, self.k,
                                                first_marker_bit=self.marker_bit)),
                         list(fec.decode_frames(data, self.generators, self.k)))

    def test_later_frames_are_still_searched_for(self):
        frames, _ = link.build_fragmented_frames(b'x' * 300, seq_start=1, chunk_size=100)
        data = fec.encode_frame(b''.join(frames), self.generators, self.k)
        statuses = [status for status, _, _ in
                    fec.decode_frames(data, self.generators, self.k, first_marker_bit=self.marker_bit)]
        self.assertEqual(statuses, ['ok'] * len(frames))

    def test_wrong_position_fails_cleanly(self):
        data = modem.bits_to_bytes(self.bits)
        results = list(fec.decode_frames(data, self.generators, self.k,
                                         first_marker_bit=self.marker_bit + 5))
        self.assertNotEqual(results[0][0], 'ok')


class BitErrorCountTests(unittest.TestCase):
    def test_identical_is_zero(self):
        self.assertEqual(fec.bit_error_count([1, 0, 1], [1, 0, 1]), 0)

    def test_counts_differences(self):
        self.assertEqual(fec.bit_error_count([1, 0, 1, 0], [0, 0, 0, 0]), 2)


class EncodeFrameTests(unittest.TestCase):
    def test_output_is_longer_than_input(self):
        generators, k = fec.PRESETS['r1_2_k7']
        frame = link.build_frame(b'hello world', seq=1, src_id=b'K0MDT', dst_id=b'W1AW')
        encoded = fec.encode_frame(frame, generators, k)
        self.assertGreater(len(encoded), len(frame))

    def test_prefix_and_suffix_unchanged(self):
        generators, k = fec.PRESETS['r1_2_k7']
        frame = link.build_frame(b'hello', seq=1, lead_pad=24, end_pad=24)
        encoded = fec.encode_frame(frame, generators, k)
        marker = link.PREAMBLE + link.SYNC_WORD
        prefix_len = frame.index(marker) + len(marker)
        self.assertEqual(encoded[:prefix_len], frame[:prefix_len])
        self.assertTrue(encoded.endswith(b'\x00' * 24))

    def test_rejects_data_without_marker(self):
        generators, k = fec.PRESETS['r1_2_k7']
        with self.assertRaises(ValueError):
            fec.encode_frame(b'not a real frame at all', generators, k)


class MultiFragmentTests(unittest.TestCase):
    """The actual bug this session's testing surfaced: an earlier
    encode_frame()/decode_received() only ever handled the FIRST frame in
    a build_fragmented_frames() message, silently treating every fragment
    past it as harmless unprotected padding - a large/fragmented message
    would report a clean 'ok' decode while actually only recovering
    fragment 1. These tests exercise real multi-fragment messages end to
    end through encode_frame()/decode_frames() and reassembly."""

    def test_encode_frame_protects_every_fragment_not_just_the_first(self):
        generators, k = fec.PRESETS['r1_2_k7']
        payload = b'Y' * 5000  # needs 5 fragments at MAX_PAYLOAD_LEN=1024
        frames, _ = link.build_fragmented_frames(payload, seq_start=0)
        self.assertEqual(len(frames), 5)
        data = b''.join(frames)

        encoded = fec.encode_frame(data, generators, k)
        results = list(fec.decode_frames(encoded, generators, k))

        self.assertEqual(len(results), 5, "every fragment should be found and decoded")
        self.assertTrue(all(status == 'ok' for status, _, _ in results))
        self.assertEqual([h['fragment_index'] for _, h, _ in results], [0, 1, 2, 3, 4])
        self.assertEqual(b''.join(p for _, _, p in results), payload)

    def test_decode_frames_yields_one_result_per_fragment(self):
        generators, k = fec.PRESETS['r1_2_k7']
        frames, _ = link.build_fragmented_frames(b'Z' * 3000, seq_start=0)
        self.assertGreater(len(frames), 1)
        encoded = fec.encode_frame(b''.join(frames), generators, k)
        results = list(fec.decode_frames(encoded, generators, k))
        self.assertEqual(len(results), len(frames))

    def test_full_reassembly_via_linkreceiver_recovers_complete_message(self):
        """The end-to-end proof: fec_decode.py rebuilds each recovered
        fragment as a standard frame (see fec_decode.py), and feeding that
        stream through the normal LinkReceiver/FragmentReassembler path
        (exactly what openwave_cli.py recv does) must recover the FULL
        original message, not just the first fragment."""
        generators, k = fec.PRESETS['r1_2_k7']
        original = b'The quick brown fox jumps over the lazy dog. ' * 150  # several KB
        frames, _ = link.build_fragmented_frames(original, seq_start=0, src_id=b'K0MDT', dst_id=b'W1AW')
        self.assertGreater(len(frames), 1)
        encoded = fec.encode_frame(b''.join(frames), generators, k)

        # mirror fec_decode.py: decode every fragment, rebuild each as a
        # standard frame, concatenate
        rebuilt = bytearray()
        for status, header, payload in fec.decode_frames(encoded, generators, k):
            self.assertEqual(status, 'ok')
            rebuilt += link.build_frame(
                payload, lead_pad=0, end_pad=0, version=header['version'],
                packet_type=header['packet_type'], mode_id=header['mode_id'],
                flags=header['flags'], seq=header['seq'], fragment_index=header['fragment_index'],
                src_id=header['src_id'].rstrip(b'\x00'), dst_id=header['dst_id'].rstrip(b'\x00'),
            )

        messages = []
        receiver = link.LinkReceiver(on_message=messages.append)
        for status, header, payload in link.iter_frames(bytes(rebuilt)):
            if status == 'ok':
                receiver.feed_frame(header, payload)
            else:
                receiver.feed_crc_failure(status)

        self.assertEqual(messages, [original.decode('utf-8')])
        self.assertEqual(receiver.counts['frag_discarded'], 0)
        self.assertEqual(receiver.counts['frag_aborted'], 0)

    def test_bit_errors_in_a_later_fragment_are_corrected_too(self):
        """Specifically targets what the old single-frame code got wrong:
        corrupting a fragment OTHER than the first must still be
        correctable, proving later fragments are genuinely FEC-protected
        (not silently passed through raw)."""
        generators, k = fec.PRESETS['r1_2_k7']
        payload = b'W' * 3000  # 3 fragments
        frames, _ = link.build_fragmented_frames(payload, seq_start=0)
        self.assertEqual(len(frames), 3)
        encoded = bytearray(fec.encode_frame(b''.join(frames), generators, k))

        # find the SECOND fragment's marker and corrupt bits well past it
        marker = link.PREAMBLE + link.SYNC_WORD
        first_marker_end = encoded.index(marker) + len(marker)
        second_marker_start = encoded.index(marker, first_marker_end)

        bits = modem.bytes_to_bits(bytes(encoded))
        rng = np.random.default_rng(5)
        # corrupt starting partway into the second fragment's coded region
        start_bit = (second_marker_start + len(marker)) * 8
        positions = rng.choice(range(start_bit, len(bits)), size=10, replace=False)
        for pos in positions:
            bits[pos] ^= 1
        corrupted = modem.bits_to_bytes(bits)

        results = list(fec.decode_frames(corrupted, generators, k))
        self.assertEqual(len(results), 3)
        self.assertTrue(all(status == 'ok' for status, _, _ in results),
                         f"statuses: {[r[0] for r in results]}")
        self.assertEqual(b''.join(p for _, _, p in results), payload)


class SyncGapTests(unittest.TestCase):
    """decode_frames() must explicitly report a 'gap' when a fragment's
    marker is too corrupted to find nearby, instead of the old silent
    behavior: an unbounded search just locked onto whatever real marker
    came next, however far away, with no indication a fragment in between
    was lost. Found via a 3dB/100-iteration FEC sweep across presets whose
    pass rates didn't track code strength the way they should have - a
    direct repro showed decode_frames() yielding fragment seq 0, 2, 3, 4,
    every one reporting 'ok', with seq 1 simply missing and no error at
    all. See openwave_sync.FRAGMENT_GAP_SEARCH_BITS."""

    def test_fully_corrupted_marker_reports_gap_and_later_fragments_still_recovered(self):
        generators, k = fec.PRESETS['r1_2_k7']
        payload = b'G' * 3000  # 3 fragments
        frames, _ = link.build_fragmented_frames(payload, seq_start=0)
        self.assertEqual(len(frames), 3)
        encoded = bytearray(fec.encode_frame(b''.join(frames), generators, k))

        # find the SECOND fragment's marker and obliterate it (well past
        # the 15% Hamming-distance tolerance) - unlike the sibling test in
        # MultiFragmentTests, which corrupts data AFTER the marker to prove
        # Viterbi correction, this corrupts the marker ITSELF so it can't
        # be found at all
        marker = link.PREAMBLE + link.SYNC_WORD
        first_marker_end = encoded.index(marker) + len(marker)
        second_marker_start = encoded.index(marker, first_marker_end)
        for i in range(second_marker_start, second_marker_start + len(marker)):
            encoded[i] ^= 0xFF

        results = list(fec.decode_frames(bytes(encoded), generators, k))
        statuses = [status for status, _, _ in results]
        self.assertEqual(statuses, ['ok', 'gap', 'ok'],
                          "second fragment's marker is unrecoverable and must be "
                          "reported as a gap, not silently absorbed")
        self.assertEqual(results[0][2], payload[:link.MAX_PAYLOAD_LEN])
        self.assertEqual(results[2][2], payload[2 * link.MAX_PAYLOAD_LEN:])

    def test_first_fragments_marker_destroyed_also_reports_gap(self):
        """The very first marker is now searched for the same bounded-then-
        fallback way as every subsequent one - a real 3dB repro showed the
        FIRST fragment's marker getting silently skipped (decode_frames()
        yielding seq 1, 2, 3, 4 with no seq 0 and no error at all), which
        an earlier version of this fix missed entirely: it only bounded
        the search after an initial frame had already synced, since a
        truly free-floating first search has no well-defined 'nearby'.
        This protocol's fixed lead_pad makes that distinction not worth
        keeping - the first marker should be found just as easily whether
        or not anything synced before it."""
        generators, k = fec.PRESETS['r1_2_k7']
        payload = b'F' * 3000  # 3 fragments
        frames, _ = link.build_fragmented_frames(payload, seq_start=0)
        self.assertEqual(len(frames), 3)
        encoded = bytearray(fec.encode_frame(b''.join(frames), generators, k))

        marker = link.PREAMBLE + link.SYNC_WORD
        first_marker_start = encoded.index(marker)
        for i in range(first_marker_start, first_marker_start + len(marker)):
            encoded[i] ^= 0xFF

        results = list(fec.decode_frames(bytes(encoded), generators, k))
        statuses = [status for status, _, _ in results]
        self.assertEqual(statuses, ['gap', 'ok', 'ok'])
        self.assertEqual(results[1][2], payload[link.MAX_PAYLOAD_LEN:2 * link.MAX_PAYLOAD_LEN])
        self.assertEqual(results[2][2], payload[2 * link.MAX_PAYLOAD_LEN:])

    def test_no_gap_reported_for_a_clean_multi_fragment_decode(self):
        generators, k = fec.PRESETS['r1_2_k7']
        payload = b'H' * 3000
        frames, _ = link.build_fragmented_frames(payload, seq_start=0)
        encoded = fec.encode_frame(b''.join(frames), generators, k)
        statuses = [status for status, _, _ in fec.decode_frames(encoded, generators, k)]
        self.assertEqual(statuses, ['ok', 'ok', 'ok'])


class DecodeReceivedNoErrorsTests(unittest.TestCase):
    def test_clean_round_trip_all_presets(self):
        for name, (generators, k) in fec.PRESETS.items():
            frame = link.build_frame(b'hello world', seq=5, mode_id=2,
                                      src_id=b'K0MDT', dst_id=b'W1AW')
            encoded = fec.encode_frame(frame, generators, k)
            status, header, payload = _decode_first(encoded, generators, k)
            self.assertEqual(status, 'ok', f"failed for preset {name}")
            self.assertEqual(payload, b'hello world')
            self.assertEqual(header['seq'], 5)
            self.assertEqual(header['mode_id'], 2)

    def test_no_sync_on_garbage(self):
        generators, k = fec.PRESETS['r1_2_k7']
        status, header, payload = _decode_first(b'garbage' * 20, generators, k)
        self.assertEqual(status, 'no_sync')

    def test_truncated_data(self):
        generators, k = fec.PRESETS['r1_2_k7']
        frame = link.build_frame(b'hello world', seq=1)
        encoded = fec.encode_frame(frame, generators, k)
        marker_len = len(link.PREAMBLE) + len(link.SYNC_WORD)
        marker_end = encoded.index(link.PREAMBLE + link.SYNC_WORD) + marker_len
        truncated = encoded[:marker_end + 3]  # cut off almost immediately after sync
        status, header, payload = _decode_first(truncated, generators, k)
        self.assertEqual(status, 'truncated')


class DecodeReceivedErrorCorrectionTests(unittest.TestCase):
    """The actual point: simulate real channel-induced bit errors on an
    encoded frame and confirm FEC recovers the original where an uncoded
    frame would have failed CRC."""

    def test_corrects_scattered_bit_errors_in_payload(self):
        generators, k = fec.PRESETS['r1_2_k7']
        frame = link.build_frame(b'this message should survive some noise', seq=3,
                                  src_id=b'K0MDT', dst_id=b'W1AW')
        encoded = bytearray(fec.encode_frame(frame, generators, k))

        # flip a handful of scattered bits somewhere past the sync word
        # (i.e. within the FEC-coded region)
        marker_end = encoded.index(link.PREAMBLE + link.SYNC_WORD) + len(link.PREAMBLE) + len(link.SYNC_WORD)
        rng = np.random.default_rng(42)
        coded_region_bits = (len(encoded) - marker_end) * 8
        error_bit_positions = rng.choice(coded_region_bits, size=10, replace=False)
        bits = modem.bytes_to_bits(bytes(encoded))
        offset_bits = marker_end * 8
        for pos in error_bit_positions:
            bits[offset_bits + pos] ^= 1
        corrupted = modem.bits_to_bytes(bits)

        status, header, payload = _decode_first(corrupted, generators, k)
        self.assertEqual(status, 'ok')
        self.assertEqual(payload, b'this message should survive some noise')

    def test_stronger_code_survives_where_weaker_fails(self):
        """Same corruption pattern applied to both a weak (K=3) and strong
        (K=7) encoding of the same frame - the weak one should fail more
        often. This is exactly the kind of comparison the project needs to
        actually choose a configuration."""
        frame = link.build_frame(b'compare code strength here please', seq=7)
        marker_end = frame.index(link.PREAMBLE + link.SYNC_WORD) + len(link.PREAMBLE) + len(link.SYNC_WORD)

        def trial(preset_name, seed):
            generators, k = fec.PRESETS[preset_name]
            encoded = fec.encode_frame(frame, generators, k)
            coded_region_start_bit = marker_end * 8  # same prefix length regardless of preset
            bits = modem.bytes_to_bits(encoded)
            rng = np.random.default_rng(seed)
            # calibrated empirically: at 60 scattered errors across this
            # frame's coded region, k3 succeeds ~8/15 trials, k7 ~15/15
            num_errors = 60
            positions = rng.choice(len(bits) - coded_region_start_bit, size=num_errors, replace=False)
            for pos in positions:
                bits[coded_region_start_bit + pos] ^= 1
            corrupted = modem.bits_to_bytes(bits)
            status, header, payload = _decode_first(corrupted, generators, k)
            return status == 'ok' and payload == b'compare code strength here please'

        k3_successes = sum(trial('r1_2_k3', seed) for seed in range(15))
        k7_successes = sum(trial('r1_2_k7', seed) for seed in range(15))
        self.assertGreater(k7_successes, k3_successes)

    def test_uncorrectable_errors_report_crc_fail_not_silent_garbage(self):
        generators, k = fec.PRESETS['r1_2_k3']  # weak code, easy to overwhelm
        frame = link.build_frame(b'too much noise for this weak code', seq=1)
        encoded = fec.encode_frame(frame, generators, k)
        marker_end = encoded.index(link.PREAMBLE + link.SYNC_WORD) + len(link.PREAMBLE) + len(link.SYNC_WORD)
        bits = modem.bytes_to_bits(encoded)
        rng = np.random.default_rng(1)
        coded_region_bits = len(bits) - marker_end * 8
        num_errors = int(coded_region_bits * 0.35)
        positions = rng.choice(coded_region_bits, size=num_errors, replace=False)
        for pos in positions:
            bits[marker_end * 8 + pos] ^= 1
        corrupted = modem.bits_to_bytes(bits)

        status, header, payload = _decode_first(corrupted, generators, k)
        # with this much damage we expect either crc_fail or a garbled
        # payload under a different status - what we must NOT see is a
        # false 'ok' with wrong content (that would mean CRC re-verification
        # isn't actually catching miscorrection, a real bug)
        if status == 'ok':
            self.assertEqual(payload, b'too much noise for this weak code')


if __name__ == '__main__':
    unittest.main()
