"""
Unit tests for openwave_link.py, the shared link-layer framing module used by
phy_sim_loopback.grc, tx_pluto.grc, and rx_rtlsdr.grc's epy_blocks.

Pure Python, no GNU Radio dependency - fast, run first.

    python3 -m unittest tests.test_openwave_link -v
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import openwave_link as link


class HeaderRoundTripTests(unittest.TestCase):
    def test_default_header_round_trips(self):
        header = link.build_header(payload_len=10)
        parsed = link.parse_header(header)
        self.assertEqual(parsed['version'], link.VERSION)
        self.assertEqual(parsed['packet_type'], link.PACKET_TYPE_DATA)
        self.assertEqual(parsed['payload_len'], 10)
        self.assertEqual(parsed['seq'], 0)
        self.assertEqual(parsed['flags'], 0)

    def test_header_is_exactly_32_bytes(self):
        self.assertEqual(len(link.build_header(0)), link.HEADER_LEN)
        self.assertEqual(len(link.build_header(link.MAX_PAYLOAD_LEN)), link.HEADER_LEN)

    def test_all_fields_round_trip(self):
        header = link.build_header(
            payload_len=42, version=3, packet_type=link.PACKET_TYPE_ACK,
            mode_id=2, flags=link.FLAG_ACK_REQUESTED | link.FLAG_PRIORITY,
            seq=1234, src_id=b'K0MDT', dst_id=b'W1AW',
        )
        parsed = link.parse_header(header)
        self.assertEqual(parsed['version'], 3)
        self.assertEqual(parsed['packet_type'], link.PACKET_TYPE_ACK)
        self.assertEqual(parsed['mode_id'], 2)
        self.assertEqual(parsed['flags'], link.FLAG_ACK_REQUESTED | link.FLAG_PRIORITY)
        self.assertEqual(parsed['seq'], 1234)
        self.assertEqual(parsed['src_id'], b'K0MDT\x00\x00\x00')
        self.assertEqual(parsed['dst_id'], b'W1AW\x00\x00\x00\x00')
        self.assertEqual(parsed['payload_len'], 42)

    def test_src_dst_id_truncated_to_8_bytes(self):
        header = link.build_header(0, src_id=b'TOOLONGCALL', dst_id=b'ALSOTOOLONG')
        parsed = link.parse_header(header)
        self.assertEqual(parsed['src_id'], b'TOOLONGC')
        self.assertEqual(parsed['dst_id'], b'ALSOTOOL')

    def test_payload_len_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            link.build_header(-1)
        with self.assertRaises(ValueError):
            link.build_header(link.MAX_PAYLOAD_LEN + 1)

    def test_parse_header_wrong_length_rejected(self):
        with self.assertRaises(ValueError):
            link.parse_header(b'\x00' * (link.HEADER_LEN - 1))
        with self.assertRaises(ValueError):
            link.parse_header(b'\x00' * (link.HEADER_LEN + 1))

    def test_seq_wraps_at_16_bits(self):
        header = link.build_header(0, seq=link.SEQ_MODULUS + 5)
        self.assertEqual(link.parse_header(header)['seq'], 5)

    def test_seq_wraps_for_negative_input(self):
        # a caller decrementing past 0 shouldn't blow up building the header
        header = link.build_header(0, seq=-1)
        self.assertEqual(link.parse_header(header)['seq'], link.SEQ_MODULUS - 1)


class FlagsTests(unittest.TestCase):
    def test_no_flags(self):
        self.assertEqual(link.format_flags(0), '-')
        self.assertFalse(any(link.parse_flags(0).values()))

    def test_single_flag(self):
        parsed = link.parse_flags(link.FLAG_ACK_REQUESTED)
        self.assertTrue(parsed['ack_requested'])
        self.assertFalse(parsed['priority'])
        self.assertEqual(link.format_flags(link.FLAG_ACK_REQUESTED), 'ack_requested')

    def test_multiple_flags(self):
        combo = link.FLAG_ACK_REQUESTED | link.FLAG_PRIORITY
        parsed = link.parse_flags(combo)
        self.assertTrue(parsed['ack_requested'])
        self.assertTrue(parsed['priority'])
        self.assertFalse(parsed['fragmented'])
        formatted = link.format_flags(combo)
        self.assertIn('ack_requested', formatted)
        self.assertIn('priority', formatted)

    def test_reserved_bits_ignored(self):
        # bits 5-7 (0xE0) aren't named - shouldn't register as any known flag
        self.assertFalse(any(link.parse_flags(0xE0).values()))
        self.assertEqual(link.format_flags(0xE0), '-')


class ModeAndStationIdTests(unittest.TestCase):
    def test_format_mode_known(self):
        self.assertEqual(link.format_mode(0), 'Mode 0 (600 sym/s)')
        self.assertEqual(link.format_mode(4), 'Mode 4 (9600 sym/s)')

    def test_format_mode_unknown(self):
        self.assertEqual(link.format_mode(99), 'Mode 99 (unknown)')

    def test_decode_station_id_strips_padding(self):
        header = link.build_header(0, src_id=b'K0MDT', dst_id=b'W1AW')
        parsed = link.parse_header(header)
        self.assertEqual(link.decode_station_id(parsed['src_id']), 'K0MDT')
        self.assertEqual(link.decode_station_id(parsed['dst_id']), 'W1AW')

    def test_decode_station_id_empty_placeholder(self):
        header = link.build_header(0)  # default dst_id=b'' -> all-zero field
        parsed = link.parse_header(header)
        self.assertEqual(link.decode_station_id(parsed['dst_id']), '')

    def test_is_for_station_unconfigured_accepts_everything(self):
        header = link.build_header(0, dst_id=b'W1AW')
        self.assertTrue(link.is_for_station(link.parse_header(header)['dst_id'], ''))

    def test_is_for_station_matches_own_callsign(self):
        header = link.build_header(0, dst_id=b'K0MDT')
        dst = link.parse_header(header)['dst_id']
        self.assertTrue(link.is_for_station(dst, 'K0MDT'))
        self.assertFalse(link.is_for_station(dst, 'W1AW'))

    def test_is_for_station_accepts_broadcast(self):
        header = link.build_header(0, dst_id=b'ALL')
        dst = link.parse_header(header)['dst_id']
        self.assertTrue(link.is_for_station(dst, 'K0MDT'))

    def test_is_for_station_accepts_placeholder_dst_even_when_configured(self):
        # today's TX chains all send an all-zero dst_id - an addressed
        # receiver must still hear them, not go silent because of it.
        header = link.build_header(0)  # dst_id defaults to b''
        dst = link.parse_header(header)['dst_id']
        self.assertTrue(link.is_for_station(dst, 'K0MDT'))


class FrameBuildAndParseTests(unittest.TestCase):
    def _header_start(self, frame):
        idx = frame.find(link.SYNC_WORD)
        self.assertNotEqual(idx, -1, "sync word not found in frame")
        return idx + len(link.SYNC_WORD)

    def _feed_all(self, parser, data):
        status, payload = 'incomplete', None
        for b in data:
            status, payload = parser.feed(b)
            if status != 'incomplete':
                break
        return status, payload

    def test_clean_round_trip(self):
        payload = b'Hello OpenWave!'
        frame = link.build_frame(payload, lead_pad=4, end_pad=4)
        start = self._header_start(frame)
        status, result = self._feed_all(link.FrameParser(), frame[start:])
        self.assertEqual(status, 'ok')
        self.assertEqual(result, payload)

    def test_empty_payload_round_trips(self):
        frame = link.build_frame(b'', lead_pad=2, end_pad=2)
        start = self._header_start(frame)
        status, result = self._feed_all(link.FrameParser(), frame[start:])
        self.assertEqual(status, 'ok')
        self.assertEqual(result, b'')

    def test_max_payload_round_trips(self):
        payload = b'X' * link.MAX_PAYLOAD_LEN
        frame = link.build_frame(payload, lead_pad=2, end_pad=2)
        start = self._header_start(frame)
        status, result = self._feed_all(link.FrameParser(), frame[start:])
        self.assertEqual(status, 'ok')
        self.assertEqual(result, payload)

    def test_over_max_payload_rejected(self):
        with self.assertRaises(ValueError):
            link.build_frame(b'X' * (link.MAX_PAYLOAD_LEN + 1))

    def test_seq_and_flags_survive_round_trip(self):
        frame = link.build_frame(b'hi', seq=999, flags=link.FLAG_ACK_REQUESTED)
        start = self._header_start(frame)
        parser = link.FrameParser()
        status, result = self._feed_all(parser, frame[start:])
        self.assertEqual(status, 'ok')
        self.assertEqual(parser.last_header['seq'], 999)
        self.assertTrue(link.parse_flags(parser.last_header['flags'])['ack_requested'])

    def test_corrupted_header_byte_detected(self):
        frame = bytearray(link.build_frame(b'Hello OpenWave!'))
        start = self._header_start(bytes(frame))
        frame[start + 5] ^= 0xFF  # flip a bit inside the header
        status, result = self._feed_all(link.FrameParser(), bytes(frame[start:]))
        self.assertIn(status, ('crc_fail', 'bad_length'))
        self.assertIsNone(result)

    def test_corrupted_payload_byte_fails_crc(self):
        payload = b'Hello OpenWave!'
        frame = bytearray(link.build_frame(payload))
        start = self._header_start(bytes(frame))
        frame[start + link.HEADER_LEN + 3] ^= 0xFF  # inside the payload, not the header
        status, result = self._feed_all(link.FrameParser(), bytes(frame[start:]))
        self.assertEqual(status, 'crc_fail')
        self.assertIsNone(result)

    def test_corrupted_crc_trailer_detected(self):
        payload = b'Hello OpenWave!'
        frame = bytearray(link.build_frame(payload))
        start = self._header_start(bytes(frame))
        frame[start + link.HEADER_LEN + len(payload)] ^= 0xFF  # first CRC byte
        status, result = self._feed_all(link.FrameParser(), bytes(frame[start:]))
        self.assertEqual(status, 'crc_fail')
        self.assertIsNone(result)

    def test_truncated_frame_stays_incomplete(self):
        frame = link.build_frame(b'Hello OpenWave!')
        start = self._header_start(frame)
        parser = link.FrameParser()
        status, result = None, None
        for b in frame[start:start + link.HEADER_LEN - 1]:
            status, result = parser.feed(b)
        self.assertEqual(status, 'incomplete')
        self.assertIsNone(result)

    def test_reset_clears_partial_state(self):
        frame = link.build_frame(b'Hello OpenWave!')
        start = self._header_start(frame)
        parser = link.FrameParser()
        for b in frame[start:start + 10]:
            parser.feed(b)
        parser.reset()
        self.assertEqual(len(parser.buf), 0)
        self.assertIsNone(parser.last_header)
        # a fresh frame should still parse correctly after reset
        status, result = self._feed_all(parser, frame[start:])
        self.assertEqual(status, 'ok')

    def test_lead_and_end_pad_dont_affect_payload(self):
        payload = b'padding shouldn\'t matter'
        for lead, end in [(0, 0), (1, 1), (48, 48)]:
            frame = link.build_frame(payload, lead_pad=lead, end_pad=end)
            start = self._header_start(frame)
            status, result = self._feed_all(link.FrameParser(), frame[start:])
            self.assertEqual(status, 'ok', f"failed for lead_pad={lead} end_pad={end}")
            self.assertEqual(result, payload)


class BuildFrameWithBadLengthTests(unittest.TestCase):
    def _header_start(self, frame):
        return frame.index(link.PREAMBLE + link.SYNC_WORD) + len(link.PREAMBLE) + len(link.SYNC_WORD)

    def test_declared_length_over_max_rejected_by_parser(self):
        frame = link.build_frame_with_bad_length(b'hi', declared_length=link.MAX_PAYLOAD_LEN + 500)
        parser = link.FrameParser()
        start = self._header_start(frame)
        status = None
        for b in frame[start:start + link.HEADER_LEN]:
            status, _ = parser.feed(b)
        self.assertEqual(status, 'bad_length')

    def test_declared_length_visible_in_parsed_header_before_rejection(self):
        frame = link.build_frame_with_bad_length(b'hi', declared_length=9999)
        parser = link.FrameParser()
        start = self._header_start(frame)
        for b in frame[start:start + link.HEADER_LEN]:
            status, _ = parser.feed(b)
        self.assertEqual(status, 'bad_length')
        self.assertEqual(parser.last_header['payload_len'], 9999)

    def test_iter_frames_reports_bad_length(self):
        frame = link.build_frame_with_bad_length(b'hi', declared_length=5000)
        status, header, payload = next(link.iter_frames(frame))
        self.assertEqual(status, 'bad_length')
        self.assertIsNone(header)
        self.assertIsNone(payload)

    def test_describe_frames_reports_bad_length_with_header(self):
        frame = link.build_frame_with_bad_length(b'hi', declared_length=5000, mode_id=1)
        [result] = list(link.describe_frames(frame))
        self.assertEqual(result['status'], 'bad_length')
        self.assertEqual(result['header']['payload_len'], 5000)
        self.assertIsNone(result['payload'])


class AckFrameTests(unittest.TestCase):
    def _header_start(self, frame):
        idx = frame.find(link.SYNC_WORD)
        self.assertNotEqual(idx, -1)
        return idx + len(link.SYNC_WORD)

    def _feed_all(self, parser, data):
        status, payload = 'incomplete', None
        for b in data:
            status, payload = parser.feed(b)
            if status != 'incomplete':
                break
        return status, payload

    def test_build_ack_frame_has_correct_type_flags_and_seq(self):
        frame = link.build_ack_frame(acked_seq=42)
        start = self._header_start(frame)
        header = link.parse_header(frame[start:start + link.HEADER_LEN])
        self.assertEqual(header['packet_type'], link.PACKET_TYPE_ACK)
        self.assertTrue(link.parse_flags(header['flags'])['ack_response'])
        self.assertEqual(header['seq'], 42)
        self.assertEqual(header['payload_len'], 0)

    def test_ack_frame_round_trips_through_frame_parser(self):
        frame = link.build_ack_frame(acked_seq=7)
        start = self._header_start(frame)
        parser = link.FrameParser()
        status, payload = self._feed_all(parser, frame[start:])
        self.assertEqual(status, 'ok')
        self.assertEqual(payload, b'')
        self.assertTrue(link.is_ack(parser.last_header))
        self.assertFalse(link.needs_ack(parser.last_header))

    def test_needs_ack_true_for_data_with_flag_set(self):
        header = link.parse_header(link.build_header(
            0, packet_type=link.PACKET_TYPE_DATA, flags=link.FLAG_ACK_REQUESTED))
        self.assertTrue(link.needs_ack(header))
        self.assertFalse(link.is_ack(header))

    def test_needs_ack_false_without_flag(self):
        header = link.parse_header(link.build_header(0, packet_type=link.PACKET_TYPE_DATA, flags=0))
        self.assertFalse(link.needs_ack(header))

    def test_needs_ack_false_for_ack_packet_itself(self):
        # even if (nonsensically) ack_requested were also set on an ACK packet,
        # needs_ack should only ever fire for Data packets
        header = link.parse_header(link.build_header(
            0, packet_type=link.PACKET_TYPE_ACK,
            flags=link.FLAG_ACK_REQUESTED | link.FLAG_ACK_RESPONSE))
        self.assertFalse(link.needs_ack(header))
        self.assertTrue(link.is_ack(header))


class _FakeClock:
    """Manually-advanceable clock for RetryManager tests, so timeout behavior
    is tested without any real wall-clock sleeps."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class RetryManagerTests(unittest.TestCase):
    def test_idle_when_nothing_pending(self):
        rm = link.RetryManager()
        self.assertEqual(rm.poll(), 'idle')
        self.assertFalse(rm.is_pending)

    def test_waiting_before_timeout(self):
        clock = _FakeClock()
        rm = link.RetryManager(timeout_s=1.0, time_func=clock)
        rm.note_sent(seq=5)
        clock.advance(0.5)
        self.assertEqual(rm.poll(), 'waiting')

    def test_retry_after_timeout(self):
        clock = _FakeClock()
        rm = link.RetryManager(timeout_s=1.0, max_retries=3, time_func=clock)
        rm.note_sent(seq=5)
        clock.advance(1.5)
        self.assertEqual(rm.poll(), 'retry')

    def test_matching_ack_resolves_and_returns_to_idle(self):
        clock = _FakeClock()
        rm = link.RetryManager(timeout_s=1.0, time_func=clock)
        rm.note_sent(seq=5)
        self.assertTrue(rm.note_ack(5))
        self.assertFalse(rm.is_pending)
        self.assertEqual(rm.poll(), 'idle')

    def test_mismatched_ack_does_not_resolve(self):
        clock = _FakeClock()
        rm = link.RetryManager(timeout_s=1.0, time_func=clock)
        rm.note_sent(seq=5)
        self.assertFalse(rm.note_ack(6))
        self.assertTrue(rm.is_pending)

    def test_ack_with_nothing_pending_does_not_resolve(self):
        rm = link.RetryManager()
        self.assertFalse(rm.note_ack(1))

    def test_gives_up_after_max_retries(self):
        clock = _FakeClock()
        rm = link.RetryManager(timeout_s=1.0, max_retries=2, time_func=clock)
        rm.note_sent(seq=5)                       # attempt 1 (original)
        clock.advance(1.5)
        self.assertEqual(rm.poll(), 'retry')
        rm.note_sent(seq=5)                       # attempt 2 (retry 1)
        clock.advance(1.5)
        self.assertEqual(rm.poll(), 'retry')
        rm.note_sent(seq=5)                       # attempt 3 (retry 2)
        clock.advance(1.5)
        self.assertEqual(rm.poll(), 'give_up')
        self.assertFalse(rm.is_pending, "give_up should reset state automatically")

    def test_new_frame_after_give_up_works_cleanly(self):
        clock = _FakeClock()
        rm = link.RetryManager(timeout_s=1.0, max_retries=0, time_func=clock)
        rm.note_sent(seq=1)
        clock.advance(1.5)
        self.assertEqual(rm.poll(), 'give_up')
        rm.note_sent(seq=2)
        self.assertTrue(rm.is_pending)
        self.assertEqual(rm.pending_seq, 2)

    def test_default_time_func_is_real_monotonic_clock(self):
        # not exercising real timing behavior, just confirming the default
        # doesn't crash and produces sane non-decreasing values
        rm = link.RetryManager()
        rm.note_sent(seq=1)
        self.assertIsInstance(rm._sent_at, float)


