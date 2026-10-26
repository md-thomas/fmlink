"""
Tests for fmlink_kiss.py (KISS framing, AX.25 address helpers) and
fmlink_tnc.py (two TNCs relaying KISS frames through ota_sim). The TNC
tests need ota_sim installed (pip install -e ../ota_sim) and are skipped
otherwise.
"""
import asyncio
import os
import unittest

import fmlink_kiss as kiss
import fmlink_link as link

import sibling_ota_sim  # noqa: F401 - finds ../ota_sim if it isn't installed

try:
    from ota_sim import FMLink
    from ota_sim.simulator import OTASimulator
    import fmlink_tnc as tnc
    OTA_SIM_AVAILABLE = True
except ImportError:
    OTA_SIM_AVAILABLE = False


class KissFramingTests(unittest.TestCase):
    def test_round_trip_with_escapes(self):
        data = bytes([0x01, kiss.FEND, 0x02, kiss.FESC, 0x03])
        encoded = kiss.encode(data)
        self.assertEqual(encoded, bytes([0xC0, 0x00, 0x01, 0xDB, 0xDC, 0x02, 0xDB, 0xDD, 0x03, 0xC0]))
        self.assertEqual(kiss.Decoder().feed(encoded), [(0, kiss.CMD_DATA, data)])

    def test_any_chunking(self):
        frames = [bytes(range(i, i + 20)) + bytes([kiss.FEND]) for i in range(5)]
        stream = b''.join(kiss.encode(f) for f in frames)
        for size in (1, 2, 3, 7, len(stream)):
            with self.subTest(size=size):
                decoder, out = kiss.Decoder(), []
                for i in range(0, len(stream), size):
                    out += decoder.feed(stream[i:i + size])
                self.assertEqual([d for _, _, d in out], frames)

    def test_commands_ports_and_padding(self):
        stream = (bytes([kiss.FEND, kiss.FEND]) + kiss.encode(b'\x32', command=kiss.CMD_TXDELAY)
                  + kiss.encode(b'abc', port=2) + bytes([kiss.FEND, 0xFF, kiss.FEND]))
        self.assertEqual(kiss.Decoder().feed(stream),
                         [(0, kiss.CMD_TXDELAY, b'\x32'), (2, kiss.CMD_DATA, b'abc'),
                          (0, kiss.CMD_RETURN, b'')])

    def test_noise_before_first_fend_ignored(self):
        self.assertEqual(kiss.Decoder().feed(b'junk' + kiss.encode(b'x')), [(0, 0, b'x')])


class CrcVariantTests(unittest.TestCase):
    """SMACK and FlexNet, as Linux's mkiss sends them in its default auto mode."""

    def test_smack_crc_is_crc16_arc(self):
        self.assertEqual(kiss.crc_smack(b'123456789'), 0xBB3D)  # the standard check value

    def test_flexnet_table_matches_mkiss(self):
        self.assertEqual(kiss._FLEX_TABLE[:8],
                         [0x0F87, 0x1E0E, 0x2C95, 0x3D1C, 0x49A3, 0x582A, 0x6AB1, 0x7B38])

    def test_round_trip_and_mode_tracking(self):
        frame = kiss.ax25_ui_frame('K0MDT-1', 'K0MDT-2', bytes(range(256)))
        for mode in (kiss.CRC_SMACK, kiss.CRC_FLEXNET, kiss.CRC_NONE):
            with self.subTest(mode=mode):
                decoder = kiss.Decoder()
                self.assertEqual(decoder.feed(kiss.encode(frame, crc=mode)), [(0, kiss.CMD_DATA, frame)])
                self.assertEqual(decoder.crc_mode, mode)

    def test_flag_bits_without_a_valid_crc_are_ports(self):
        # A plain frame for port 2 (type 0x20) or port 8 (0x80) must not be
        # mistaken for FlexNet / SMACK just because the flag bit is set.
        decoder = kiss.Decoder()
        self.assertEqual(decoder.feed(kiss.encode(b'port two', port=2)), [(2, 0, b'port two')])
        self.assertEqual(decoder.feed(kiss.encode(b'port eight', port=8)), [(8, 0, b'port eight')])

    def test_corrupted_crc_is_not_accepted_as_crc_frame(self):
        encoded = bytearray(kiss.encode(b'hello', crc=kiss.CRC_SMACK))
        encoded[3] ^= 0x01  # flip a bit in the data
        (port, command, data), = kiss.Decoder().feed(bytes(encoded))
        self.assertEqual(port, 8)  # fell back to "plain frame for port 8", which the TNC ignores


