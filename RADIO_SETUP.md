# OpenWave on real radios

How to run `ota_station.py` over two real FM radios, one per computer, with
no simulator and no connection between the computers except the radio link.
Verified 2026-10-04 with an HT on an AIOC adapter and an HT on a Digirig,
in both directions, with `--ack --auto`: mode negotiation, messages and
ACKs all worked.

> **This transmits.** Use a clear frequency and low power, and run it only
> where you are licensed to. Each station's callsign goes in every frame's
> source field and in the messages you type; OpenWave frames are not
> encrypted. Station identification and band-plan rules are your
> responsibility.

## What you need

Per station: an HT (or other FM radio), an audio/PTT interface, and a
computer running Linux with this repo, `numpy`, `scipy` and `arecord`/`aplay`
(ALSA). No `ota_sim`, no network.

| Interface | Audio | PTT | Notes |
|---|---|---|---|
| **AIOC** (All-In-One-Cable, USB `1209:7388`) | USB sound card `AllInOneCable` | serial **DTR** | PTT port is `/dev/ttyACM*` |
| **Digirig** (CM108 sound card + CP2102N serial) | USB sound card `USB PnP Sound Device` | serial **RTS** | stereo-only playback; its AGC must be turned off |

`./radio_stations.py` detects both automatically by their USB IDs, so
device numbers (`hw:1,0`, `ttyACM0`) can change without breaking anything.

What does **not** work:

- A laptop's 3.5 mm headphone jack. The one tried (MacBookAir5,2 / CS4206)
  is output-only for plain audio; an HT's cable into it records silence.
  Use a USB interface.
- Bluetooth KISS TNCs (Mobilinkd etc.). They modulate and demodulate
  themselves and only hand over packets, never the radio's audio, which the
  OpenWave audio PHY needs.

## One-time setup, per computer

```
cd ~/Projects/open_wave && git pull
pip install numpy scipy pyserial     # if not already installed
```

**PTT serial permission.** The PTT port is `root:dialout`. Permanent fix,
then log out and back in:

```
sudo usermod -aG dialout $USER
```

Or, for the current plug-in only (resets when the adapter is replugged):

```
sudo chmod a+rw /dev/ttyACM0        # AIOC; the Digirig is /dev/ttyUSB0
```

Check the interface is found:

```
./radio_stations.py
# aioc     audio hw:CARD=AllInOneCable,DEV=0   PTT DTR usb-AIOC_All-In-One-Cable_...
# digirig  audio hw:CARD=Device,DEV=0          PTT RTS usb-Silicon_Labs_CP2102N_...
```

## Audio setup and leveling

Plugging the cable into an HT's speaker jack mutes its speaker, so you cannot
set the volume by ear. Use the level meter. Do this on **each** station
before the first run, and again if you change the volume or replug anything.

### 1. Receive side (the important one)

1. Put the radio on the frequency. For setting levels, **open the squelch**
   (monitor button, or squelch 0) so the speaker output is steady hiss.
2. **Digirig only: turn the sound chip's automatic gain control off and set a
   low capture gain.** With AGC on it inflates quiet hiss to full scale and
   pumps during a burst, and neither the radio volume nor the capture gain
   seems to do anything:

   ```
   amixer -c Device sset "Auto Gain Control" off
   amixer -c Device sset Mic cap 2
   ```

   (`-c Device` is the Digirig's ALSA card name; `amixer -c 1 ...` also works
   if it is card 1.) These settings can reset when the Digirig is unplugged;
   run them again if the meter reads wildly high.
3. **AIOC:** leave its capture gain at the default 100 % (0 dB); it is
   adjusted with the radio's volume.
4. Run the meter and turn the **radio's volume knob** until it says `good`:

   ```
   ./level_meter.py            # the interface on this computer
   ```

   ```
   [███████████████████████████████·   |    ] rms  -12.3  peak   -2.7 dBFS  good
   ```

   The `█` bar is the RMS level, `|` the peak, and `·` marks the target zone
   for RMS. **Targets for open-squelch hiss: RMS about -26 to -10 dBFS, peaks
   below about -2 dBFS.** The text on the right says what to do (`low - turn
   up`, `high - turn down`, `CLIPPING`). Full scale (clipping) ruins
   decoding, and a very low level wastes resolution.

   What readings meant in practice: AIOC at 0 dB capture sat near -38 dBFS
   with the radio volume low (too quiet) and was fixed with the volume
   knob. The Digirig with AGC on read RMS 0.97 (clipped at any setting); with
   AGC off and gain 2 it read about -12 dBFS (good).

