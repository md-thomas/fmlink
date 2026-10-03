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

import openwave_audio as audio_phy
import openwave_fec as fec
import openwave_link as link

try:
    from ota_sim import FMLink, OTALink
    from ota_sim.simulator import OTASimulator
    OTA_SIM_AVAILABLE = True
except ImportError:
    OTA_SIM_AVAILABLE = False

if OTA_SIM_AVAILABLE:
    from ota_station import Station, _ping_survey_summary, _handle_command, NARROWBAND_MAX_SYMBOL_RATE

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
        # live: a channel at openwave_fec.py's proven-good 0dB anchor must
        # select r1_2_k3, and one at its proven-bad -6dB anchor must fall
        # back to r1_4_k9 - see openwave_link.py's MODE_SNR_TABLE comment
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
        # Mode 1 bursts air for ~1.4 s each way - longer than the old fixed
        # 2 s reply timeout in total, which would retry (and collide, on a
        # half-duplex link) before the ACK could ever arrive.
        async def scenario():
            sim = await _create_simulator(self._fm_channel(seed=2))
            received = []
            a = await _create_station('K0MDT', 'W1AW', mode_id=1, use_fec=True, use_ack=True,
                                      phy='audio', audio_port='mic')
            b = await _create_station('W1AW', 'K0MDT', mode_id=1, use_fec=True, use_ack=True,
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
        request = link.build_mode_request_frame(7, mode_id=0, src_id=b'W1AW', dst_id=b'K0MDT')
        coded = fec.encode_frame(request, sender._control_fec_generators, sender._control_fec_k)
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

    def test_strip_frame_padding_keeps_exactly_the_frame(self):
        from ota_station import _strip_frame_padding
        frame = link.build_frame(b'payload', seq=3)
        stripped = _strip_frame_padding(frame)
        self.assertTrue(stripped.startswith(link.PREAMBLE + link.SYNC_WORD))
        self.assertEqual(len(stripped), len(frame) - 48)  # build_frame's 24 + 24 pad bytes
        self.assertEqual(_strip_frame_padding(stripped), stripped)
        self.assertEqual(_strip_frame_padding(b'no marker here'), b'no marker here')

    def test_audio_bursts_drop_the_padding_airtime(self):
        # At 600 baud the 48 uncoded pad bytes are 384 symbols = 0.64 s.
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
        self.assertAlmostEqual(padded_s - sent_s, 0.64, delta=0.01)
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
            await _handle_command(a, '/lead-in 750')
            a._transmit_frame(frame)
            first, second = (len(p) / 2 / audio_phy.AUDIO_SAMPLE_RATE for p in a._sim.sent)
            self.assertAlmostEqual(second - first, 0.5, places=3)

        self._run(scenario())

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
            Station(station_id='K0MDT', dst_id='W1AW', mode_id=3, phy='audio', audio_port='mic')
        Station(station_id='K0MDT', dst_id='W1AW', mode_id=3, phy='audio', audio_port='data')

    def test_port_ceiling_is_hard_and_bandwidth_cannot_lift_it(self):
        async def scenario():
            logs = []
            mic = Station(station_id='K0MDT', dst_id='W1AW', mode_id=2, phy='audio',
                          audio_port='mic', on_log=logs.append)
            self.assertEqual(mic.max_symbol_rate, 1200)
            await _handle_command(mic, '/bandwidth wide')
            self.assertEqual(mic.max_symbol_rate, 1200)
            await _handle_command(mic, '/mode 3')
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