class PreambleTests(unittest.TestCase):
    def test_preamble_is_128_symbols(self):
        # 2 bits/symbol -> 256 bits -> 32 bytes
        self.assertEqual(len(link.PREAMBLE), 32)

    def test_preamble_is_not_a_simple_repeating_pattern(self):
        # guards against accidentally regressing to something like the old
        # 0x55-repeated preamble, which this sequence replaced specifically
        # for its poor autocorrelation properties (see test below)
        self.assertNotEqual(link.PREAMBLE, b'\x55' * len(link.PREAMBLE))
        distinct_bytes = len(set(link.PREAMBLE))
        self.assertGreater(distinct_bytes, len(link.PREAMBLE) // 4,
                            "preamble looks suspiciously repetitive")

    def test_preamble_has_sharp_autocorrelation_peak(self):
        """The actual property we care about: a real PN sequence has a sharp,
        unambiguous correlation peak at zero offset and much weaker
        correlation everywhere else - unlike a periodic pattern like 0x55,
        which correlates almost as strongly at every 2-bit-multiple offset as
        at zero, making frame-start detection ambiguous under noise."""
        bits = []
        for byte in link.PREAMBLE:
            for i in range(7, -1, -1):
                bits.append((byte >> i) & 1)
        bipolar = [1 if b else -1 for b in bits]
        n = len(bipolar)

        def autocorr(shift):
            overlap = n - shift
            return sum(bipolar[i] * bipolar[i + shift] for i in range(overlap))

        peak = autocorr(0)
        self.assertEqual(peak, n)
        max_sidelobe = max(abs(autocorr(s)) for s in range(1, n))
        # the old 0x55 pattern's sidelobes were ~99% of its peak at every
        # even offset; this sequence's worst sidelobe should be nowhere close
        self.assertLess(max_sidelobe, peak * 0.25,
                         "preamble's correlation sidelobes are too close to its peak")

    def test_preamble_generation_is_deterministic(self):
        # both TX and RX derive PREAMBLE from the same generator - it must
        # produce the exact same bytes every time, not depend on system state
        self.assertEqual(link._pn_bytes(32), link.PREAMBLE)


