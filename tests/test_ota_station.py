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

            a.send_text("integration test message")

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

            a.send_text("ack test")

            ok = await _wait_for(lambda: not a.retry_mgr.is_pending)

            a_transport.close()
            b_transport.close()
            sim_transport.close()

            self.assertTrue(ok, "ACK never resolved within timeout")
            self.assertEqual(received, ["ack test"])

        self._run(scenario())


if __name__ == '__main__':
    unittest.main()
