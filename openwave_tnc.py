#!/usr/bin/env python3
"""
OpenWave as a KISS TNC: packet software (the Linux kernel's AX.25 stack via
kissattach, and through it Pat / Linux RMS Gateway / axcall) sends AX.25
frames over KISS, and each one goes out as an OpenWave burst - FEC,
interleaving and the audio PHY - instead of 1200-baud AFSK. Received
bursts carrying AX.25 frames are handed back over KISS.

The TNC is just a modem: AX.25 does the addressing, connections, ACKs,
retries and ordering, so OpenWave's own chat link layer (ACKs, fragmentation,
negotiation) isn't used. Both TNCs must use the same mode and FEC preset.

  # simulator (start `python3 -m ota_sim.simulator --channel fm --port mic` first)
  ./openwave_tnc.py --callsign K0MDT --sim-host 127.0.0.1 --pty-link /tmp/kiss-k0mdt
  # real radio
  ./openwave_tnc.py --callsign K0MDT --radio --pty-link /tmp/kiss-k0mdt

Then attach the kernel AX.25 stack (see KISS_TNC_SCOPE.md):
  sudo kissattach /tmp/kiss-k0mdt ow0

KISS is also served over TCP (--kiss-port, default 8001) for software that
speaks KISS-over-TCP. See KISS_TNC_SCOPE.md for the design and test plan.
"""
import argparse
import asyncio
import os
import sys
import tty

import openwave_audio as audio_phy
import openwave_fec as fec
import openwave_kiss as kiss
import openwave_link as link
import ota_station


class KissHub:
    """Every KISS client (TCP connections, the pty) in one place: frames
    from any client go to on_frame; frames from the air go to all of them."""

    def __init__(self, on_frame, log):
        self._on_frame = on_frame
        self._log = log
        self._writers = []       # TCP StreamWriters
        self._pty_master = None  # fd
        self._pty_slave = None   # kept open so the master never sees EOF/EIO
        self._server = None
        self.pty_path = None
        self.tcp_address = None

    async def serve_tcp(self, host, port):
        self._server = await asyncio.start_server(self._handle_tcp, host, port)
        self.tcp_address = self._server.sockets[0].getsockname()[:2]

    async def _handle_tcp(self, reader, writer):
        self._writers.append(writer)
        decoder = kiss.Decoder()
        self._log(f"[kiss] TCP client connected from {writer.get_extra_info('peername')}")
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                self._dispatch(decoder.feed(chunk))
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()
            self._log("[kiss] TCP client disconnected")

    def open_pty(self, link_path=None):
        master, slave = os.openpty()
        tty.setraw(slave)  # no echo or line editing: KISS is binary
        os.set_blocking(master, False)
        self._pty_master, self._pty_slave = master, slave
        self.pty_path = os.ttyname(slave)
        if link_path:
            if os.path.islink(link_path):
                os.unlink(link_path)
            os.symlink(self.pty_path, link_path)
        decoder = kiss.Decoder()

        def readable():
            try:
                chunk = os.read(master, 4096)
            except (BlockingIOError, OSError):
                return
            self._dispatch(decoder.feed(chunk))
        asyncio.get_running_loop().add_reader(master, readable)

    def _dispatch(self, frames):
        for port, command, data in frames:
            if command == kiss.CMD_DATA and port == 0:
                self._on_frame(data)
            elif command == kiss.CMD_DATA:
                self._log(f"[kiss] ignoring a frame for TNC port {port} (only port 0 exists)")
            else:
                # Channel-access and timing parameters (TXDELAY etc.) are
                # tuned for AFSK TNCs; this TNC's lead-in and timing come from
                # its own settings, so they're noted, not applied.
                name = kiss.COMMAND_NAMES.get(command, f'0x{command:X}')
                self._log(f"[kiss] host set {name}={data.hex() or '-'} (not applied)")

    def broadcast(self, frame):
        encoded = kiss.encode(frame)
        for writer in list(self._writers):
            writer.write(encoded)
        if self._pty_master is not None:
            try:
                os.write(self._pty_master, encoded)
            except OSError:
                pass

    def close(self):
        if self._server is not None:
            self._server.close()
        for writer in self._writers:
            writer.close()
        if self._pty_master is not None:
            asyncio.get_running_loop().remove_reader(self._pty_master)
            os.close(self._pty_master)
            os.close(self._pty_slave)


class Tnc:
    """A Station used purely as a modem for AX.25 frames, plus its KISS hub."""

    def __init__(self, callsign, mode_id=0, fec_preset=None, phy='audio', audio_port='mic',
                 lead_in_s=audio_phy.DEFAULT_LEAD_IN_S, verbose=False, log=None):
        self._log = log or (lambda msg: print(msg, file=sys.stderr))
        self.verbose = verbose
        self.station = ota_station.Station(
            station_id=callsign, dst_id='ALL', mode_id=mode_id, use_fec=True,
            fec_preset=fec_preset or link.WAVEFORM_MODES[mode_id]['fec_preset'],
            use_ack=False, auto=False, phy=phy, audio_port=audio_port, lead_in_s=lead_in_s,
            on_ax25=self._from_air, on_log=self._station_log)
        self.hub = KissHub(self._from_host, self._log)

    def _station_log(self, msg):
        if self.verbose:
            self._log(msg)

    def _from_host(self, frame):
        src, dst = kiss.ax25_endpoints(frame)
        try:
            airtime = self.station.send_ax25(frame, src, dst)
        except ValueError as exc:
            self._log(f"[tnc] not sent: {exc}")
            return
        self._log(f"[tnc] tx {src or '?'}>{dst or '?'} {len(frame)} bytes ({airtime:.2f}s on air)")

    def _from_air(self, frame):
        src, dst = kiss.ax25_endpoints(frame)
        snr = self.station._last_rx_snr_db
        self._log(f"[tnc] rx {src or '?'}>{dst or '?'} {len(frame)} bytes"
                  + (f" (SNR ~{snr:.1f}dB)" if snr is not None else ""))
        self.hub.broadcast(frame)

    def close(self):
        self.hub.close()
        self.station.close()


