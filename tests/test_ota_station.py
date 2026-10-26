"""
Integration test for ota_station.py: spins up an in-process ota_sim
simulator plus two Station instances (all in the same event loop, no
subprocesses), sends a real message end to end through the actual
modem chain (modulate/pulse-shape -> simulated channel -> matched-filter/
demodulate/sync), and confirms it arrives correctly.

Requires ota_sim to be installed (pip install -e ../ota_sim from this
repo) - skipped entirely if it isn't, matching the project's existing
optional-dependency skip pattern (see legacy/gnuradio/tests/).
"""
import asyncio
import unittest

import numpy as np

import fmlink_audio as audio_phy
import fmlink_fec as fec
import fmlink_link as link

try:
    from ota_sim import FMLink, OTALink
    from ota_sim.simulator import OTASimulator
    OTA_SIM_AVAILABLE = True
except ImportError:
    OTA_SIM_AVAILABLE = False

if OTA_SIM_AVAILABLE:
    from ota_station import Station, _ping_survey_summary, _handle_command, NARROWBAND_MAX_SYMBOL_RATE
    import ota_station

TIMEOUT_S = 5.0


async def _create_station(station_id, dst_id, **kwargs):
    """Not yet connected - call station.connect(*sim.address)."""
    return Station(station_id=station_id, dst_id=dst_id, **kwargs)


async def _create_simulator(channel):
    return await OTASimulator(channel, log=lambda msg: None).start('127.0.0.1', 0)


async def _wait_for(predicate, timeout_s=TIMEOUT_S, interval_s=0.02):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_s)
    return False


