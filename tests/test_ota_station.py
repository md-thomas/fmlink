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

import openwave_link as link

try:
    from ota_sim import OTALink
    from ota_sim.simulator import OTASimulator
    OTA_SIM_AVAILABLE = True
except ImportError:
    OTA_SIM_AVAILABLE = False

if OTA_SIM_AVAILABLE:
    from ota_station import Station, _ping_survey_summary, _handle_command, NARROWBAND_MAX_SYMBOL_RATE

TIMEOUT_S = 5.0


async def _create_station(station_id, dst_id, **kwargs):
    station = Station(station_id=station_id, dst_id=dst_id, **kwargs)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: station, local_addr=('127.0.0.1', 0))
    return station, transport


async def _create_simulator(channel):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: OTASimulator(channel), local_addr=('127.0.0.1', 0))
    return protocol, transport


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
            sim, sim_transport = await _create_simulator(channel)

            received = []
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=0,
                                                     on_message=None)
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=0,
                                                     on_message=received.append)

            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
            await asyncio.sleep(0.05)  # let both registrations land before sending

            await a.send_text("integration test message")

            ok = await _wait_for(lambda: len(received) > 0)

            a_transport.close()
            b_transport.close()
            sim_transport.close()

            self.assertTrue(ok, "message never arrived within timeout")
            self.assertEqual(received[0], "integration test message")

        self._run(scenario())

    def test_ack_requested_message_gets_acknowledged(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=2)
            sim, sim_transport = await _create_simulator(channel)

            received = []
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=0, use_ack=True)
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=0, use_ack=True,
                                                     on_message=received.append)

            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
            await asyncio.sleep(0.05)  # let both registrations land before sending

            await a.send_text("ack test")

            ok = await _wait_for(lambda: not a.retry_mgr.is_pending)

            a_transport.close()
            b_transport.close()
            sim_transport.close()

            self.assertTrue(ok, "ACK never resolved within timeout")
            self.assertEqual(received, ["ack test"])

        self._run(scenario())

    def test_negotiate_selects_mode_from_channel_snr(self):
        # Regression coverage for the exact miscalibration this feature hit
        # live: a channel at openwave_fec.py's proven-good 0dB anchor must
        # select r1_2_k3, and one at its proven-bad -6dB anchor must fall
        # back to r1_4_k9 - see openwave_link.py's MODE_SNR_TABLE comment
        # for the real measurements behind these thresholds.
        async def negotiate_at(base_snr_db, seed):
            channel = OTALink(distance_m=1000, reference_distance_m=1000,
                               base_snr_db=base_snr_db, fading=False, drop_prob=0.0,
                               latency_s=0.005, jitter_s=0.0, seed=seed)
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=0,
                                                     use_fec=True, use_ack=True)
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=0,
                                                     use_fec=True, use_ack=True)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
            await asyncio.sleep(0.02)
            await a.negotiate()
            result = (a.mode_id, a.fec_preset)
            a_transport.close()
            b_transport.close()
            sim_transport.close()
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
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=0,
                                                     use_fec=True, use_ack=True)
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=0,
                                                     use_fec=True, use_ack=True)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
            await asyncio.sleep(0.02)

            await a.negotiate()  # only A initiates - B never calls negotiate() itself

            a_transport.close()
            b_transport.close()
            sim_transport.close()

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
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=0,
                                                     use_fec=True, use_ack=True, bandwidth='narrow')
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=0,
                                                     use_fec=True, use_ack=True)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
            await asyncio.sleep(0.02)

            await a.negotiate()

            a_transport.close()
            b_transport.close()
            sim_transport.close()

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
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=0, use_fec=True)
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=0, use_fec=True)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
            await asyncio.sleep(0.02)

            await _handle_command(a, '/mode 3')

            a_transport.close()
            b_transport.close()
            sim_transport.close()

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
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=2, use_fec=True)
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=2, use_fec=True)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
            await asyncio.sleep(0.02)

            await _handle_command(a, '/fec r1_4_k9')

            a_transport.close()
            b_transport.close()
            sim_transport.close()

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
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=1)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            a.register()
            await asyncio.sleep(0.02)
            self.assertFalse(a.use_fec, "sanity check - station must start without FEC")

            await _handle_command(a, '/fec r1_2_k9')

            a_transport.close()
            sim_transport.close()

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
                         '/help', '/quit'):
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
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=1, use_fec=True)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            a.register()
            await asyncio.sleep(0.02)

            await _handle_command(a, '/mode 99')

            a_transport.close()
            sim_transport.close()

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
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=1,
                                                     use_fec=True, bandwidth='narrow')
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            a.register()
            await asyncio.sleep(0.02)

            await _handle_command(a, '/mode 5')  # 16-QAM @ 9600 sym/s - over the 1200 ceiling

            a_transport.close()
            sim_transport.close()

            self.assertEqual(a.mode_id, 1, "must stay on the original mode, not switch to 5")
            self.assertFalse(a._auto_negotiate_done)

        self._run(scenario())

    def test_fec_command_rejects_unknown_preset_without_changing_anything(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=11)
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=1, use_fec=True)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            a.register()
            await asyncio.sleep(0.02)
            original_fec_preset = a.fec_preset

            await _handle_command(a, '/fec not-a-real-preset')

            a_transport.close()
            sim_transport.close()

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
            sim, sim_transport = await _create_simulator(channel)

            received = []
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=0,
                                                     use_fec=True, use_ack=True, auto=True)
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=0,
                                                     use_fec=True, use_ack=True,
                                                     on_message=received.append)

            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
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

            a_transport.close()
            b_transport.close()
            sim_transport.close()

        self._run(scenario())

    def test_ping_trials_measure_round_trip(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=4)
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=0)
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=0)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
            await asyncio.sleep(0.02)

            results, summary = await a._ping_trials(count=3)

            a_transport.close()
            b_transport.close()
            sim_transport.close()

            self.assertEqual(len(results), 3)
            self.assertTrue(all(r['ok'] for r in results), results)
            self.assertTrue(all(r['rtt_s'] > 0 for r in results), results)
            self.assertIn('3/3 ok', summary)

        self._run(scenario())

    def test_ping_survey_tests_every_mode_and_restores_original(self):
        async def scenario():
            channel = OTALink(distance_m=500, reference_distance_m=500,
                               base_snr_db=60, fading=False, drop_prob=0.0,
                               latency_s=0.01, jitter_s=0.0, seed=5)
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=1, use_fec=True)
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=1, use_fec=True)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
            await asyncio.sleep(0.02)

            original = (a.mode_id, a.fec_preset)

            visited_modes = []
            original_announce = a._announce_mode

            async def tracking_announce(mode_id, fec_preset=None):
                visited_modes.append(mode_id)
                return await original_announce(mode_id, fec_preset)
            a._announce_mode = tracking_announce

            await a.ping_survey()

            a_transport.close()
            b_transport.close()
            sim_transport.close()

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
            sim, sim_transport = await _create_simulator(channel)
            a, a_transport = await _create_station('K0MDT', 'W1AW', mode_id=1,
                                                     use_fec=True, bandwidth='narrow')
            b, b_transport = await _create_station('W1AW', 'K0MDT', mode_id=1, use_fec=True)
            sim_addr = sim_transport.get_extra_info('sockname')
            a._sim_addr = sim_addr
            b._sim_addr = sim_addr
            a.register()
            b.register()
            await asyncio.sleep(0.02)

            visited_modes = []
            original_announce = a._announce_mode

            async def tracking_announce(mode_id, fec_preset=None):
                visited_modes.append(mode_id)
                return await original_announce(mode_id, fec_preset)
            a._announce_mode = tracking_announce

            await a.ping_survey()

            a_transport.close()
            b_transport.close()
            sim_transport.close()

            expected = [m for m in sorted(link.WAVEFORM_MODES)
                        if link.WAVEFORM_MODES[m]['symbol_rate'] <= 1200]
            self.assertEqual(expected, [0, 1, 2])  # sanity-check the ceiling picked modes 0-2
            self.assertEqual(visited_modes, expected + [1])  # +1 to restore the original mode

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