class SplitPayloadTests(unittest.TestCase):
    def test_empty_payload_is_one_empty_chunk(self):
        self.assertEqual(link.split_payload(b''), [b''])

    def test_payload_shorter_than_chunk_size_is_one_chunk(self):
        self.assertEqual(link.split_payload(b'hi', chunk_size=10), [b'hi'])

    def test_exact_multiple_splits_evenly(self):
        self.assertEqual(link.split_payload(b'AABBCC', chunk_size=2), [b'AA', b'BB', b'CC'])

    def test_remainder_goes_in_final_short_chunk(self):
        self.assertEqual(link.split_payload(b'AABBC', chunk_size=2), [b'AA', b'BB', b'C'])

    def test_reassembling_chunks_recovers_original(self):
        payload = bytes(range(50)) * 3  # 150 arbitrary bytes
        chunks = link.split_payload(payload, chunk_size=7)
        self.assertEqual(b''.join(chunks), payload)


class BuildFragmentedFramesTests(unittest.TestCase):
    def _frame_header(self, frame):
        idx = frame.find(link.SYNC_WORD)
        self.assertNotEqual(idx, -1)
        start = idx + len(link.SYNC_WORD)
        return link.parse_header(frame[start:start + link.HEADER_LEN])

    def test_short_payload_is_a_single_unfragmented_frame(self):
        frames, next_seq = link.build_fragmented_frames(b'short', seq_start=10, chunk_size=100)
        self.assertEqual(len(frames), 1)
        header = self._frame_header(frames[0])
        self.assertFalse(link.parse_flags(header['flags'])['fragmented'])
        self.assertFalse(link.parse_flags(header['flags'])['more_fragments'])
        self.assertEqual(header['seq'], 10)
        self.assertEqual(next_seq, 11)

    def test_short_payload_matches_plain_build_frame(self):
        # fragmentation should be entirely invisible for a payload that fits
        frames, _ = link.build_fragmented_frames(b'short', seq_start=0, chunk_size=100)
        plain = link.build_frame(b'short', seq=0)
        self.assertEqual(frames[0], plain)

    def test_long_payload_splits_with_correct_flags_and_consecutive_seq(self):
        payload = b'X' * 25
        frames, next_seq = link.build_fragmented_frames(payload, seq_start=100, chunk_size=10)
        self.assertEqual(len(frames), 3)  # 10 + 10 + 5
        self.assertEqual(next_seq, 103)

        headers = [self._frame_header(f) for f in frames]
        for h in headers:
            self.assertTrue(link.parse_flags(h['flags'])['fragmented'])
        self.assertTrue(link.parse_flags(headers[0]['flags'])['more_fragments'])
        self.assertTrue(link.parse_flags(headers[1]['flags'])['more_fragments'])
        self.assertFalse(link.parse_flags(headers[2]['flags'])['more_fragments'])
        self.assertEqual([h['seq'] for h in headers], [100, 101, 102])

    def test_seq_wraps_across_fragments(self):
        payload = b'X' * 15
        frames, next_seq = link.build_fragmented_frames(
            payload, seq_start=link.SEQ_MODULUS - 1, chunk_size=10)
        headers = [self._frame_header(f) for f in frames]
        self.assertEqual([h['seq'] for h in headers], [link.SEQ_MODULUS - 1, 0])
        self.assertEqual(next_seq, 1)

    def test_fragments_individually_pass_frame_parser(self):
        payload = b'reassemble me across several fragments please'
        frames, _ = link.build_fragmented_frames(payload, seq_start=0, chunk_size=10)
        chunks = []
        for frame in frames:
            idx = frame.find(link.SYNC_WORD) + len(link.SYNC_WORD)
            parser = link.FrameParser()
            status, result = 'incomplete', None
            for b in frame[idx:]:
                status, result = parser.feed(b)
                if status != 'incomplete':
                    break
            self.assertEqual(status, 'ok')
            chunks.append(result)
        self.assertEqual(b''.join(chunks), payload)