@unittest.skipUnless(OTA_SIM_AVAILABLE, 'ota_sim not installed (pip install -e ../ota_sim)')
class OTAStationIntegrationTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_clean_link_message_arrives_correctly(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=1)
            sim = await _create_simulator(channel)

            received = []
            a = await _create_station('K0MDT', 'W1AW', mode_id=0,
                                      on_message=None)
            b = await _create_station('W1AW', 'K0MDT', mode_id=0,
                                      on_message=received.append)

            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.05)  # let both registrations land before sending

            await a.send_text("integration test message")

            ok = await _wait_for(lambda: len(received) > 0)

            a.close()
            b.close()
            sim.close()

            self.assertTrue(ok, "message never arrived within timeout")
            self.assertEqual(received[0], "integration test message")

        self._run(scenario())

    def test_burst_far_beyond_old_udp_ceiling_arrives_in_one_fragment(self):
        # Mode 0 + r1_4_k9 with a full MAX_PAYLOAD_LEN payload is several
        # hundred KB of samples - the old UDP relay's ~64KB datagram limit
        # forced this down to a few dozen chars per burst.
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                              base_snr_db=60, fading=False, drop_prob=0.0,
                              latency_s=0.01, jitter_s=0.0, seed=1)
            sim = await _create_simulator(channel)
            received = []
            a = await _create_station('K0MDT', 'W1AW', mode_id=0, use_fec=True,
                                      fec_preset='r1_4_k9')
            b = await _create_station('W1AW', 'K0MDT', mode_id=0, use_fec=True,
                                      fec_preset='r1_4_k9', on_message=received.append)
            await a.connect(*sim.address)
            await b.connect(*sim.address)
            await asyncio.sleep(0.05)

            text = 'x' * link.MAX_PAYLOAD_LEN
            await a.send_text(text)
            ok = await _wait_for(lambda: len(received) > 0)

            a.close()
            b.close()
            sim.close()

            self.assertTrue(ok, "message never arrived within timeout")
            self.assertEqual(received[0], text)
            self.assertEqual(a.stats['sent_fragments'], 1)
            self.assertGreater(a.stats['sent_air_bytes'], 4 * 65507)

        self._run(scenario())

    def test_ack_requested_message_gets_acknowledged(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=2)
            sim = await _create_simulator(channel)

            received = []
            a = await _create_station('K0MDT', 'W1AW', mode_id=0, use_ack=True)
            b = await _create_station('W1AW', 'K0MDT', mode_id=0, use_ack=True,
                                      on_message=received.append)

            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.05)  # let both registrations land before sending

            await a.send_text("ack test")

            ok = await _wait_for(lambda: not a.retry_mgr.is_pending)

            a.close()
            b.close()
            sim.close()

            self.assertTrue(ok, "ACK never resolved within timeout")
            self.assertEqual(received, ["ack test"])

        self._run(scenario())

    def test_negotiate_selects_mode_from_channel_snr(self):
        # Regression coverage for the exact miscalibration this feature hit
        # live: a channel at fmlink_fec.py's proven-good 0dB anchor must
        # select r1_2_k3, and one at its proven-bad -6dB anchor must fall
        # back to r1_4_k9 - see fmlink_link.py's MODE_SNR_TABLE comment
        # for the real measurements behind these thresholds.
        async def negotiate_at(base_snr_db, seed):
            channel = OTALink(distance_m=1000, reference_distance_m=1000,
                               base_snr_db=base_snr_db, fading=False, drop_prob=0.0,
                               latency_s=0.005, jitter_s=0.0, seed=seed)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=0,
                                      use_fec=True, use_ack=True)
            b = await _create_station('W1AW', 'K0MDT', mode_id=0,
                                      use_fec=True, use_ack=True)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.02)
            await a.negotiate()
            result = (a.mode_id, a.fec_preset)
            a.close()
            b.close()
            sim.close()
            return result

        async def scenario():
            self.assertEqual(await negotiate_at(0, seed=1), (1, 'r1_2_k3'))
            self.assertEqual(await negotiate_at(-6, seed=1), (0, 'r1_4_k9'))

        self._run(scenario())

    def test_negotiate_leaves_both_stations_on_the_same_mode(self):
        # The whole point of the initiator-driven redesign: only A calls
        # negotiate() (measuring its OWN reception, per the user's
        # request), but B must end up on the identical mode/FEC too - not
        # its own independent opinion of the link - and must mark itself
        # already-negotiated so it doesn't later re-negotiate on its own
        # first send.
        async def scenario():
            channel = OTALink(distance_m=1000, reference_distance_m=1000,
                               base_snr_db=0, fading=False, drop_prob=0.0,
                               latency_s=0.005, jitter_s=0.0, seed=1)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=0,
                                      use_fec=True, use_ack=True)
            b = await _create_station('W1AW', 'K0MDT', mode_id=0,
                                      use_fec=True, use_ack=True)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.02)

            await a.negotiate()  # only A initiates - B never calls negotiate() itself

            a.close()
            b.close()
            sim.close()

            self.assertEqual((a.mode_id, a.fec_preset), (b.mode_id, b.fec_preset))
            self.assertTrue(b._auto_negotiate_done,
                             "B must consider the session already negotiated once it adopts A's MODE_SET")

        self._run(scenario())

    def test_bandwidth_narrow_never_picks_above_the_ceiling(self):
        # A clean, high-SNR link that would normally reach Mode 5
        # (16-QAM) must stay at or under NARROWBAND_MAX_SYMBOL_RATE
        # (1200 sym/s - modes 0-2) when bandwidth='narrow', regardless of
        # how good the measured link actually is - the ceiling models a
        # real deployment that just can't use a faster symbol rate, not
        # a reliability judgment.
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=6)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=0,
                                      use_fec=True, use_ack=True, bandwidth='narrow')
            b = await _create_station('W1AW', 'K0MDT', mode_id=0,
                                      use_fec=True, use_ack=True)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.02)

            await a.negotiate()

            a.close()
            b.close()
            sim.close()

            self.assertLessEqual(link.WAVEFORM_MODES[a.mode_id]['symbol_rate'], 1200)
            self.assertEqual((a.mode_id, a.fec_preset), (b.mode_id, b.fec_preset))

        self._run(scenario())

    def test_mode_command_switches_both_stations_and_communicates_it(self):
        # /mode is a manual override, not a measurement - it should just
        # apply whatever mode was asked for and tell the peer, the same
        # confirmed MODE_SET/MODE_SET_ACK exchange negotiate() uses.
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=8)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=0, use_fec=True)
            b = await _create_station('W1AW', 'K0MDT', mode_id=0, use_fec=True)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.02)

            await _handle_command(a, '/mode 3')

            a.close()
            b.close()
            sim.close()

            self.assertEqual(a.mode_id, 3)
            self.assertEqual((a.mode_id, a.fec_preset), (b.mode_id, b.fec_preset),
                              "the peer must be told and confirm, not just apply it locally")
            self.assertTrue(a._auto_negotiate_done,
                             "a manual /mode choice must count as already-negotiated")

        self._run(scenario())

    def test_fec_command_switches_preset_keeps_mode_and_communicates_it(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=9)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=2, use_fec=True)
            b = await _create_station('W1AW', 'K0MDT', mode_id=2, use_fec=True)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.02)

            await _handle_command(a, '/fec r1_4_k9')

            a.close()
            b.close()
            sim.close()

            self.assertEqual(a.mode_id, 2, "mode must be unchanged by /fec")
            self.assertEqual(a.fec_preset, 'r1_4_k9')
            self.assertEqual((a.mode_id, a.fec_preset), (b.mode_id, b.fec_preset))

        self._run(scenario())

    def test_fec_command_turns_on_use_fec_even_if_started_without_it(self):
        # A station started without --fec/--auto has use_fec=False - /fec
        # PRESET must turn it on, or the preset it just set would be
        # silently unused (nothing on the wire would actually change).
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=12)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=1)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await asyncio.sleep(0.02)
            self.assertFalse(a.use_fec, "sanity check - station must start without FEC")

            await _handle_command(a, '/fec r1_2_k9')

            a.close()
            sim.close()

            self.assertTrue(a.use_fec)
            self.assertEqual(a.fec_preset, 'r1_2_k9')

        self._run(scenario())

    def test_bandwidth_command_updates_ceiling_without_touching_mode(self):
        # Unlike /mode and /fec, /bandwidth is a constraint on FUTURE
        # automatic selection - it must not itself change the currently
        # active mode, and needs no peer communication (nothing sent).
        async def scenario():
            a = Station(station_id='K0MDT', dst_id='W1AW', mode_id=3)
            self.assertIsNone(a.max_symbol_rate, "sanity check - starts wide by default")

            await _handle_command(a, '/bandwidth narrow')
            self.assertEqual(a.max_symbol_rate, NARROWBAND_MAX_SYMBOL_RATE)
            self.assertEqual(a.mode_id, 3, "/bandwidth must not change the current mode")

            await _handle_command(a, '/bandwidth wide')
            self.assertIsNone(a.max_symbol_rate)

        self._run(scenario())

    def test_bandwidth_command_rejects_invalid_value(self):
        async def scenario():
            a = Station(station_id='K0MDT', dst_id='W1AW', mode_id=0, bandwidth='narrow')
            await _handle_command(a, '/bandwidth ultrawide')
            self.assertEqual(a.max_symbol_rate, NARROWBAND_MAX_SYMBOL_RATE,
                              "an invalid value must not change the ceiling")

        self._run(scenario())

    def test_help_command_lists_every_other_command(self):
        async def scenario():
            a = Station(station_id='K0MDT', dst_id='W1AW', mode_id=0)
            logged = []
            a._log = logged.append

            await _handle_command(a, '/help')

            self.assertEqual(len(logged), 1)
            for name in ('/ping', '/renegotiate', '/mode', '/modes', '/fec', '/bandwidth',
                         '/ack', '/auto', '/lead-in', '/help', '/quit'):
                self.assertIn(name, logged[0])

        self._run(scenario())

    def test_quit_and_exit_signal_the_stdin_loop_to_stop(self):
        # run()'s stdin loop breaks when _handle_command() returns False -
        # everything else must return True so the session keeps going.
        async def scenario():
            a = Station(station_id='K0MDT', dst_id='W1AW', mode_id=0)
            a._log = lambda msg: None

            self.assertFalse(await _handle_command(a, '/quit'))
            self.assertFalse(await _handle_command(a, '/exit'))
            self.assertTrue(await _handle_command(a, '/help'))
            self.assertTrue(await _handle_command(a, '/modes'))
            self.assertTrue(await _handle_command(a, '/nonsense'))
            self.assertTrue(await _handle_command(a, '/mode'))
            self.assertTrue(await _handle_command(a, '/bandwidth'))

        self._run(scenario())

    def test_question_mark_is_an_alias_for_help(self):
        async def scenario():
            a = Station(station_id='K0MDT', dst_id='W1AW', mode_id=0)
            logged = []
            a._log = logged.append

            await _handle_command(a, '/?')
            await _handle_command(a, '/help')

            self.assertEqual(logged[0], logged[1])

        self._run(scenario())

    def test_unrecognized_command_points_to_help(self):
        async def scenario():
            a = Station(station_id='K0MDT', dst_id='W1AW', mode_id=0)
            logged = []
            a._log = logged.append

            await _handle_command(a, '/nonsense')

            self.assertEqual(len(logged), 1)
            self.assertIn('/help', logged[0])

        self._run(scenario())

    def test_mode_command_rejects_invalid_mode_without_changing_anything(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=10)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=1, use_fec=True)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await asyncio.sleep(0.02)

            await _handle_command(a, '/mode 99')

            a.close()
            sim.close()

            self.assertEqual(a.mode_id, 1)
            self.assertFalse(a._auto_negotiate_done)

        self._run(scenario())

    def test_mode_command_rejects_modes_above_narrow_bandwidth_ceiling(self):
        # A narrow --bandwidth ceiling is a hard limit on what this
        # deployment can actually carry, not just a hint --auto prefers -
        # /mode must respect it too, or "narrow" would be trivially
        # bypassable by hand.
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=13)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=1,
                                      use_fec=True, bandwidth='narrow')
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await asyncio.sleep(0.02)

            await _handle_command(a, '/mode 5')  # 16-QAM @ 9600 sym/s - over the 1200 ceiling

            a.close()
            sim.close()

            self.assertEqual(a.mode_id, 1, "must stay on the original mode, not switch to 5")
            self.assertFalse(a._auto_negotiate_done)

        self._run(scenario())

    def test_fec_command_rejects_unknown_preset_without_changing_anything(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=11)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=1, use_fec=True)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await asyncio.sleep(0.02)
            original_fec_preset = a.fec_preset

            await _handle_command(a, '/fec not-a-real-preset')

            a.close()
            sim.close()

            self.assertEqual(a.fec_preset, original_fec_preset)
            self.assertFalse(a._auto_negotiate_done)

        self._run(scenario())

    def test_auto_negotiate_triggers_lazily_on_first_send_only(self):
        # --auto must NOT probe the link until this station actually has
        # something to send (a station with nothing to send yet just
        # listens), and must only ever do so ONCE per session, not on
        # every send_text() call.
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=3)
            sim = await _create_simulator(channel)

            received = []
            a = await _create_station('K0MDT', 'W1AW', mode_id=0,
                                      use_fec=True, use_ack=True, auto=True)
            b = await _create_station('W1AW', 'K0MDT', mode_id=0,
                                      use_fec=True, use_ack=True,
                                      on_message=received.append)

            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.05)

            self.assertFalse(a._auto_negotiate_done, "must not negotiate before there's anything to send")
            self.assertEqual(a.mode_id, 0, "mode must be untouched before the first send")

            negotiate_calls = []
            original_negotiate = a.negotiate

            async def counting_negotiate():
                negotiate_calls.append(1)
                await original_negotiate()
            a.negotiate = counting_negotiate

            await a.send_text("first message")
            self.assertTrue(a._auto_negotiate_done)
            self.assertEqual(len(negotiate_calls), 1)
            negotiated_mode = a.mode_id

            await a.send_text("second message")
            self.assertEqual(len(negotiate_calls), 1,
                              "negotiate() must not run again on a later send")
            self.assertEqual(a.mode_id, negotiated_mode)

            a.close()
            b.close()
            sim.close()

        self._run(scenario())

    def test_ping_trials_measure_round_trip(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=4)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=0)
            b = await _create_station('W1AW', 'K0MDT', mode_id=0)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.02)

            results, summary = await a._ping_trials(count=3)

            a.close()
            b.close()
            sim.close()

            self.assertEqual(len(results), 3)
            self.assertTrue(all(r['ok'] for r in results), results)
            self.assertTrue(all(r['rtt_s'] > 0 for r in results), results)
            self.assertTrue(all(r['evm_percent'] > 0 for r in results), results)
            self.assertTrue(all(r['burst_bytes'] > 0 for r in results), results)
            self.assertTrue(all(r['failure_reason'] is None for r in results), results)
            self.assertIn('3/3 ok', summary)
            self.assertIn('EVM', summary)
            self.assertIn('B/burst', summary)

        self._run(scenario())

    def test_ping_trials_reports_no_reply_when_nothing_is_listening(self):
        # Nobody home at all (no station B, no reply of any kind) must be
        # distinguished from a reply that arrived but couldn't be used -
        # see _handle_received_samples()'s _ping_last_rx_status hook.
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=14)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=0)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await asyncio.sleep(0.02)

            results, summary = await a._ping_trials(count=1)

            a.close()
            sim.close()

            self.assertFalse(results[0]['ok'])
            self.assertEqual(results[0]['failure_reason'], 'no_reply')
            self.assertIn('no_reply', summary)

        self._run(scenario())

    def test_ping_last_rx_status_hook_records_no_sync_while_pending(self):
        # Unit-level check of the hook _ping_trials() relies on to tell
        # "nothing came back" from "something came back but was
        # unusable" - a burst that doesn't even sync, arriving WHILE a
        # ping is outstanding, must set _ping_last_rx_status. No network
        # needed - _handle_received_samples() works on raw bytes alone.
        a = Station(station_id='K0MDT', dst_id='W1AW', mode_id=0)
        a._log = lambda msg: None
        self.assertIsNone(a._ping_last_rx_status, "sanity check")

        a._ping_event = asyncio.Event()  # simulate an outstanding ping
        garbage = (b'\x00' * 64)  # not a real modulated burst - won't sync at all
        a._handle_received_samples(garbage)

        self.assertEqual(a._ping_last_rx_status, 'no_sync')

        # ...but the SAME garbage must NOT set anything when no ping is
        # outstanding - this hook is scoped to ping's own concern only.
        a._ping_event = None
        a._ping_last_rx_status = None
        a._handle_received_samples(garbage)
        self.assertIsNone(a._ping_last_rx_status)

    def test_ping_survey_tests_every_mode_and_restores_original(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=5)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=1, use_fec=True)
            b = await _create_station('W1AW', 'K0MDT', mode_id=1, use_fec=True)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.02)

            original = (a.mode_id, a.fec_preset)

            visited_modes = []
            original_announce = a._announce_mode

            async def tracking_announce(mode_id, fec_preset=None):
                visited_modes.append(mode_id)
                return await original_announce(mode_id, fec_preset)
            a._announce_mode = tracking_announce

            await a.ping_survey()

            a.close()
            b.close()
            sim.close()

            # every waveform mode announced once during the survey, plus once
            # more at the end to restore the original mode
            self.assertEqual(visited_modes, sorted(link.WAVEFORM_MODES) + [original[0]])
            self.assertEqual((a.mode_id, a.fec_preset), original,
                              "survey must leave the station back on its original mode")

        self._run(scenario())

    def test_ping_survey_respects_narrow_bandwidth_ceiling(self):
        # Unlike --auto (which only constrains automatic selection), the
        # survey's whole point is showing real performance for modes
        # actually usable under the current deployment - so bandwidth=
        # 'narrow' should skip modes above NARROWBAND_MAX_SYMBOL_RATE
        # entirely, not just decline to auto-pick them.
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=7)
            sim = await _create_simulator(channel)
            a = await _create_station('K0MDT', 'W1AW', mode_id=1,
                                      use_fec=True, bandwidth='narrow')
            b = await _create_station('W1AW', 'K0MDT', mode_id=1, use_fec=True)
            sim_addr = sim.address
            await a.connect(*sim_addr)
            await b.connect(*sim_addr)
            await asyncio.sleep(0.02)

            visited_modes = []
            original_announce = a._announce_mode

            async def tracking_announce(mode_id, fec_preset=None):
                visited_modes.append(mode_id)
                return await original_announce(mode_id, fec_preset)
            a._announce_mode = tracking_announce

            await a.ping_survey()

            a.close()
            b.close()
            sim.close()

            expected = [m for m in sorted(link.WAVEFORM_MODES)
                        if link.WAVEFORM_MODES[m]['symbol_rate'] <= 1200]
            self.assertEqual(expected, [0, 1, 2])  # sanity-check the ceiling picked modes 0-2
            self.assertEqual(visited_modes, expected + [1])  # +1 to restore the original mode

        self._run(scenario())