async def run(args):
    fec_preset = args.fec_preset or link.WAVEFORM_MODES[args.mode]['fec_preset']
    tnc = Tnc(args.callsign, args.mode, fec_preset, args.phy, args.audio_port,
              args.lead_in_ms / 1000, args.verbose)
    link_note = await ota_station.connect_from_args(tnc.station, args)
    if args.kiss_port:
        await tnc.hub.serve_tcp(args.kiss_host, args.kiss_port)
    if args.pty or args.pty_link:
        tnc.hub.open_pty(args.pty_link)
    mode = link.WAVEFORM_MODES[args.mode]
    print(f"[tnc] {args.callsign}: mode {args.mode} ({mode['modulation']} {mode['symbol_rate']} sym/s) "
          f"FEC {fec_preset}, phy {args.phy}" + (f" ({args.audio_port} port)" if args.phy == 'audio' else '')
          + f", {link_note}", file=sys.stderr)
    if tnc.hub.tcp_address:
        print(f"[tnc] KISS over TCP on {tnc.hub.tcp_address[0]}:{tnc.hub.tcp_address[1]}", file=sys.stderr)
    if tnc.hub.pty_path:
        where = f"{args.pty_link} -> {tnc.hub.pty_path}" if args.pty_link else tnc.hub.pty_path
        print(f"[tnc] KISS on pty {where}  (e.g. sudo kissattach {args.pty_link or tnc.hub.pty_path} ow0)",
              file=sys.stderr)
    print("[tnc] running - Ctrl+C to stop", file=sys.stderr)
    try:
        await asyncio.Event().wait()
    finally:
        tnc.close()
        if args.pty_link and os.path.islink(args.pty_link):
            os.unlink(args.pty_link)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--callsign', required=True,
                        help="this station's callsign (identifies it to the simulator; frames carry "
                             "the AX.25 host's own addresses)")
    parser.add_argument('--sim-host', help='ota_sim simulator address')
    parser.add_argument('--sim-port', type=int, default=9500, help='ota_sim simulator port (default: 9500)')
    parser.add_argument('--radio', nargs='?', const='', default=None, metavar='INTERFACE',
                        help='use a real radio (aioc, digirig, ... - see radio_stations.py) instead '
                             'of the simulator')
    parser.add_argument('--ptt-line', choices=('dtr', 'rts'), help="override the interface's PTT line")
    parser.add_argument('--tx-level', type=float, default=0.5, help='transmit audio level (default: 0.5)')
    parser.add_argument('--phy', choices=('audio', 'iq'), default='audio',
                        help='audio (default; a radio or --channel fm) or iq (--channel iq)')
    parser.add_argument('--audio-port', choices=sorted(audio_phy.PORT_PROFILES), default='mic',
                        help='radio connection for --phy audio (default: mic)')
    parser.add_argument('--mode', type=int, default=0, choices=sorted(link.WAVEFORM_MODES),
                        help='Waveform Mode; both TNCs must match (default: 0)')
    parser.add_argument('--fec-preset', choices=sorted(fec.PRESETS),
                        help="FEC preset; both TNCs must match (default: the mode's own)")
    parser.add_argument('--lead-in-ms', type=float, default=audio_phy.DEFAULT_LEAD_IN_S * 1000,
                        help='silence before each burst for key-up and squelch (default: %(default)g)')
    parser.add_argument('--kiss-host', default='127.0.0.1', help='KISS TCP listen address (default: 127.0.0.1)')
    parser.add_argument('--kiss-port', type=int, default=8001, help='KISS TCP port, 0 = none (default: 8001)')
    parser.add_argument('--pty', action='store_true', help='also serve KISS on a pseudo-terminal')
    parser.add_argument('--pty-link', metavar='PATH',
                        help='serve KISS on a pseudo-terminal and symlink it here (implies --pty)')
    parser.add_argument('--verbose', '-v', action='store_true', help='show modem-level detail')
    args = parser.parse_args()
    try:
        link.pack_callsign(args.callsign)
    except ValueError as exc:
        parser.error(f"--callsign: {exc}")
    if args.radio is None and not args.sim_host:
        parser.error("give --sim-host (simulator) or --radio (a real radio)")
    if args.radio is not None and args.phy != 'audio':
        parser.error("--radio needs --phy audio")
    if args.phy == 'audio':
        limit = audio_phy.PORT_PROFILES[args.audio_port]['max_symbol_rate']
        if link.WAVEFORM_MODES[args.mode]['symbol_rate'] > limit:
            parser.error(f"--mode {args.mode} needs more than the {args.audio_port} port's {limit} sym/s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