class FragmentReassemblerTests(unittest.TestCase):
    def _fragments_as_header_payload_pairs(self, payload, seq_start=0, chunk_size=10):
        """Build fragmented frames and decode each back to (header, payload)
        via FrameParser, as RX would see them."""
        frames, _ = link.build_fragmented_frames(payload, seq_start=seq_start, chunk_size=chunk_size)
        pairs = []
        for frame in frames:
            idx = frame.find(link.SYNC_WORD) + len(link.SYNC_WORD)
            parser = link.FrameParser()
            status, result = 'incomplete', None
            for b in frame[idx:]:
                status, result = parser.feed(b)
                if status != 'incomplete':
                    break
            assert status == 'ok'
            pairs.append((parser.last_header, result))
        return pairs

    def test_clean_multi_fragment_reassembly(self):
        payload = b'a message that is definitely longer than one fragment chunk'
        pairs = self._fragments_as_header_payload_pairs(payload, chunk_size=10)
        self.assertGreater(len(pairs), 1, "test payload should actually need multiple fragments")

        reassembler = link.FragmentReassembler()
        statuses = []
        result = None
        for header, chunk in pairs:
            status, result = reassembler.feed(header, chunk)
            statuses.append(status)

        self.assertEqual(statuses[:-1], ['waiting'] * (len(statuses) - 1))
        self.assertEqual(statuses[-1], 'complete')
        self.assertEqual(result, payload)
        self.assertFalse(reassembler.in_progress)
        self.assertEqual(reassembler.abort_count, 0)

    def test_missing_middle_fragment_aborts_and_discards(self):
        payload = b'four fragments worth of test payload data here'
        pairs = self._fragments_as_header_payload_pairs(payload, chunk_size=10)
        self.assertGreaterEqual(len(pairs), 4)

        reassembler = link.FragmentReassembler()
        # feed fragment 0, then skip fragment 1 (simulate a CRC failure/loss),
        # then feed fragment 2
        status0, _ = reassembler.feed(*pairs[0])
        self.assertEqual(status0, 'waiting')
        self.assertEqual(reassembler.abort_count, 0)

        # fragment 2 arrives next (fragment_index doesn't match expected) -
        # this is NOT a legitimate new-run start (fragment_index != 0), so it
        # must be discarded, not misread as restarting a fresh run - the old
        # "restart from whatever arrived" behavior was exactly the bug that
        # let a receiver silently accept a truncated tail as a complete
        # message (see FragmentReassembler's docstring)
        status2, result2 = reassembler.feed(*pairs[2])
        self.assertEqual(status2, 'discarded')
        self.assertIsNone(result2)
        self.assertEqual(reassembler.abort_count, 1)  # the fragment-0-only run was cut short
        self.assertEqual(reassembler.discarded_count, 1)
        self.assertFalse(reassembler.in_progress)

        # a genuine fresh start (fragment_index == 0) afterward still works
        status0b, _ = reassembler.feed(*pairs[0])
        self.assertEqual(status0b, 'waiting')

    def test_overflow_then_leftover_fragments_are_discarded_not_delivered(self):
        """The actual bug this field was added to fix: after an overflow,
        the leftover continuation fragments of the abandoned run must NOT
        be allowed to quietly reassemble into a 'complete' truncated
        message - see this module's PREAMBLE/fragment_index comment."""
        payload = b'Z' * 500
        pairs = self._fragments_as_header_payload_pairs(payload, chunk_size=10)
        self.assertGreater(len(pairs), 6)

        reassembler = link.FragmentReassembler(max_fragments=3)
        statuses = [reassembler.feed(h, c)[0] for h, c in pairs[:4]]
        self.assertEqual(statuses[:3], ['waiting', 'waiting', 'waiting'])
        self.assertEqual(statuses[3], 'overflow')

        # every leftover fragment of the abandoned run must be discarded,
        # never delivered as 'complete'
        for header, chunk in pairs[4:]:
            status, result = reassembler.feed(header, chunk)
            self.assertEqual(status, 'discarded', "leftover fragment must not silently reassemble")
            self.assertIsNone(result)
        self.assertGreater(reassembler.discarded_count, 0)

    def test_interleaved_sources_do_not_merge(self):
        """Two different stations' fragmented messages with coincidentally
        matching sequence numbers must not merge into one garbled result -
        fragment_index alone isn't enough if src_id isn't checked too."""
        payload_a = b'message from station A, several fragments long here'
        payload_b = b'a totally different message from station B goes here'
        pairs_a = self._fragments_as_header_payload_pairs(payload_a, chunk_size=10)
        pairs_b = self._fragments_as_header_payload_pairs(payload_b, chunk_size=10)
        self.assertGreater(len(pairs_a), 1)
        self.assertGreater(len(pairs_b), 1)

        # force distinct, non-default source IDs so a same-bytes coincidence
        # isn't hiding a bug
        for header, _ in pairs_a:
            header['src_id'] = b'STATIONA'
        for header, _ in pairs_b:
            header['src_id'] = b'STATIONB'

        reassembler = link.FragmentReassembler()
        status0, _ = reassembler.feed(*pairs_a[0])
        self.assertEqual(status0, 'waiting')

        # station B's fragment 1 arrives next (same fragment_index B would
        # expect if this were A's stream) - must be discarded, not merged in
        status1, result1 = reassembler.feed(*pairs_b[1])
        self.assertEqual(status1, 'discarded')
        self.assertIsNone(result1)

    def test_overflow_protection(self):
        payload = b'Y' * 500
        pairs = self._fragments_as_header_payload_pairs(payload, chunk_size=10)
        self.assertGreater(len(pairs), 5)

        reassembler = link.FragmentReassembler(max_fragments=3)
        statuses = [reassembler.feed(h, c)[0] for h, c in pairs[:4]]
        self.assertEqual(statuses[:3], ['waiting', 'waiting', 'waiting'])
        self.assertEqual(statuses[3], 'overflow')
        self.assertEqual(reassembler.overflow_count, 1)
        self.assertFalse(reassembler.in_progress)

    def test_reassembler_recovers_after_overflow(self):
        payload = b'Z' * 100
        pairs = self._fragments_as_header_payload_pairs(payload, chunk_size=10)
        reassembler = link.FragmentReassembler(max_fragments=2)
        for h, c in pairs[:3]:
            reassembler.feed(h, c)
        self.assertFalse(reassembler.in_progress)  # overflowed and reset

        # a fresh, short run afterward should work normally
        payload2 = b'short again'
        pairs2 = self._fragments_as_header_payload_pairs(payload2, seq_start=0, chunk_size=100)
        self.assertEqual(len(pairs2), 1)
        # single-chunk payloads aren't marked fragmented by build_fragmented_frames,
        # so force fragmentation flags on to exercise the reassembler directly
        header, chunk = pairs2[0]
        header = dict(header)
        header['flags'] = link.FLAG_FRAGMENTED  # more_fragments=0 -> final
        status, result = reassembler.feed(header, chunk)
        self.assertEqual(status, 'complete')
        self.assertEqual(result, payload2)