@unittest.skipUnless(OTA_SIM_AVAILABLE, 'ota_sim not installed (pip install -e ../ota_sim)')
class AudioPhyStationTests(unittest.TestCase):
    """--phy audio: stations exchanging real 48 kHz audio through ota_sim's
    FM transceiver model, where bursts take real airtime."""

    def _run(self, coro):
        return asyncio.run(coro)

    @staticmethod
    def _fm_channel(port='mic', seed=1):
        return FMLink(port, base_cnr_db=25, latency_s=0.01, jitter_s=0.0, seed=seed)

    def test_message_arrives_over_fm(self):
        async def scenario():
            sim = await _create_simulator(self._fm_channel())
            received = []
            a = await _create_station('K0MDT', 'W1AW', mode_id=2, use_fec=True,
                                      phy='audio', audio_port='mic')
            b = await _create_station('W1AW', 'K0MDT', mode_id=2, use_fec=True,
                                      phy='audio', audio_port='mic', on_message=received.append)
            await a.connect(*sim.address)
            await b.connect(*sim.address)
            await asyncio.sleep(0.05)

            await a.send_text("over the FM channel")
            ok = await _wait_for(lambda: received)

            a.close()
            b.close()
            sim.close()

            self.assertTrue(ok, "message never arrived within timeout")
            self.assertEqual(received[0], "over the FM channel")
            self.assertGreater(b._last_rx_snr_db, 15)

        self._run(scenario())

    def test_ack_waits_out_airtime_instead_of_retrying(self):
        # Mode 0 bursts air for ~1.9-3 s each way - longer than the old fixed
        # 2 s reply timeout in total, which would retry (and collide, on a
        # half-duplex link) before the ACK could ever arrive.
        async def scenario():
            sim = await _create_simulator(self._fm_channel(seed=2))
            received = []
            a = await _create_station('K0MDT', 'W1AW', mode_id=0, use_fec=True, use_ack=True,
                                      phy='audio', audio_port='mic')
            b = await _create_station('W1AW', 'K0MDT', mode_id=0, use_fec=True, use_ack=True,
                                      phy='audio', audio_port='mic', on_message=received.append)
            await a.connect(*sim.address)
            await b.connect(*sim.address)
            await asyncio.sleep(0.05)
            transmissions = []
            original_transmit = a._transmit_frame
            a._transmit_frame = lambda *args, **kw: (transmissions.append(1),
                                                     original_transmit(*args, **kw))

            await asyncio.wait_for(a.send_text("ack me"), timeout=15)

            a.close()
            b.close()
            sim.close()

            self.assertEqual(a.receiver.counts['acks_rx'], 1)
            self.assertEqual(received, ["ack me"])
            self.assertEqual(len(transmissions), 1, "should not have retried")
            self.assertGreater(a._last_tx_airtime_s, 1.0)

        self._run(scenario())

    def _probe_through_fm(self, **fm_kwargs):
        """A MODE_REQUEST probe burst from W1AW, through an FM channel, fed
        straight into a K0MDT station's RX path; returns that station."""
        station = Station(station_id='K0MDT', dst_id='W1AW', phy='audio', audio_port='mic',
                          on_log=lambda msg: None)
        sender = Station(station_id='W1AW', dst_id='K0MDT', phy='audio', audio_port='mic')
        request = audio_phy.frame_for_audio(
            link.build_mode_request_frame(7, mode_id=0, src_id=b'W1AW', dst_id=b'K0MDT'))
        coded = fec.encode_frame(request, sender._control_fec_generators, sender._control_fec_k,
                                 marker=link.AUDIO_MARKER)
        audio = audio_phy.modulate(sender._control_modulate(coded), 600,
                                   audio_phy.PORT_PROFILES['mic']['carrier_hz'])
        received, _ = FMLink('mic', freq_hz=440e6, latency_s=0, jitter_s=0, **fm_kwargs).transmit(audio)
        station._handle_received_samples(audio_phy.audio_to_pcm16(received))
        return station

    def test_mobile_fading_selects_from_the_fading_table(self):
        # The case that motivated FADING_MODE_SNR_TABLE: 15 dB CNR, 80 km/h
        # at 440 MHz. The plain table's pick (r1_2_k3) can't ride out the
        # dropouts; the fading table goes by the whole-burst average and
        # uses K=7/K=9 codes.
        station = self._probe_through_fm(base_cnr_db=15, fading='rayleigh', speed_kmh=80, seed=4)
        self.assertIsNotNone(station._last_rx_quality, "probe should have decoded")
        self.assertTrue(station._fading())
        expected = link.select_mode_for_snr(station._last_rx_quality['snr_db'],
                                            max_symbol_rate=station.max_symbol_rate,
                                            table=link.FADING_MODE_SNR_TABLE)
        self.assertEqual(station._select_mode(), expected)
        self.assertNotEqual(expected[1], 'r1_2_k3')

    def test_no_fading_keeps_the_regular_table(self):
        station = self._probe_through_fm(base_cnr_db=15, seed=4)
        self.assertFalse(station._fading())
        self.assertEqual(station._select_mode(),
                         link.select_mode_for_snr(station._last_rx_snr_db,
                                                  max_symbol_rate=station.max_symbol_rate))

    def test_frame_for_audio_keeps_exactly_the_frame(self):
        frame = link.build_frame(b'payload', seq=3)
        audio_frame = audio_phy.frame_for_audio(frame)
        self.assertTrue(audio_frame.startswith(link.AUDIO_MARKER))
        # build_frame's 24 + 24 pad bytes, and 16 of the 32 preamble bytes
        self.assertEqual(len(audio_frame), len(frame) - 48 - 16)
        self.assertEqual(audio_frame[len(link.AUDIO_MARKER):], frame[24 + len(link.IQ_MARKER):-24])
        self.assertEqual(audio_phy.frame_for_audio(audio_frame), audio_frame)
        self.assertEqual(audio_phy.frame_for_audio(b'no marker here'), b'no marker here')

    def test_audio_bursts_drop_the_padding_airtime(self):
        # The audio frame drops the 48 pad bytes and 16 preamble bytes (uncoded).
        class _Recorder:
            connected = True

            def __init__(self):
                self.sent = []

            def send(self, payload):
                self.sent.append(payload)

        station = Station(station_id='K0MDT', dst_id='W1AW', mode_id=0, use_fec=True,
                          phy='audio', audio_port='mic', on_log=lambda msg: None)
        station._sim = _Recorder()
        frame = link.build_ack_frame(5, mode_id=0, src_id=b'K0MDT', dst_id=b'W1AW')
        station._transmit_frame(frame)
        sent_s = len(station._sim.sent[0]) / 2 / audio_phy.AUDIO_SAMPLE_RATE
        padded = fec.encode_frame(frame, station._fec_generators, station._fec_k)
        padded_s = len(audio_phy.modulate(station._modulate(padded), 600, 1650)) / audio_phy.AUDIO_SAMPLE_RATE
        # 48 pad bytes + 16 preamble bytes, uncoded: 512 symbols = 0.853 s at 600 baud
        self.assertAlmostEqual(padded_s - sent_s, 512 / 600, delta=0.01)
        self.assertAlmostEqual(station._last_tx_airtime_s, sent_s, places=6)

    def test_ack_auto_and_lead_in_commands(self):
        async def scenario():
            logs = []
            a = Station(station_id='K0MDT', dst_id='W1AW', phy='audio', audio_port='mic',
                        on_log=logs.append)
            self.assertFalse(a.use_ack)
            self.assertIsNone(a.retry_mgr)
            await _handle_command(a, '/ack on')
            self.assertTrue(a.use_ack)
            self.assertIsNotNone(a.retry_mgr)
            await _handle_command(a, '/ack off')
            self.assertFalse(a.use_ack)

            a._auto_negotiate_done = True  # e.g. after a manual /mode
            await _handle_command(a, '/auto on')
            self.assertTrue(a.auto)
            self.assertFalse(a._auto_negotiate_done, "on must re-arm negotiation for the next send")
            await _handle_command(a, '/auto off')
            self.assertFalse(a.auto)

            await _handle_command(a, '/lead-in 400')
            self.assertAlmostEqual(a.lead_in_s, 0.4)
            for bad in ('/lead-in -5', '/lead-in soon', '/ack maybe', '/auto 1'):
                await _handle_command(a, bad)
            self.assertAlmostEqual(a.lead_in_s, 0.4)
            self.assertFalse(a.use_ack)
            self.assertFalse(a.auto)

            iq = Station(station_id='K0MDT', dst_id='W1AW', on_log=logs.append)
            await _handle_command(iq, '/lead-in 400')
            self.assertIn('only applies with --phy audio', logs[-1])

        self._run(scenario())

    def test_lead_in_change_reaches_the_next_burst(self):
        class _Recorder:
            connected = True

            def __init__(self):
                self.sent = []

            def send(self, payload):
                self.sent.append(payload)

        async def scenario():
            a = Station(station_id='K0MDT', dst_id='W1AW', mode_id=2, phy='audio', audio_port='mic',
                        on_log=lambda msg: None)
            a._sim = _Recorder()
            frame = link.build_frame(b'x', seq=1, mode_id=2)
            a._transmit_frame(frame)
            await asyncio.sleep(2 * ota_station.BATCH_COLLECT_S)   # let it go out on its own
            await _handle_command(a, '/lead-in 750')
            a._transmit_frame(frame)
            await asyncio.sleep(2 * ota_station.BATCH_COLLECT_S)
            first, second = (len(p) / 2 / audio_phy.AUDIO_SAMPLE_RATE for p in a._sim.sent)
            self.assertAlmostEqual(second - first, 0.5, places=3)

        self._run(scenario())

    def _batch_station(self, **kw):
        class _Recorder:
            connected = True

            def __init__(self):
                self.sent = []

            def send(self, payload):
                self.sent.append(payload)

        a = Station(station_id='K0MDT', dst_id='W1AW', mode_id=2, phy='audio', audio_port='mic',
                    on_log=lambda msg: None, **kw)
        a._sim = _Recorder()
        return a

    def test_bursts_sent_together_share_one_transmission(self):
        async def scenario():
            a = self._batch_station()
            frame = link.build_frame(b'x' * 50, seq=1, mode_id=2)
            a._transmit_frame(frame)
            await asyncio.sleep(2 * ota_station.BATCH_COLLECT_S)
            single = len(a._sim.sent[0])
            a._sim.sent.clear()
            for _ in range(3):
                a._transmit_frame(frame)
            self.assertEqual(a._sim.sent, [])           # held for the collection window
            await asyncio.sleep(2 * ota_station.BATCH_COLLECT_S)
            self.assertEqual(len(a._sim.sent), 1)       # one key-up for all three
            rate = audio_phy.AUDIO_SAMPLE_RATE
            lead = int(round(a.lead_in_s * rate))
            gap = int(round(ota_station.BATCH_GAP_S * rate))
            expected = single + 2 * (single - 2 * lead + 2 * gap)
            self.assertEqual(len(a._sim.sent[0]), expected)  # later bursts: a gap, not a lead-in

        self._run(scenario())

    def test_max_tx_s_splits_a_batch(self):
        async def scenario():
            a = self._batch_station(max_tx_s=0.8)
            frame = link.build_frame(b'x' * 50, seq=1, mode_id=2)   # ~0.6 s each, two won't fit
            for _ in range(3):
                a._transmit_frame(frame)
            await asyncio.sleep(2 * ota_station.BATCH_COLLECT_S)
            self.assertEqual(len(a._sim.sent), 3)
            self.assertTrue(all(len(p) / 2 / audio_phy.AUDIO_SAMPLE_RATE <= 0.8 for p in a._sim.sent))

        self._run(scenario())

    def test_without_an_event_loop_bursts_go_straight_out(self):
        a = self._batch_station()
        a._transmit_frame(link.build_frame(b'x', seq=1, mode_id=2))
        self.assertEqual(len(a._sim.sent), 1)

    def test_reply_timeout_tracks_airtime(self):
        iq = Station(station_id='K0MDT', dst_id='W1AW')
        self.assertEqual(iq._reply_timeout_s(2.0), 2.0)
        iq._last_tx_airtime_s = 1.5
        self.assertEqual(iq._reply_timeout_s(2.0), 5.0)

    def test_audio_needs_a_known_port_and_a_mode_that_fits_it(self):
        with self.assertRaises(ValueError):
            Station(station_id='K0MDT', dst_id='W1AW', phy='audio')
        with self.assertRaises(ValueError):
            Station(station_id='K0MDT', dst_id='W1AW', phy='audio', audio_port='speaker')
        with self.assertRaises(ValueError):
            Station(station_id='K0MDT', dst_id='W1AW', mode_id=4, phy='audio', audio_port='mic')
        Station(station_id='K0MDT', dst_id='W1AW', mode_id=3, phy='audio', audio_port='mic')
        Station(station_id='K0MDT', dst_id='W1AW', mode_id=3, phy='audio', audio_port='data')

    def test_port_ceiling_is_hard_and_bandwidth_cannot_lift_it(self):
        async def scenario():
            logs = []
            mic = Station(station_id='K0MDT', dst_id='W1AW', mode_id=2, phy='audio',
                          audio_port='mic', on_log=logs.append)
            self.assertEqual(mic.max_symbol_rate, 2400)
            await _handle_command(mic, '/bandwidth wide')
            self.assertEqual(mic.max_symbol_rate, 2400)
            await _handle_command(mic, '/mode 4')
            self.assertEqual(mic.mode_id, 2)
            self.assertTrue(any('mic audio port' in line for line in logs), logs)

            data = Station(station_id='K0MDT', dst_id='W1AW', phy='audio', audio_port='data')
            self.assertEqual(data.max_symbol_rate, 2400)
            await _handle_command(data, '/bandwidth narrow')
            self.assertEqual(data.max_symbol_rate, NARROWBAND_MAX_SYMBOL_RATE)
            await _handle_command(data, '/bandwidth wide')
            self.assertEqual(data.max_symbol_rate, 2400)

        self._run(scenario())


