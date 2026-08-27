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

try:
    from ota_sim import OTALink
    from ota_sim.simulator import OTASimulator
    OTA_SIM_AVAILABLE = True
except ImportError:
    OTA_SIM_AVAILABLE = False

if OTA_SIM_AVAILABLE:
    from ota_station import Station

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


if __name__ == '__main__':
    unittest.main()