class IterFramesTests(unittest.TestCase):
    def test_no_marker_yields_nothing(self):
        self.assertEqual(list(link.iter_frames(b'no frame in here at all')), [])

    def test_single_frame_found_and_parsed(self):
        frame = link.build_frame(b'hello', seq=7, src_id=b'K0MDT', dst_id=b'W1AW')
        results = list(link.iter_frames(frame))
        self.assertEqual(len(results), 1)
        status, header, payload = results[0]
        self.assertEqual(status, 'ok')
        self.assertEqual(payload, b'hello')
        self.assertEqual(header['seq'], 7)

    def test_multiple_frames_in_one_buffer(self):
        data = link.build_frame(b'first', seq=1) + link.build_frame(b'second', seq=2)
        results = list(link.iter_frames(data))
        self.assertEqual([r[0] for r in results], ['ok', 'ok'])
        self.assertEqual([r[2] for r in results], [b'first', b'second'])

    def test_corrupted_payload_reported_as_crc_fail(self):
        frame = bytearray(link.build_frame(b'hello', seq=1))
        # flip a byte inside the payload, well past the header
        payload_start = frame.index(b'hello')
        frame[payload_start] ^= 0xFF
        status, header, payload = next(link.iter_frames(bytes(frame)))
        self.assertEqual(status, 'crc_fail')
        self.assertIsNone(header)
        self.assertIsNone(payload)

    def test_truncated_trailing_frame_yields_nothing_for_it(self):
        frame = link.build_frame(b'hello', seq=1)
        marker = link.PREAMBLE + link.SYNC_WORD
        cut = frame.index(marker) + len(marker) + 10  # stop partway through the header
        results = list(link.iter_frames(frame[:cut]))
        self.assertEqual(results, [])