@unittest.skipUnless(OTA_SIM_AVAILABLE, 'ota_sim not installed (pip install -e ../ota_sim)')
class PingSurveySummaryTests(unittest.TestCase):
    """_ping_survey_summary() is a pure function over ping_survey()'s
    collected per-mode results - no simulator/asyncio needed to test its
    "best fully-reliable mode" selection logic directly."""

    def _row(self, mode_id, oks):
        results = [{'ok': ok, 'rtt_s': 0.05 if ok else None,
                    'snr_db': 20.0 if ok else None} for ok in oks]
        n_ok = sum(oks)
        summary = f"{n_ok}/{len(oks)} ok"
        return (mode_id, results, summary)

    def test_picks_highest_throughput_fully_reliable_mode(self):
        rows = [
            self._row(0, [True, True]),
            self._row(1, [True, True]),
            self._row(2, [True, False]),  # not fully reliable - skipped
            self._row(3, [True, True]),
            self._row(4, [False, False]),
        ]
        summary = _ping_survey_summary(rows)
        self.assertIn('best fully-reliable mode: Mode 3', summary)

    def test_ranks_by_actual_throughput_not_row_order(self):
        # The faster mode (5, 16-QAM @ 9600 sym/s) listed FIRST, the
        # slower one (0, BPSK @ 600 sym/s) LAST - the opposite of
        # mode_id/throughput order. A naive "last fully-reliable row
        # wins" selection would wrongly pick mode 0; ranking by actual
        # throughput (link.mode_raw_bps()) picks mode 5 regardless of
        # row order. Uses real WAVEFORM_MODES entries (not fake mode_ids)
        # since _ping_survey_summary() looks each one up to build its
        # display label - but deliberately doesn't rely on the real
        # table's mode_id ordering to make the point.
        rows = [self._row(5, [True, True]), self._row(0, [True, True])]
        summary = _ping_survey_summary(rows)
        self.assertIn('best fully-reliable mode: Mode 5', summary)

    def test_no_fully_reliable_mode(self):
        rows = [self._row(0, [True, False]), self._row(1, [False, False])]
        summary = _ping_survey_summary(rows)
        self.assertIn('no mode was fully reliable', summary)

    def test_all_modes_listed(self):
        rows = [self._row(mode_id, [True]) for mode_id in sorted(link.WAVEFORM_MODES)]
        summary = _ping_survey_summary(rows)
        for mode_id in link.WAVEFORM_MODES:
            self.assertIn(f'Mode {mode_id}', summary)


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(OTA_SIM_AVAILABLE, 'ota_sim not installed (pip install -e ../ota_sim)')
class BatchingOverFMTests(unittest.TestCase):
    """Burst batching part 2 (BURST_BATCHING_PROPOSAL.md): --auto --ack
    stations through ota_sim's FM model send a multi-segment message in
    batches and acknowledge it with selective ACKs."""

    TEXT = ''.join(f"segment test line {i:03d}. " for i in range(40))   # ~1000 chars

    async def _pair(self, a_kw=None, b_kw=None, seed=3):
        sim = await _create_simulator(FMLink('mic', base_cnr_db=25, latency_s=0.01, jitter_s=0.0,
                                             seed=seed))
        received = []
        quiet = lambda msg: None  # noqa: E731
        a = await _create_station('K0MDT', 'W1AW', use_ack=True, auto=True, phy='audio',
                                  audio_port='mic', on_log=quiet, **(a_kw or {}))
        b = await _create_station('W1AW', 'K0MDT', use_ack=True, auto=True, phy='audio',
                                  audio_port='mic', on_message=received.append, on_log=quiet,
                                  **(b_kw or {}))
        for s in (a, b):
            await s.connect(*sim.address)
        await asyncio.sleep(0.05)
        return sim, a, b, received

    @staticmethod
    def _count_data_frames(station):
        sent = []
        original = station._transmit_frame
        def counting(frame_bytes, *args, **kw):
            at = frame_bytes.find(link.IQ_MARKER) + len(link.IQ_MARKER)
            header = link.parse_header(frame_bytes[at:at + link.HEADER_LEN])
            if header['packet_type'] == link.PACKET_TYPE_DATA:
                sent.append(header['fragment_index'])
            return original(frame_bytes, *args, **kw)
        station._transmit_frame = counting
        return sent

    def _finish(self, sim, a, b):
        a.close()
        b.close()
        sim.close()

    def test_a_message_goes_in_one_batch_and_one_selective_ack(self):
        async def scenario():
            sim, a, b, received = await self._pair()
            await asyncio.wait_for(a.negotiate(), timeout=40)
            a._auto_negotiate_done = True
            a.max_fragment_chars = 200      # 5 segments of short bursts (negotiation resets it)
            sent = self._count_data_frames(a)
            acks = []
            original_ack = b._send_selective_ack
            b._send_selective_ack = lambda *args: (acks.append(1), original_ack(*args))
            await asyncio.wait_for(a.send_text(self.TEXT), timeout=60)
            ok = await _wait_for(lambda: received, timeout_s=10)
            self._finish(sim, a, b)
            self.assertTrue(a.batch_active and b.batch_active)
            self.assertTrue(ok)
            self.assertEqual(received, [self.TEXT])
            self.assertEqual(sorted(sent), [0, 1, 2, 3, 4])    # each segment sent once
            self.assertEqual(len(acks), 1)                     # one ACK for the batch
        asyncio.run(scenario())

    def test_only_the_lost_segment_is_resent(self):
        self._lose_one_segment(2, seed=4)

    def test_losing_the_segment_that_asks_for_the_ack(self):
        # The receiver never sees the ACK request, so it acknowledges once
        # the batch has gone quiet; the sender then resends just that one.
        self._lose_one_segment(4, seed=6)

    def _lose_one_segment(self, lost_index, seed):
        async def scenario():
            sim, a, b, received = await self._pair(seed=seed)
            await asyncio.wait_for(a.negotiate(), timeout=40)
            a._auto_negotiate_done = True
            a.max_fragment_chars = 200      # 5 segments of short bursts (negotiation resets it)
            sent = self._count_data_frames(a)
            dropped = []
            original_feed = b.receiver.feed_frame
            def lossy(header, payload):
                if header['packet_type'] == link.PACKET_TYPE_DATA \
                        and header['fragment_index'] == lost_index and not dropped:
                    dropped.append(1)      # this segment is lost on the air, once
                    return
                original_feed(header, payload)
            b.receiver.feed_frame = lossy
            await asyncio.wait_for(a.send_text(self.TEXT), timeout=90)
            ok = await _wait_for(lambda: received, timeout_s=10)
            self._finish(sim, a, b)
            self.assertTrue(ok)
            self.assertEqual(received, [self.TEXT])
            self.assertEqual(sorted(sent), sorted([0, 1, 2, 3, 4, lost_index]))  # just that one again
        asyncio.run(scenario())

    def test_a_station_without_batching_gets_stop_and_wait(self):
        async def scenario():
            sim, a, b, received = await self._pair(b_kw={'batching': False}, seed=5)
            await asyncio.wait_for(a.negotiate(), timeout=40)
            a._auto_negotiate_done = True
            a.max_fragment_chars = 200      # 5 segments of short bursts (negotiation resets it)
            await asyncio.wait_for(a.send_text(self.TEXT), timeout=120)
            ok = await _wait_for(lambda: received, timeout_s=10)
            self._finish(sim, a, b)
            self.assertFalse(a.batch_active or b.batch_active)
            self.assertTrue(ok)
            self.assertEqual(received, [self.TEXT])
        asyncio.run(scenario())