### 2. Transmit side

- The station normalises each burst's peak to `--tx-level` (default **0.5**)
  before playing it into the radio. That worked without further adjustment
  on both interfaces here. The interface's own playback volume matters too:
  the AIOC was left at 100 %, the Digirig's `Speaker` playback at 40 %.
- **Check TX level from the other station:** run `./level_meter.py` on the
  receiving station while the other one sends. A burst should show clearly
  above the idle hiss without pinning at `CLIPPING`. If the received burst
  sounds distorted or clips, lower `--tx-level` (try 0.3); if it is
  weak, raise it (try 0.7). An FM radio's mic path limits and
  pre-emphasises, so too much level shows up as distortion, not volume.
- **Squelch:** everything so far was tested with the receiving radio's
  squelch open. With squelch closed, the first part of a burst can be lost
  while the squelch opens; raise `--lead-in-ms` (default 250) to cover your
  radio's squelch-open time.

### 3. Both radios

Same frequency, clear channel, low power, FM, the `mic` audio port (the
interfaces above connect to the radio's mic/speaker lines; it limits modes to
1200 symbols/s, i.e. modes 0-2).

## Running the live stations

One command on each computer; the two stations talk only over the radio link.
Callsigns here are examples: use your own, and make each `--dst` the other
station's `--station-id`.

**Station 1** (for example the AIOC laptop):

```
cd ~/Projects/open_wave
./radio_stations.py                       # confirm the interface is found
./level_meter.py                          # set the radio volume (Ctrl-C when "good")
./ota_station.py --station-id K0MDT-1 --dst K0MDT-2 \
    --phy audio --audio-port mic --radio --ack --auto
```

**Station 2** (for example the Digirig laptop):

```
cd ~/Projects/open_wave
./radio_stations.py
./level_meter.py
./ota_station.py --station-id K0MDT-2 --dst K0MDT-1 \
    --phy audio --audio-port mic --radio --ack --auto
```

Type a line and press Enter to send it; messages from the other station
print as they arrive. `/help` lists the commands (`/mode`, `/fec`, `/ping`,
`/ping survey`, `/renegotiate`, ...). Ctrl-C exits and prints a session
summary.

- **Every run writes a log, with no switch needed**: a new timestamped file
  `logs/<station-id>-<date>-<time>.log` in the repo (gitignored), whose path is
  printed at startup. It holds what you see on screen plus, for the radio,
  every burst heard (mode, SNR, sync quality, peak level), every
  transmission (keyed and released) and every line you type. The terminal
  output is not saved anywhere else. Send the log (not a screenshot) when
  asking for help debugging a link. `--log-file PATH` picks another path;
  `--no-log` turns it off.
- `--radio` alone uses the only interface attached to that computer; with two
  attached, name one: `--radio aioc` or `--radio digirig`.
- `--ack` retries each fragment until it is acknowledged. `--auto`
  negotiates one mode/FEC for both directions on the first message you send
  (a short exchange of control bursts first; on the real radios the
  negotiation took about 12 seconds and the first message was acknowledged
  after about 16, with later messages taking about 3 - measured with frame
  format v1. v2's bursts are 17-31% shorter, so expect less now. Both
  stations must run v2 code to hear each other.) Both are optional: for the simplest possible test use
  `--mode 0 --fec --ack` without `--auto`.
- `--tx-level 0.5` is the default; `--ptt-line dtr|rts` overrides the
  interface's PTT line; `-v` prints per-message detail.
- If a mode needs a faster symbol rate than the `mic` port allows, the station
  refuses it. The `data` port profile (up to 2400 baud) needs a radio data
  connector and has not been tried.

## Simpler tests and tools

These are for checking one thing at a time. None uses ssh unless noted.