class FormatPacketTypeTests(unittest.TestCase):
    def test_known_types(self):
        self.assertEqual(link.format_packet_type(link.PACKET_TYPE_DATA), 'Data')
        self.assertEqual(link.format_packet_type(link.PACKET_TYPE_ACK), 'ACK')
        self.assertEqual(link.format_packet_type(link.PACKET_TYPE_BEACON), 'Beacon')
        self.assertEqual(link.format_packet_type(link.PACKET_TYPE_CONTROL), 'Control')

    def test_unknown_type(self):
        self.assertEqual(link.format_packet_type(0x99), 'Unknown(0x99)')


class DescribeFramesTests(unittest.TestCase):
    def test_no_marker_yields_nothing(self):
        self.assertEqual(list(link.describe_frames(b'nothing to see here')), [])

    def test_ok_frame_has_all_fields(self):
        frame = link.build_frame(b'hello', seq=7, mode_id=2, src_id=b'K0MDT', dst_id=b'W1AW')
        [result] = list(link.describe_frames(frame))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['offset'], frame.index(link.PREAMBLE))
        self.assertEqual(result['preamble'], link.PREAMBLE)
        self.assertEqual(result['sync_word'], link.SYNC_WORD)
        self.assertEqual(len(result['header_bytes']), link.HEADER_LEN)
        self.assertEqual(result['header']['seq'], 7)
        self.assertEqual(result['header']['mode_id'], 2)
        self.assertEqual(result['payload'], b'hello')
        self.assertEqual(len(result['crc_bytes']), link.CRC_LEN)
        self.assertEqual(result['total_len'],
                          len(link.PREAMBLE) + len(link.SYNC_WORD) + link.HEADER_LEN + 5 + link.CRC_LEN)

    def test_crc_fail_still_reports_header_and_payload(self):
        frame = bytearray(link.build_frame(b'hello', seq=1))
        payload_start = frame.index(b'hello')
        frame[payload_start] ^= 0xFF
        [result] = list(link.describe_frames(bytes(frame)))
        self.assertEqual(result['status'], 'crc_fail')
        self.assertIsNotNone(result['header'])
        self.assertIsNotNone(result['payload'])

    def test_truncated_header_reports_no_header(self):
        frame = link.build_frame(b'hello', seq=1)
        marker = link.PREAMBLE + link.SYNC_WORD
        cut = frame.index(marker) + len(marker) + 10
        [result] = list(link.describe_frames(frame[:cut]))
        self.assertEqual(result['status'], 'truncated')
        self.assertIsNone(result['header'])

    def test_truncated_payload_reports_header_but_no_payload(self):
        frame = link.build_frame(b'hello world this is longer', seq=1)
        marker = link.PREAMBLE + link.SYNC_WORD
        cut = frame.index(marker) + len(marker) + link.HEADER_LEN + 3  # header complete, payload cut short
        [result] = list(link.describe_frames(frame[:cut]))
        self.assertEqual(result['status'], 'truncated')
        self.assertIsNotNone(result['header'])
        self.assertIsNone(result['payload'])

    def test_multiple_frames_each_described(self):
        data = link.build_frame(b'first', seq=1) + link.build_frame(b'second', seq=2)
        results = list(link.describe_frames(data))
        self.assertEqual([r['status'] for r in results], ['ok', 'ok'])
        self.assertEqual([r['payload'] for r in results], [b'first', b'second'])