class BatchingOverRadioTests(unittest.TestCase):
    """A batched message through the real radio transport over an in-memory
    'air' (fmlink_radio.SimulatedAir): the burst detector must hand the
    station each segment of one transmission cleanly, so the whole message
    goes in one round with no resends."""

    def test_one_round_no_resends(self):
        import fmlink_radio as radio

        async def scenario():
            air = radio.SimulatedAir(noise_rms=0.02)
            received = []
            quiet = lambda msg: None  # noqa: E731

            def make(name, peer, sink=None):
                return Station(name, peer, mode_id=3, use_fec=True, fec_preset='r1_2_k3',
                               use_ack=True, phy='audio', audio_port='mic', on_message=sink,
                               on_log=quiet)

            a, b = make('K0AAA', 'K0BBB'), make('K0BBB', 'K0AAA', received.append)
            for station in (a, b):
                port = air.port()
                await station.connect_radio({'rx_device': None, 'rx_channels': 1},
                                            capture=port.capture, player=port.player)
            for station in (a, b):                  # as negotiation would leave them
                station._set_peer_capabilities(link.CAP_BATCH)
            a.max_fragment_chars = 300
            text = ''.join(f"radio batch line {i:03d}. " for i in range(50))[:1200]   # 4 segments
            sent = []
            original = a._transmit_frame
            a._transmit_frame = lambda f, *args, **kw: (sent.append(1), original(f, *args, **kw))
            await asyncio.wait_for(a.send_text(text), timeout=60)
            ok = await _wait_for(lambda: received, timeout_s=10)
            a.close()
            b.close()
            self.assertTrue(ok)
            self.assertEqual(received, [text])
            self.assertEqual(len(sent), 4)          # every segment once: no resend round

        asyncio.run(scenario())