| Command | What it does |
|---|---|
| `./radio_stations.py` | list detected interfaces |
| `./level_meter.py [aioc\|digirig]` | live input level bar (see above) |
| `./radio_send.py --src K0MDT -m 0 1 2 --count 3` | transmit test bursts from this station (asks for `yes`, waits 3 s, then sends) |
| `./radio_listen.py` | decode bursts arriving at this station, one line each with mode, SNR and message; Ctrl-C prints a summary; `--wav FILE` replays a recording |
| `./air_loopback_test.py` | two full stations over an in-memory simulated air; no hardware, nothing transmitted (about a minute) |
| `python3 audio_wav_test.py gen\|decode` | make a burst WAV / decode a recording, offline |
| `./radio_link_test.py --src K0MDT --tx aioc --rx digirig ...` | one machine drives both stations over **ssh** and prints pass/fail and SNR per burst. Needs `[scan] hosts = [...]` in `stations.toml` and passwordless ssh; skip it if you control each station separately |

Typical one-way check: run `./radio_listen.py` on one computer, then
`./radio_send.py --src K0MDT -m 0 1 2 --count 3` on the other.

## Results so far

One-way bursts (AIOC HT transmitting, Digirig HT receiving; open squelch,
low power), data-aided SNR from the preamble:

| Mode | Modulation | SNR |
|---|---|---|
| 0 | BPSK 600 baud | 21.3 dB |
| 1 | BPSK 1200 baud | 17.2 dB |
| 2 | QPSK 1200 baud | 15.6 / 15.8 dB (two runs) |

All decoded at sync quality 0.99-1.00. Both stations running `ota_station.py
--radio --ack --auto` exchanged messages in both directions.

A session log (the lenovo/AIOC station sending its first message, 2026-10-04):
the negotiation probe and reply (BPSK 600) measured 17.8 and 20.0 dB and chose
Mode 2 (QPSK 1200, `r1_2_k3`); the first message was acknowledged 16 s after
it was typed, and a second one 3 s after. The QPSK bursts heard after
negotiating measured 14.8 and 14.7 dB (about 3 dB below the BPSK 600 probe,
as expected from the doubled symbol rate), all with peaks of 0.45-0.57.

### How much SNR each mode needs (software noise test)

`./audio_margin_sweep.py` adds white noise to real audio bursts and finds where
each mode stops decoding (30-character message, mic port; nothing is
transmitted):

| Mode | FEC | Stops delivering reliably near |
|---|---|---|
| 0 (BPSK 600) | `r1_4_k9` | -1 dB |
| 1 (BPSK 1200) | `r1_2_k3` | 4.5 dB |
| 2 (QPSK 1200) | `r1_2_k3` | 7 dB |

**Flagged 2026-10-06: modes 1/2 didn't reproduce.** Running
`./audio_margin_sweep.py -m 1 2 --fec r1_2_k3` fresh on this machine, with
no arguments changed from the defaults used above, reproduces mode 0's
row closely (-1 to -2 dB) but gives mode 1's 50%-delivered point around
~1.75 dB and mode 2's around ~5.0 dB - 2.5-3 dB more pessimistic than this
table, consistently, in a 24-trial run and confirmed again at 100 trials
with finer noise steps (so not just run-to-run scatter averaging out).
Not yet root-caused - a numpy version difference affecting
`default_rng(7)`'s output and environment drift since this was measured
are both candidates, but unconfirmed. Worth a re-check before relying on
this table for new link-margin work; see `tasklist.txt` for the
cross-check that ruled out a modem/FEC code change as the cause.