class LinkReceiverTests(unittest.TestCase):
    def _feed(self, receiver, frame_bytes):
        for status, header, payload in link.iter_frames(frame_bytes):
            if status == 'ok':
                receiver.feed_frame(header, payload)
            else:
                receiver.feed_crc_failure(status)

    def test_plain_message_dispatches_on_message(self):
        messages = []
        receiver = link.LinkReceiver(on_message=messages.append)
        self._feed(receiver, link.build_frame(b'hello', seq=1))
        self.assertEqual(messages, ['hello'])
        self.assertEqual(receiver.counts['ok'], 1)

    def test_on_raw_message_fires_with_undecoded_bytes(self):
        raw = []
        receiver = link.LinkReceiver(on_raw_message=raw.append)
        self._feed(receiver, link.build_frame(b'hello', seq=1))
        self.assertEqual(raw, [b'hello'])

    def test_on_raw_message_preserves_arbitrary_binary_content(self):
        # bytes that are NOT valid UTF-8 - on_message's text would be
        # corrupted (replacement characters) via errors='replace', but
        # on_raw_message must return them completely unchanged
        binary_payload = bytes(range(256)) * 4  # every possible byte value
        raw = []
        texts = []
        receiver = link.LinkReceiver(on_raw_message=raw.append, on_message=texts.append)
        self._feed(receiver, link.build_frame(binary_payload, seq=1))
        self.assertEqual(raw, [binary_payload])
        # confirm the premise: the text path really would have mangled this
        self.assertNotEqual(texts[0].encode('utf-8', errors='replace'), binary_payload)

    def test_on_raw_message_fires_once_per_reassembled_fragment_run(self):
        raw = []
        receiver = link.LinkReceiver(on_raw_message=raw.append)
        payload = b'F' * 3000  # several fragments at the default chunk size
        frames, _ = link.build_fragmented_frames(payload, seq_start=0, chunk_size=1000)
        self.assertGreater(len(frames), 1)
        self._feed(receiver, b''.join(frames))
        self.assertEqual(raw, [payload])

    def test_ack_requested_flag_triggers_on_ack_needed(self):
        needed = []
        receiver = link.LinkReceiver(on_ack_needed=needed.append)
        frame = link.build_frame(b'hi', seq=3, flags=link.FLAG_ACK_REQUESTED)
        self._feed(receiver, frame)
        self.assertEqual(needed, [3])

    def test_ack_frame_triggers_on_ack_received_not_message(self):
        messages = []
        received = []
        receiver = link.LinkReceiver(on_message=messages.append, on_ack_received=received.append)
        self._feed(receiver, link.build_ack_frame(acked_seq=5))
        self.assertEqual(received, [5])
        self.assertEqual(messages, [])
        self.assertEqual(receiver.counts['acks_rx'], 1)

    def test_unconfigured_station_id_accepts_everything(self):
        messages = []
        receiver = link.LinkReceiver(station_id='', on_message=messages.append)
        self._feed(receiver, link.build_frame(b'hi', seq=1, dst_id=b'W1AW'))
        self.assertEqual(messages, ['hi'])
        self.assertEqual(receiver.counts['not_for_us'], 0)

    def test_mismatched_destination_dropped(self):
        messages = []
        receiver = link.LinkReceiver(station_id='K0MDT', on_message=messages.append)
        self._feed(receiver, link.build_frame(b'hi', seq=1, dst_id=b'W1AW'))
        self.assertEqual(messages, [])
        self.assertEqual(receiver.counts['not_for_us'], 1)

    def test_broadcast_still_accepted_when_station_id_set(self):
        messages = []
        receiver = link.LinkReceiver(station_id='K0MDT', on_message=messages.append)
        self._feed(receiver, link.build_frame(b'hi', seq=1, dst_id=b'ALL'))
        self.assertEqual(messages, ['hi'])

    def test_duplicate_seq_counted(self):
        receiver = link.LinkReceiver()
        self._feed(receiver, link.build_frame(b'a', seq=1))
        self._feed(receiver, link.build_frame(b'a', seq=1))
        self.assertEqual(receiver.counts['dup'], 1)

    def test_lost_seq_gap_counted(self):
        receiver = link.LinkReceiver()
        self._feed(receiver, link.build_frame(b'a', seq=1))
        self._feed(receiver, link.build_frame(b'b', seq=5))
        self.assertEqual(receiver.counts['lost'], 3)

    def test_fragmented_message_buffers_until_complete(self):
        messages = []
        receiver = link.LinkReceiver(on_message=messages.append)
        frames, _ = link.build_fragmented_frames(b'x' * 10, seq_start=0, chunk_size=4)
        for frame in frames[:-1]:
            self._feed(receiver, frame)
        self.assertEqual(messages, [])
        self._feed(receiver, frames[-1])
        self.assertEqual(messages, ['x' * 10])

    def test_crc_failure_counted_not_dispatched(self):
        messages = []
        receiver = link.LinkReceiver(on_message=messages.append)
        frame = bytearray(link.build_frame(b'hello', seq=1))
        payload_start = frame.index(b'hello')
        frame[payload_start] ^= 0xFF
        self._feed(receiver, bytes(frame))
        self.assertEqual(messages, [])
        self.assertEqual(receiver.counts['crc_fail'], 1)

    def test_stats_line_reflects_state(self):
        receiver = link.LinkReceiver()
        self._feed(receiver, link.build_frame(b'hi', seq=9, mode_id=2, src_id=b'K0MDT', dst_id=b'W1AW'))
        line = receiver.stats_line()
        self.assertIn('OK:1', line)
        self.assertIn('Seq:9', line)
        self.assertIn('Mode 2', line)
        self.assertIn('K0MDT', line)
        self.assertIn('W1AW', line)


if __name__ == '__main__':
    unittest.main()