@unittest.skipUnless(OTA_SIM_AVAILABLE, 'ota_sim not installed (pip install -e ../ota_sim)')
class EventFeedTests(unittest.TestCase):
    """Station(on_event=...): the structured events a live display
    (fmlink_web) gets - every one JSON-serializable."""

    def test_a_message_exchange_over_fm(self):
        import json

        async def scenario():
            sim = await _create_simulator(FMLink('mic', base_cnr_db=25, latency_s=0.01, jitter_s=0.0,
                                                 seed=7))
            ev_a, ev_b, received = [], [], []
            quiet = lambda msg: None  # noqa: E731
            a = await _create_station('K0MDT', 'W1AW', mode_id=2, use_fec=True, use_ack=True,
                                      phy='audio', audio_port='mic', on_log=quiet,
                                      on_event=ev_a.append)
            b = await _create_station('W1AW', 'K0MDT', mode_id=2, use_fec=True, use_ack=True,
                                      phy='audio', audio_port='mic', on_log=quiet,
                                      on_message=received.append, on_event=ev_b.append)
            await a.connect(*sim.address)
            await b.connect(*sim.address)
            await asyncio.sleep(0.05)
            await asyncio.wait_for(a.send_text("event feed test"), timeout=20)
            await _wait_for(lambda: received, timeout_s=5)
            a.close()
            b.close()
            sim.close()

            for event in ev_a + ev_b:
                json.dumps(event)                        # what the web backend will send
                self.assertIn('time', event)
            kinds_a = [e['type'] for e in ev_a]
            self.assertEqual(ev_a[0]['type'], 'link')       # the initial setup
            self.assertEqual(ev_a[0]['mode_id'], 2)
            sent = next(e for e in ev_a if e['type'] == 'message_tx')
            self.assertEqual((sent['text'], sent['segments'], sent['ack']), ("event feed test", 1, True))
            done = [e for e in ev_a if e['type'] == 'delivery']
            self.assertEqual(done[-1]['id'], sent['id'])
            self.assertTrue(done[-1]['done'] and not done[-1]['failed'])
            self.assertIn('tx', kinds_a)
            self.assertIn('log', kinds_a)
            burst = next(e for e in ev_b if e['type'] == 'burst_rx')
            self.assertEqual((burst['status'], burst['mode_id'], burst['src']), ('ok', 2, 'K0MDT'))
            self.assertGreater(burst['snr_db'], 10)
            self.assertGreater(burst['quality'], 0.5)
            got = next(e for e in ev_b if e['type'] == 'message_rx')
            self.assertEqual((got['src'], got['text']), ('K0MDT', "event feed test"))
            points = next(e for e in ev_b if e['type'] == 'constellation')['points']
            self.assertTrue(0 < len(points) <= 300 and len(points[0]) == 2)

        asyncio.run(scenario())

    def test_a_failing_handler_does_not_break_the_station(self):
        def broken(event):
            raise RuntimeError('display bug')
        import contextlib, io
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            station = Station(station_id='K0MDT', dst_id='W1AW', on_event=broken, on_log=lambda m: None)
            station._log("still fine")          # emits a 'log' event; must not raise
        self.assertEqual(station.link_state()['station_id'], 'K0MDT')
        self.assertIn('display bug', errors.getvalue())   # reported, not raised


