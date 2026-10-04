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
    --phy audio --audio-port mic --radio --ack --auto --log-file ~/station1.log
```

**Station 2** (for example the Digirig laptop):

```
cd ~/Projects/open_wave
./radio_stations.py
./level_meter.py
./ota_station.py --station-id K0MDT-2 --dst K0MDT-1 \
    --phy audio --audio-port mic --radio --ack --auto --log-file ~/station2.log
```

Type a line and press Enter to send it; messages from the other station
print as they arrive. `/help` lists the commands (`/mode`, `/fec`, `/ping`,
`/ping survey`, `/renegotiate`, ...). Ctrl-C exits and prints a session
summary.

- `--log-file PATH` appends everything the station logs to a timestamped
  file: what you see on screen plus, for the radio, every burst heard (mode,
  SNR, sync quality, peak level), every transmission (keyed and released) and
  every line you type. The terminal output is not saved anywhere otherwise.
  Keep it on for real-radio runs, and send the file (not a screenshot) when
  asking for help debugging a link.
- `--radio` alone uses the only interface attached to that computer; with two
  attached, name one: `--radio aioc` or `--radio digirig`.
- `--ack` retries each fragment until it is acknowledged. `--auto`
  negotiates one mode/FEC for both directions on the first message you send
  (a short exchange of control bursts first; it took about 15 seconds in
  simulation, so allow for that the first time). Both are optional: for the simplest possible test use
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
| something odd on the link | rerun with `--log-file` on both stations and look at the timestamps: who keyed when, what each side heard and at what SNR |

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