These SNRs are the station's own data-aided readings. A faster mode sees more
of the channel's noise at the same signal: at identical noise, mode 1 reads
about 3.1 dB below mode 0 and mode 2 about 2.5 dB below. `--auto` measures SNR
on a 600 baud probe, so on the audio path it now subtracts that penalty
(`10*log10(rate/600)`) before comparing with the mode table. Before this, a
weak link could be put on a mode right at, or past, its cliff. The logged
real-radio link (probe 17.8 dB, so about 14.8 dB at mode 2) has about 8 dB of
margin over mode 2's cliff. This is plain added noise; real FM noise has a
threshold effect and fades, so keep some extra margin.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `Permission denied` running a script | `chmod +x`, or run it with `python3 script.py` |
| `permission denied` opening the PTT port | add yourself to `dialout`, or `sudo chmod a+rw /dev/ttyACM0` (see setup) |
| `aplay: Channels count non available` | stereo-only sound card (the Digirig). Fixed by playing through `plughw:`; `git pull` if you still see it |
| meter reads near silence (below about -50 dBFS) | squelch closed, radio volume too low, or the cable is in a jack with no audio input (see "What does not work") |
| meter pinned at `CLIPPING` whatever the volume | Digirig AGC is on: `amixer -c Device sset "Auto Gain Control" off` |
| `radio_stations.py` shows nothing | check `lsusb` for the adapter and `arecord -l` for its sound card |
| stations hear nothing from each other | wrong PTT line (try `--ptt-line dtr` or `rts`; the radio's TX light shows it keying), TX level too low or too high, different frequencies, or squelch closed |
| the sender keeps retrying and the peer reports "duplicate" | the peer's ACK is not getting back: check the levels and PTT in that direction; the transport already waits a 0.6 s turnaround before replying |
| high CPU on a small computer | the receiver re-decodes its audio buffer every 0.5 s (about half a core with 2-3 candidate modes) |
| `[radio tx error]` | the line after it names the `aplay` or serial error |
| something odd on the link | read the `logs/` file on both stations and compare the timestamps: who keyed when, what each side heard and at what SNR |

## How it works (short)

`openwave_radio.py` gives `ota_station.py` a radio in place of the simulator,
with the same send / receive-callback interface, so the protocol code is
unchanged:

- **Receive:** `arecord` feeds a continuous capture into a burst detector. It
  looks for the station's current candidate bursts (the negotiated data
  mode, then the fixed control mode) in a rolling buffer by correlating the
  preamble and sync word, decodes to find where each burst ends, and hands
  that burst's audio to the station's normal receive path.
- **Transmit:** `send()` queues the burst. A worker scales it to `--tx-level`,
  waits a turnaround after the last received burst, keys PTT (`ptt_play.py`),
  plays it through `aplay`, and releases PTT (always, even on error).
- **Half duplex:** receiving is paused while keyed and for a short settle
  time afterwards. The station's reply timeouts allow 2 seconds per
  direction for key-up, detection and decode.

## Winlink peer-to-peer (Pat) over OpenWave

`openwave_tnc.py` runs OpenWave as a KISS TNC on the radio, Linux's AX.25
stack attaches to it, and Pat (a Winlink client) sends Winlink messages
station to station over it. **Rehearsed end to end over the simulator on
2026-10-05** (Pat K0MDT-1 -> Pat K0MDT-2 through two TNCs and ota_sim's FM
model, message delivered in 18.6 s); **not yet tried on the air.** Design
and status: [KISS_TNC_SCOPE.md](KISS_TNC_SCOPE.md).

Stations used here: **K0MDT-1** and **K0MDT-2**, one per computer. Both TNCs
must use the same `--mode` and frame format (current `main` on both).

### One-time setup on a laptop (Debian/Ubuntu)

Everything from "One-time setup, per computer" above (repo, numpy/scipy,
PTT `dialout` permission, `./radio_stations.py`, audio leveling), then:

```bash
# AX.25 tools and kernel modules (also load them at every boot)
sudo apt install ax25-tools ax25-apps
sudo modprobe ax25 && sudo modprobe mkiss
printf 'ax25\nmkiss\n' | sudo tee /etc/modules-load.d/ax25.conf

# The AX.25 port: name "ow", this station's callsign (K0MDT-2 on the other one)
printf 'ow\tK0MDT-1\t9600\t255\t2\tOpenWave TNC\n' | sudo tee -a /etc/ax25/axports

# Pat (check `dpkg --print-architecture`: amd64 on Intel/AMD laptops)
wget https://github.com/la5nta/pat/releases/download/v1.0.0/pat_1.0.0_linux_amd64.deb
sudo apt install ./pat_1.0.0_linux_amd64.deb
```

Then `pat configure` and set (leave the rest; K0MDT-2 on the other station):

```json
"mycall": "K0MDT-1",
"ax25": { "engine": "linux", "beacon": { "every": 0, "message": "", "destination": "" } },
"ax25_linux": { "port": "ow" }
```

`"every": 0` stops Pat beaconing on its own. No Winlink password is needed
for peer-to-peer.

### One-time setup on a Raspberry Pi