class RadioEventTests(unittest.TestCase):
    def test_tx_and_level_events_over_simulated_air(self):
        import fmlink_radio as radio

        async def scenario():
            air = radio.SimulatedAir(noise_rms=0.02)
            events = []
            a = Station('K0AAA', 'K0BBB', mode_id=2, use_fec=True, phy='audio', audio_port='mic',
                        on_log=lambda m: None, on_event=events.append)
            port = air.port()
            await a.connect_radio({'rx_device': None, 'rx_channels': 1}, capture=port.capture,
                                  player=port.player)
            a._transmit_frame(link.build_frame(b'x' * 20, seq=1, mode_id=2))
            await _wait_for(lambda: any(e['type'] == 'tx' and e['state'] == 'released'
                                        for e in events), timeout_s=10)
            a.close()
            tx = [e['state'] for e in events if e['type'] == 'tx']
            self.assertEqual(tx[:3], ['queued', 'keyed', 'released'])
            levels = [e for e in events if e['type'] == 'level']
            self.assertTrue(levels)
            self.assertTrue(all(e['rms_dbfs'] <= 0 and 'transmitting' in e for e in levels))

        asyncio.run(scenario())


class LiveSettingsTests(unittest.TestCase):
    """Station.update_settings(): settings changed while running, all-or-nothing."""

    def _station(self, **kw):
        events = []
        st = Station('K0MDT', 'W1AW', mode_id=2, use_fec=True, phy='audio', audio_port='mic',
                     on_log=lambda m: None, on_event=events.append, **kw)
        return st, events

    def test_invalid_changes_apply_nothing(self):
        async def scenario():
            st, _ = self._station()
            before = st.link_state()
            for bad in ({'nonsense': 1}, {'mode_id': 9}, {'mode_id': 4}, {'max_tx_s': 0.5},
                        {'ack': 'yes'}, {'bandwidth': 'huge'}, {'fec_preset': 'r9_9'},
                        {'tx_level': 0.5},                       # no radio connected
                        {'lead_in_s': 0.3, 'max_tx_s': 1000}):   # one good, one bad
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        await st.update_settings(**bad)
                    self.assertEqual(st.link_state(), before)
            with self.assertRaises(ValueError) as ctx:
                await st.update_settings(max_tx_s=-1, ack='x')
            self.assertIn('max_tx_s', str(ctx.exception))
            self.assertIn('ack', str(ctx.exception))   # every problem named at once
        asyncio.run(scenario())

    def test_local_settings_apply_and_emit_link(self):
        async def scenario():
            st, events = self._station()
            state = await st.update_settings(lead_in_s=0.4, max_tx_s=20, ack=True, auto=True,
                                             bandwidth='narrow', batching=False)
            self.assertEqual((state['lead_in_s'], state['max_tx_s'], state['ack'], state['auto'],
                              state['bandwidth'], state['batching']),
                             (0.4, 20.0, True, True, 'narrow', False))
            self.assertEqual(st.max_symbol_rate, NARROWBAND_MAX_SYMBOL_RATE)
            self.assertEqual(events[-1]['type'], 'link')
            self.assertEqual(events[-1]['max_tx_s'], 20.0)
        asyncio.run(scenario())

    def test_callsigns(self):
        async def scenario():
            st, events = self._station()
            st._set_peer_capabilities(link.CAP_BATCH)
            st._auto_negotiate_done = True
            for bad in ({'station_id': ''}, {'dst_id': 'WAY-TOO-LONG-CALL'}, {'dst_id': 'K0#DT'},
                        {'station_id': 7}):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        await st.update_settings(**bad)
            self.assertTrue(st.batch_active)            # nothing changed by the bad ones
            state = await st.update_settings(station_id=' k0mdt-1 ', dst_id='w1aw-2')
            self.assertEqual((state['station_id'], state['dst_id']), ('K0MDT-1', 'W1AW-2'))
            self.assertEqual(st.receiver.station_id, 'K0MDT-1')   # frames for the new call reach us
            self.assertFalse(st.batch_active)          # a new pair: negotiate again
            self.assertFalse(st._auto_negotiate_done)
            self.assertEqual(events[-1]['dst_id'], 'W1AW-2')
        asyncio.run(scenario())

    def test_radio_settings(self):
        import fmlink_radio as radio

        async def scenario():
            st, _ = self._station()
            port = radio.SimulatedAir().port()
            await st.connect_radio({'rx_device': None, 'rx_channels': 1}, capture=port.capture,
                                   player=port.player)
            state = await st.update_settings(tx_level=0.6, turnaround_s=3.0)
            st.close()
            self.assertEqual((state['tx_level'], state['turnaround_s']), (0.6, 3.0))
            self.assertEqual(st._sim.tx_level, 0.6)
        asyncio.run(scenario())

    def test_commands(self):
        async def scenario():
            logs = []
            st = Station('K0MDT', 'W1AW', phy='audio', audio_port='mic', on_log=logs.append)
            await _handle_command(st, '/max-tx 20')
            await _handle_command(st, '/batch off')
            await _handle_command(st, '/tx-level')
            await _handle_command(st, '/turnaround 3000')
            self.assertEqual((st.max_tx_s, st.batching), (20.0, False))
            self.assertTrue(any('n/a (no radio)' in line for line in logs), logs)
            self.assertTrue(any('only apply to a real radio' in line for line in logs), logs)
        asyncio.run(scenario())


