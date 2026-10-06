# Scope: OpenWave as a KISS TNC (AX.25 / Winlink)

Status: **Milestone 1 in progress (2026-10-05)** - the TNC works between
two instances over the simulator (KISS over TCP and a pty, frames
byte-identical both ways); the Linux kernel AX.25 step is next and needs
root on a test machine (see "Milestone 1: kernel AX.25 test").

## Goal

Let standard packet software run over OpenWave's modem: peer-to-peer
Winlink with Pat, digipeating, and a Winlink gateway (Linux RMS Gateway).
All three already work on top of the Linux kernel AX.25 stack, which
talks to its modem ("TNC") over KISS. So OpenWave implements the TNC side
of KISS and everything above it comes for free.

## What KISS is

The protocol between packet software and a TNC: the host builds whole
AX.25 frames (callsigns, connections, retries); the TNC puts them on the
air and hands received ones back. Each frame is wrapped as

```
C0 | 00 | <AX.25 frame: addresses, control, PID, info - no flags, no FCS> | C0
```

with C0 and DB inside the frame escaped as DB DC / DB DD. Command bytes
other than 00 set TNC parameters (TXDELAY, persistence, slot time,
TXtail). Carried over a serial port, a pseudo-terminal, or TCP.

## The stack

```
 Pat (Winlink client)          Linux RMS Gateway (-> Winlink servers)
          \                         /
     Linux kernel AX.25 stack  - callsigns, connected mode, retries, digipeating
                 |
           kissattach          - binds the kernel to a KISS device
                 |   KISS over a pty (or TCP for other software)
        openwave_tnc.py        - each AX.25 frame <-> one OpenWave burst
                 |   audio + PTT (openwave_radio) or ota_sim
             FM radio
```

Both station laptops run Linux, so the kernel stack is available on both.
Pat's main AX.25 route on Linux is exactly this (its `linux` engine), and
Linux RMS Gateway requires the kernel AX.25 stack.

## Design

- **Packet type 0x05 = AX.25.** The OpenWave payload is the AX.25 frame as
  KISS carries it; the OpenWave CRC32 replaces the AX.25 FCS. The OpenWave
  header's source/destination are filled from the AX.25 addresses
  (informational - AX.25 SSIDs fit the packed callsign format).
- **No OpenWave link layer.** No OpenWave ACKs, fragmentation or
  negotiation for AX.25 frames: AX.25 does connections, retries and
  ordering. The TNC passes every received frame to the host, like any TNC
  (the host's stack filters by address).
- **One AX.25 frame per burst** (Milestone 1). Fixed mode and FEC per TNC,
  set on the command line; both ends must match.
- **KISS parameter commands are logged, not applied.** The kernel's
  TXDELAY etc. assume an AFSK TNC; the lead-in (`--lead-in-ms`) covers
  key-up and squelch here.
- **Two KISS interfaces** (decided): TCP (default port 8001) and a
  pseudo-terminal (`--pty-link PATH`) for `kissattach`.
- **Reuses the station's modem** (`ota_station.Station`): the same PHY,
  FEC, interleaving, audio framing, airtime handling, and both transports
  (simulator and real radio via `connect_from_args`).

## Milestones

1. **Two TNCs over the simulator, kernel AX.25 on top.** Exchange UI frames
   and a connected session (e.g. `axcall` to `ax25d`). No radio.
2. **Pat peer-to-peer over the real radios.** A Winlink message from one
   laptop to the other.
3. **Digipeating.** A third station relays (standard AX.25 digipeating).
4. **Gateway.** Linux RMS Gateway on an OpenWave TNC; needs a Winlink
   gateway registration (Winlink expects gateways to run 24/7).

Later improvements, measured against real use:
- **Channel access:** carrier sense + p-persistence/slot-time backoff,
  needed once more than two stations share a frequency.
- **Burst batching:** AX.25 can send up to 7 frames in a row
  (MAXFRAME); packing them into one burst saves a lead-in, preamble and
  header each.
- **Automatic mode selection** for AX.25 traffic.

## Milestone 1 status

Done:
- `openwave_kiss.py`: KISS encoder/incremental decoder (any chunking,
  escapes, commands, ports) and AX.25 address helpers.
- `openwave_tnc.py`: the TNC - KISS over TCP and a pty, on a Station
  connected to the simulator or a real radio.
- `PACKET_TYPE_AX25`, `Station.send_ax25()` and an `on_ax25` hook;
  `ota_station.connect_from_args()` shared with the TNC.
- Tests (`tests/test_openwave_kiss.py`): framing, address parsing, two
  TNCs over the FM simulator (all 256 byte values, both directions, TCP
  and pty), and AX.25 frames bypassing the chat link layer.
- Manual run: two TNC processes over `ota_sim --channel fm` at mode 2,
  frames delivered byte-identical in ~0.9 s each way.

Next: the kernel AX.25 test below.

### Milestone 1: kernel AX.25 test (needs root)

One-time setup:

```bash
sudo apt install ax25-tools ax25-apps
sudo modprobe ax25 && sudo modprobe mkiss
```

`/etc/ax25/axports` (one line per port: name, callsign, speed, paclen,
window, description; each port needs its own callsign):

```
owa  K0MDT-1  9600  128  2  OpenWave TNC A
owb  K0MDT-2  9600  128  2  OpenWave TNC B
```

Run (one machine, both TNCs through the simulator):

```bash
python3 -m ota_sim.simulator --channel fm --port mic --base-snr-db 15
./openwave_tnc.py --callsign K0MDT-1 --sim-host 127.0.0.1 --mode 2 --kiss-port 0 --pty-link /tmp/kiss-a
./openwave_tnc.py --callsign K0MDT-2 --sim-host 127.0.0.1 --mode 2 --kiss-port 0 --pty-link /tmp/kiss-b
sudo kissattach /tmp/kiss-a owa
sudo kissattach /tmp/kiss-b owb
```

Then:
- **UI frames:** `sudo axlisten -a` in one terminal, `sudo beacon -s -d K0MDT-2 owa "hello"`
  in another; the TNC windows log `tx` / `rx`, and axlisten shows it.
- **Connected session:** run `ax25d` with an entry for K0MDT-2 on `owb`
  (e.g. one that runs `/bin/date` or a shell), then `axcall owa K0MDT-2`.

AX.25 timers: the kernel's defaults assume short 1200-baud frames. Check
`/proc/sys/net/ax25/owa/` (`t1_timeout` is the retry timer, in ms); if
frames are being retried before the reply could have arrived (several
seconds per burst at mode 0), raise it.

## Limits

- **Overhead:** AX.25 adds ~16-18 bytes of addresses/control per frame and
  waits for an ACK after each window of frames; still far more robust
  underneath than 1200-baud AFSK.
- **Every station in the chain needs an OpenWave TNC,** including
  digipeaters and gateways; standard AFSK packet stations can't hear it.
- **Station ID comes free:** every AX.25 frame carries the callsign.