Use **Raspberry Pi OS (Bookworm) 64-bit**: it has Python 3.11 (the radio code
needs 3.11+ for `tomllib`) and runs Pat's `arm64` build. A Pi 4 or 5 is
recommended. **The Pi is untested so far:** the receiver re-decodes its audio
buffer every 0.5 s and uses about half a core on a laptop, so check `top`
while it runs (a Pi Zero is unlikely to keep up).

```bash
sudo apt update
sudo apt install git python3-numpy python3-scipy python3-serial alsa-utils \
                 ax25-tools ax25-apps
git clone https://github.com/md-thomas/open_wave.git ~/Projects/open_wave
cd ~/Projects/open_wave
sudo usermod -aG dialout $USER          # PTT serial port; log out and back in
sudo modprobe ax25 && sudo modprobe mkiss
printf 'ax25\nmkiss\n' | sudo tee /etc/modules-load.d/ax25.conf
printf 'ow\tK0MDT-2\t9600\t255\t2\tOpenWave TNC\n' | sudo tee -a /etc/ax25/axports
wget https://github.com/la5nta/pat/releases/download/v1.0.0/pat_1.0.0_linux_arm64.deb
sudo apt install ./pat_1.0.0_linux_arm64.deb    # 32-bit Pi OS: the _linux_armhf.deb
./radio_stations.py                     # is the AIOC / Digirig found?
./level_meter.py                        # level the receive audio (see above)
```

Then `pat configure` exactly as for a laptop. With a headless Pi, run each of
the session steps below in its own `ssh` session (or `tmux`).

### Each session

On **both** stations:

```bash
# 1. The TNC on the radio (leave running; add --tx-level if you use one with ota_station)
./openwave_tnc.py --callsign K0MDT-1 --radio --mode 2 --kiss-port 0 --pty-link /tmp/kiss-ow
# 2. Attach it to the kernel (any KISS framing mode works, including the default)
sudo kissattach /tmp/kiss-ow ow
```

Optional raw AX.25 check before Pat (good for a first on-air run). On
K0MDT-2:

```bash
printf '[K0MDT-2 VIA ow]\nNOCALL   * * * * * *  L\ndefault  * * * * * *  -  root  /bin/echo  echo Connected to K0MDT-2 over the air\n' | sudo tee /etc/ax25/ax25d.conf
sudo ax25d
```

and on K0MDT-1 `sudo axcall ow K0MDT-2`: expect "Connected to K0MDT-2 over the
air", then a disconnect.

The Winlink message. On **K0MDT-2** (the one receiving):

```bash
pat --listen ax25 interactive        # or: pat --listen ax25 http   (web UI on :8080)
```

On **K0MDT-1**:

```bash
pat compose                          # To: K0MDT-2 (or: echo "text" | pat compose -s "Subject" --p2p-only K0MDT-2)
pat connect ax25:///K0MDT-2
```

Then `pat read` on K0MDT-2. Pat ran without root in the rehearsal; only
`kissattach` and `ax25d` need `sudo`.

**Pat's account prompt.** Before connecting, Pat asks Winlink's online lookup
whether an account named exactly `K0MDT-1` exists. Winlink accounts don't
carry SSIDs, so it warns "unable to confirm that your Winlink account is
active" and asks `c(ontinue), a(bort)`. For a peer-to-peer connection answer
**c**: no Winlink gateway is involved, so no account is created. Pat
remembers the answer for 100 days (`~/.local/state/pat/.account-confirmed_K0MDT-1.json`).

### Troubleshooting

| Symptom | Fix |
|---|---|
| `axcall`/Pat connects, then nothing comes back | A TNC was restarted: it gets a new pty, so stop the old `kissattach` (`sudo pkill kissattach`), attach again, and restart `ax25d` / the Pat listener so they bind the new port |
| Frames are retried before the reply could arrive | Raise the kernel's AX.25 retry timer on both stations: `echo 20000 \| sudo tee /proc/sys/net/ax25/ax0/t1_timeout` (mode 2 was fine at the default 10 s over the simulator; slower modes need more) |
| One TNC logs `tx` but the other never `rx` | The usual radio checks (levels, PTT, squelch, frequency) from the sections above; both TNCs on the same `--mode`; both computers on the same `main` |
| TNC logs "ignoring a frame for TNC port 8/2" | An old TNC version without SMACK/FlexNet support: `git pull` |