@unittest.skipUnless(OTA_SIM_AVAILABLE, 'ota_sim not installed (pip install -e ../ota_sim)')
class LiveSettingsOverFMTests(unittest.TestCase):
    def test_mode_and_batching_changes_reach_the_peer(self):
        async def scenario():
            sim = await _create_simulator(FMLink('mic', base_cnr_db=25, latency_s=0.01, jitter_s=0.0,
                                                 seed=8))
            quiet = lambda msg: None  # noqa: E731
            a = await _create_station('K0MDT', 'W1AW', use_ack=True, auto=True, phy='audio',
                                      audio_port='mic', on_log=quiet)
            b = await _create_station('W1AW', 'K0MDT', use_ack=True, auto=True, phy='audio',
                                      audio_port='mic', on_log=quiet)
            await a.connect(*sim.address)
            await b.connect(*sim.address)
            await asyncio.sleep(0.05)
            await asyncio.wait_for(a.negotiate(), timeout=40)
            a._auto_negotiate_done = True
            self.assertTrue(a.batch_active and b.batch_active)

            await asyncio.wait_for(a.update_settings(mode_id=2, fec_preset='r1_2_k7'), timeout=30)
            self.assertEqual((b.mode_id, b.fec_preset), (2, 'r1_2_k7'))      # the peer followed
            await asyncio.wait_for(a.update_settings(batching=False), timeout=30)
            self.assertFalse(a.batch_active)
            self.assertFalse(b.batch_active)                                 # and agrees
            a.close()
            b.close()
            sim.close()
        asyncio.run(scenario())


class RadioTestTests(unittest.TestCase):
    """Station.radio_test() and link_state()'s 'radio', over SimulatedAir."""

    def test_carrier_and_tone(self):
        import fmlink_radio as radio

        async def scenario():
            air = radio.SimulatedAir(noise_rms=0.0)
            events, heard = [], []
            st = Station('K0MDT', 'W1AW', mode_id=2, use_fec=True, phy='audio', audio_port='mic',
                         on_log=lambda m: None, on_event=events.append)
            self.assertIsNone(st.link_state()['radio'])
            with self.assertRaises(ValueError):
                st.radio_test('tone', 1.0)                 # not on a radio yet
            port = air.port()
            played = []
            await st.connect_radio({'name': 'aioc', 'host': 'localhost', 'rx_device': 'hw:X',
                                    'tx_device': 'plughw:X', 'ptt_port': '/dev/ttyACM0',
                                    'ptt_line': 'dtr', 'rx_channels': 1},
                                   capture=port.capture,
                                   player=lambda pcm: (played.append(pcm), port.player(pcm)))
            radio_info = st.link_state()['radio']
            self.assertEqual((radio_info['name'], radio_info['ptt_line'], radio_info['ptt_port']),
                             ('aioc', 'dtr', '/dev/ttyACM0'))
            for bad in (('noise', 1.0), ('tone', 9), ('tone', 0.1), ('tone', True)):
                with self.subTest(bad=bad):
                    with self.assertRaises(ValueError):
                        st.radio_test(*bad)
            st.radio_test('carrier', 0.5)
            st.radio_test('tone', 0.5)
            await _wait_for(lambda: len(played) == 2, timeout_s=10)
            st.close()
            carrier, tone = (np.frombuffer(p, '<i2') / 32768 for p in played)
            self.assertEqual(np.abs(carrier).max(), 0.0)               # silence
            self.assertAlmostEqual(np.abs(tone).max(), st._sim.tx_level, places=2)  # at the TX level
            self.assertEqual(len(tone), 24000)
            spectrum = np.abs(np.fft.rfft(tone))
            self.assertAlmostEqual(np.argmax(spectrum) * 48000 / len(tone), 1000.0, delta=5)
            keyed = [e for e in events if e['type'] == 'tx' and e['state'] == 'keyed']
            self.assertEqual(len(keyed), 2)

        asyncio.run(scenario())