class Ax25AddressTests(unittest.TestCase):
    def test_ui_frame_endpoints(self):
        frame = kiss.ax25_ui_frame('K0MDT-7', 'W1AW', b'hello')
        self.assertEqual(kiss.ax25_endpoints(frame), ('K0MDT-7', 'W1AW'))
        self.assertEqual(frame[14:16], b'\x03\xf0')
        self.assertEqual(frame[16:], b'hello')
        self.assertEqual(frame[13] & 1, 1)  # source is the last address

    def test_too_short(self):
        self.assertEqual(kiss.ax25_endpoints(b'short'), ('', ''))

    def test_ssid_15_fits_an_fmlink_callsign(self):
        call = kiss.ax25_address(kiss.ax25_address_field('VE3XYZ-15'))
        self.assertEqual(call, 'VE3XYZ-15')
        link.pack_callsign(call)  # 9 characters: must not raise


@unittest.skipUnless(OTA_SIM_AVAILABLE, 'ota_sim not installed (pip install -e ../ota_sim)')
class TncRelayTests(unittest.TestCase):
    """Two TNCs on one in-process FM simulator; KISS clients on each end."""

    async def _setup(self):
        sim = await OTASimulator(FMLink('mic', base_cnr_db=25, latency_s=0.01, jitter_s=0.0, seed=1),
                                 log=lambda msg: None).start('127.0.0.1', 0)
        quiet = lambda msg: None  # noqa: E731
        a = tnc.Tnc('K0MDT', mode_id=2, log=quiet)
        b = tnc.Tnc('W1AW', mode_id=2, log=quiet)
        await a.station.connect(*sim.address)
        await b.station.connect(*sim.address)
        await a.hub.serve_tcp('127.0.0.1', 0)
        await b.hub.serve_tcp('127.0.0.1', 0)
        return sim, a, b

    @staticmethod
    async def _read_frames(reader, decoder, want, timeout=8.0):
        frames = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(frames) < want and loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), deadline - loop.time())
            except asyncio.TimeoutError:
                break
            frames += [d for _, _, d in decoder.feed(chunk)]
        return frames

    def test_frames_cross_the_air_byte_for_byte(self):
        async def scenario():
            sim, a, b = await self._setup()
            ra, wa = await asyncio.open_connection(*a.hub.tcp_address)
            rb, wb = await asyncio.open_connection(*b.hub.tcp_address)
            await asyncio.sleep(0.05)
            out = kiss.ax25_ui_frame('K0MDT-7', 'W1AW', bytes(range(256)))  # every byte value
            wa.write(kiss.encode(out))
            got_b = await self._read_frames(rb, kiss.Decoder(), 1)
            back = kiss.ax25_ui_frame('W1AW', 'K0MDT-7', b'reply')
            wb.write(kiss.encode(back))
            got_a = await self._read_frames(ra, kiss.Decoder(), 1)
            for w in (wa, wb):
                w.close()
            a.close(), b.close(), sim.close()
            self.assertEqual(got_b, [out])
            self.assertEqual(got_a, [back])
            self.assertEqual(a.station.stats['sent_ax25_frames'], 1)
            self.assertEqual(b.station.stats['received_ax25_frames'], 1)
        asyncio.run(scenario())

    def test_pty_carries_kiss_too(self):
        async def scenario():
            sim, a, b = await self._setup()
            b.hub.open_pty()
            fd = os.open(b.hub.pty_path, os.O_RDWR | os.O_NOCTTY)
            os.set_blocking(fd, False)
            ra, wa = await asyncio.open_connection(*a.hub.tcp_address)
            os.write(fd, kiss.encode(kiss.ax25_ui_frame('W1AW', 'K0MDT', b'from the pty')))
            got = await self._read_frames(ra, kiss.Decoder(), 1)
            os.close(fd)
            wa.close()
            a.close(), b.close(), sim.close()
            self.assertEqual(got, [kiss.ax25_ui_frame('W1AW', 'K0MDT', b'from the pty')])
        asyncio.run(scenario())

    def test_replies_match_a_smack_host(self):
        async def scenario():
            sim, a, b = await self._setup()
            ra, wa = await asyncio.open_connection(*a.hub.tcp_address)
            rb, wb = await asyncio.open_connection(*b.hub.tcp_address)
            await asyncio.sleep(0.05)
            # Host B announces SMACK by sending a SMACK frame; A stays plain.
            wb.write(kiss.encode(kiss.ax25_ui_frame('K0MDT-2', 'K0MDT-1', b'smack host'), crc=kiss.CRC_SMACK))
            got_a = await self._read_frames(ra, kiss.Decoder(), 1)
            wa.write(kiss.encode(kiss.ax25_ui_frame('K0MDT-1', 'K0MDT-2', b'to the smack host')))
            raw = b''
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 8
            while raw.count(bytes([kiss.FEND])) < 2 and loop.time() < deadline:
                raw += await asyncio.wait_for(rb.read(4096), deadline - loop.time())
            for w in (wa, wb):
                w.close()
            a.close(), b.close(), sim.close()
            self.assertEqual(got_a, [kiss.ax25_ui_frame('K0MDT-2', 'K0MDT-1', b'smack host')])
            self.assertTrue(raw[1] & 0x80, "reply to a SMACK host must be SMACK-framed")
            decoder = kiss.Decoder()
            self.assertEqual(decoder.feed(raw), [(0, 0, kiss.ax25_ui_frame('K0MDT-1', 'K0MDT-2',
                                                                        b'to the smack host'))])
            self.assertEqual(decoder.crc_mode, kiss.CRC_SMACK)
        asyncio.run(scenario())

    def test_a_window_of_frames_goes_out_in_one_transmission(self):
        # The kernel hands a TNC a window of I-frames at once; with burst
        # batching they share one key-up and all arrive, in order.
        async def scenario():
            sim, a, b = await self._setup()
            sends = []
            real_send = a.station._sim.send
            a.station._sim.send = lambda payload: (sends.append(len(payload)), real_send(payload))
            ra, wa = await asyncio.open_connection(*a.hub.tcp_address)
            rb, wb = await asyncio.open_connection(*b.hub.tcp_address)
            await asyncio.sleep(0.05)
            window = [kiss.ax25_ui_frame('K0MDT-7', 'W1AW', bytes([i]) * 60) for i in range(4)]
            wa.write(b''.join(kiss.encode(f) for f in window))
            got = await self._read_frames(rb, kiss.Decoder(), 4, timeout=12)
            for w in (wa, wb):
                w.close()
            a.close(), b.close(), sim.close()
            self.assertEqual(got, window)
            self.assertEqual(len(sends), 1)
        asyncio.run(scenario())

    def test_ax25_frames_bypass_the_chat_link_layer(self):
        async def scenario():
            sim, a, b = await self._setup()
            delivered = []
            b.station._on_message = delivered.append
            a.station.send_ax25(kiss.ax25_ui_frame('K0MDT', 'W1AW', b'not chat'), 'K0MDT', 'W1AW')
            rb, wb = await asyncio.open_connection(*b.hub.tcp_address)
            await asyncio.sleep(2.0)
            wb.close()
            a.close(), b.close(), sim.close()
            self.assertEqual(delivered, [])
            self.assertEqual(b.station.receiver.counts['ok'], 0)
            self.assertEqual(b.station.stats['received_ax25_frames'], 1)
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
