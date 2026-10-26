# FMlink on real radios

How to run `ota_station.py` over two real FM radios, one per computer, with
no simulator and no connection between the computers except the radio link.
Verified 2026-10-04 with an HT on an AIOC adapter and an HT on a Digirig,
in both directions, with `--ack --auto`: mode negotiation, messages and
ACKs all worked.

> **This transmits.** Use a clear frequency and low power, and run it only
> where you are licensed to. Each station's callsign goes in every frame's
> source field and in the messages you type; FMlink frames are not
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
| **SignaLink** (USB sound card, TI/Burr-Brown PCM2904) | USB sound card `USB Audio CODEC` | **VOX** | no serial port and no CM108-style GPIO on its HID endpoint either - the radio keys itself off the transmitted audio, so there's no PTT permission to set up |

`./radio_stations.py` detects all three automatically by their USB IDs, so
device numbers (`hw:1,0`, `ttyACM0`) can change without breaking anything.

What does **not** work:

- A laptop's 3.5 mm headphone jack. The one tried (MacBookAir5,2 / CS4206)
  is output-only for plain audio; an HT's cable into it records silence.
  Use a USB interface.
- Bluetooth KISS TNCs (Mobilinkd etc.). They modulate and demodulate
  themselves and only hand over packets, never the radio's audio, which the
  FMlink audio PHY needs.

## One-time setup, per computer

```
cd ~/Projects/fmlink && git pull
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
# aioc       audio hw:CARD=AllInOneCable,DEV=0   PTT DTR usb-AIOC_All-In-One-Cable_...
# digirig    audio hw:CARD=Device,DEV=0          PTT RTS usb-Silicon_Labs_CP2102N_...
# signalink  audio hw:CARD=CODEC,DEV=0           PTT VOX audio-triggered, no PTT line
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

   **Check** (the last line must end in `[off]`):

   ```
   amixer -c Device sget "Auto Gain Control"
   amixer -c Device scontents                   # every setting at once
   ```

   **Set** (AGC off, capture gain 2, playback 30 %):

   ```
   amixer -c Device sset "Auto Gain Control" off
   amixer -c Device sset Mic cap 2
   amixer -c Device sset Speaker 30%            # the Digirig's TX audio level into the radio
   ```

   `-c Device` is the Digirig's ALSA card name; `amixer -c 1 ...` also works
   if it is card 1 (`arecord -l` lists the cards). What `scontents` should
   show when it's right:

   ```
   Simple mixer control 'Speaker',0      ... Playback 45 [30%] [-19.94dB] [on]
   Simple mixer control 'Mic',0          ... Capture 2 [12%] [2.97dB] [on]
   Simple mixer control 'Auto Gain Control',0
     Mono: Playback [off]
   ```

   **Or interactively with `alsamixer -c Device`:** F3 shows the playback
   controls (`Speaker`, and `Auto Gain Control` - press **M** on it to toggle
   it to `MM`/off), F4 the capture control (`Mic`: arrow keys to 2), F5
   everything; Esc quits.

   **These settings reset when the Digirig is unplugged** (AGC back on,
   capture 100 %): check them after any replug, set them again, then
   re-level. `sudo alsactl store` saves the current settings, and Linux
   normally restores saved settings when a sound card appears, so that may
   make them survive a replug - check with `sget` afterwards to be sure.
   On 2026-10-07 a replug left AGC on and halved the link: mode 2 into the
   Digirig fell from ~20 to ~7 dB SNR, and came back to 20.6 dB once AGC was
   off and capture was 2.
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
  the AIOC was left at 100 %, the Digirig's `Speaker` playback at 30 %
  (2026-10-07; was 40 %). Settings used: TIDRADIO mic gain 15 with
  `--tx-level 0.6`; Baofeng (no mic-gain menu) via Digirig `Speaker` 30 %
  with `--tx-level 0.5`.
- **Finding the right TX level:** raise it until the other station's SNR
  stops improving, then back off a step. A level sweep from the Baofeng
  (Digirig `Speaker` 16-80 %, an 18 dB range) moved mode 2's SNR only
  12.5 -> 13.6 dB: that direction is limited by the radios' treble roll-off,
  not level (see "Frequency response" below).
- **Check TX level from the other station:** run `./level_meter.py` on the
  receiving station while the other one sends. A burst should show clearly
  above the idle hiss without pinning at `CLIPPING`. If the received burst
  sounds distorted or clips, lower `--tx-level` (try 0.3); if it is
  weak, raise it (try 0.7). An FM radio's mic path limits and
  pre-emphasises, so too much level shows up as distortion, not volume.
- **Squelch:** everything so far was tested with the receiving radio's
  squelch open (the Baofeng at squelch 0 for the Pat run). With squelch
  closed, the first part of a burst can be lost while the squelch opens;
  raise `--lead-in-ms` (default 250) to cover your radio's squelch-open time.
- **Turnaround:** after hearing a burst, a station waits 0.6 s before keying
  up (`--turnaround-ms`, default 600). Raise it if your radio's transmit
  audio is late right after receiving. **The TIDRADIO HT here needs 3000:**
  for 2-3 s after it has been receiving it transmits a carrier without its
  mic audio, so the other station hears an empty carrier (see the Pat
  section's troubleshooting for how that shows up).

### 3. Both radios

Same frequency, clear channel, low power, FM, the `mic` audio port (the
interfaces above connect to the radio's mic/speaker lines). It carries modes
0-3: modes 0-2 on a 1650 Hz carrier, mode 3 (2400 symbols/s) on 2000 Hz.
Check a new pair of radios with `./radio_response.py` (below) - mode 3 needs
audio out to about 3.6 kHz.

## Running the live stations

One command on each computer; the two stations talk only over the radio link.
Callsigns here are examples: use your own, and make each `--dst` the other
station's `--station-id`.

**Station 1** (for example the AIOC laptop):

```
cd ~/Projects/fmlink
./radio_stations.py                       # confirm the interface is found
./level_meter.py                          # set the radio volume (Ctrl-C when "good")
./ota_station.py --station-id K0MDT-1 --dst K0MDT-2 \
    --phy audio --audio-port mic --radio --ack --auto
```

**Station 2** (for example the Digirig laptop):

```
cd ~/Projects/fmlink
./radio_stations.py
./level_meter.py
./ota_station.py --station-id K0MDT-2 --dst K0MDT-1 \
    --phy audio --audio-port mic --radio --ack --auto
```

**The two stations** (as of 2026-10-09; the commands below were run on
2026-10-08, when K0MDT-2 had a Digirig - mode 3 negotiated automatically,
every burst decoded both ways):

| Station | Computer | Interface + radio | Before starting |
|---|---|---|---|
| K0MDT-1 | server1 | AIOC + TIDRADIO | - |
| K0MDT-2 | lenovo | AIOC + Baofeng (squelch 0); a Digirig until 2026-10-08 | the AIOC's plug fully seated in the Baofeng |

```
# K0MDT-1 (server1)
./ota_station.py --station-id K0MDT-1 --dst K0MDT-2 --phy audio --audio-port mic --radio --ack --auto
# K0MDT-2 (lenovo)
./ota_station.py --station-id K0MDT-2 --dst K0MDT-1 --phy audio --audio-port mic --radio --ack --auto
```

The station keeps its sound card open, so stop it (Ctrl-C) before running
`./level_meter.py` on that computer, or the meter reports "Device or
resource busy". (The web interface below has its own live level meter, so
it doesn't need stopping for that.)

**From a browser:** the [fmlink_web](../fmlink_web) repo runs the same
station with a web page - chat, link SNR and constellation, the receive
level with its history, settings changed while running, PTT and test-tone
checks:

```
cd ~/Projects/fmlink_web && ./run.sh --station-id K0MDT-1 --dst K0MDT-2 --radio --tx-level 0.6   # server1
cd ~/Projects/fmlink_web && ./run.sh --station-id K0MDT-2 --dst K0MDT-1 --radio                 # lenovo
```

then open `http://server1:8080` / `http://lenovo-laptop:8080` (or the IP
addresses) from any computer or phone on the network. Anyone who can open
the page can transmit; `--host 127.0.0.1` keeps it to that computer.

Type a line and press Enter to send it; messages from the other station
print as they arrive. `/help` lists the commands (`/mode`, `/fec`, `/ping`,
`/ping survey`, `/renegotiate`, ...). Settings can be changed while the
station runs: `/tx-level 0.6`, `/turnaround 3000` (ms), `/lead-in 300` (ms),
`/max-tx 20` (s), `/batch on|off`, `/ack`, `/auto`, `/bandwidth`; mode, FEC
and batching changes are passed to the peer. Ctrl-C exits and prints a
session summary.

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
- **Burst batching** (on by default with `--auto --ack`, when both stations
  have it): a long message's segments go out several per transmission with
  one selective ACK per batch, and only missing segments are resent. Over
  the simulated radio, 8000 characters at mode 3 take ~31 s instead of
  ~51 s. A transmission is capped at `--max-tx-s` (default 30 s; keep it
  under your radio's transmit time-out). `--no-batch` turns it off
  (BURST_BATCHING_PROPOSAL.md).
- `--tx-level 0.5` is the default; `--ptt-line dtr|rts|vox` overrides the
  interface's PTT line (`vox` just plays the audio - no line to drive); `-v`
  prints per-message detail.
- The `mic` port carries modes 0-3 (mode 3, 2400 baud, on a 2000 Hz
  carrier); `--auto` picks mode 3 when the link is good enough. A mode faster
  than the port allows (4, 5) is refused. The `data` port profile needs a
  radio data connector and has not been tried.

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
| `./radio_link_test.py --src K0MDT --tx aioc --rx digirig ...` | one machine drives both stations over **ssh** and prints pass/fail and SNR per burst. Needs `[scan] hosts = [...]` in `stations.toml` and passwordless ssh; skip it if you control each station separately. When the transmitting station is the remote one it records 5 s past the burst (starting a transmitter over ssh is slow); `--post` overrides |
| `./radio_response.py --tx aioc --rx digirig@lenovo` | the link's **audio frequency response**: a 6 s tone sweep from one station, recorded by the other (local or over ssh like `radio_link_test.py`); prints the level and SNR per frequency and each mode's band edges. Use it on a new pair of radios to see which modes fit |

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

### Frequency response and faster modes (2026-10-07)

Stations: K0MDT-1 = AIOC + TIDRADIO HT, K0MDT-2 = Digirig + Baofeng, low
power into dummy loads ~2 m apart, open squelch. A 100 Hz - 8 kHz tone sweep
(level 0.3; `./radio_response.py`) through each direction, level relative
to the peak:

| Hz | TIDRADIO -> Baofeng | Baofeng -> TIDRADIO |
|---|---|---|
| 300 | -21 | -10 |
| 500-1000 | 0 | 0 |
| 1650 (mic carrier) | -1.5 | -3.5 |
| 2000 (data carrier) | -2 | -6 |
| 2500 | -3.5 | -9 |
| 3000 | -5.5 | -13 |
| 3700 | -20 | -28 |
| 4100 and up | at the noise | -44 and falling |

- **Modes 4 and 5 can't work through an HT's mic/speaker path:** they need
  audio to ~6.5 and ~13 kHz, and nothing gets through above ~4 kHz.
- **Mode 3 (QPSK 2400 baud) works on these HTs** on a 2000 Hz carrier
  (measured with `radio_link_test.py --port data -m 3`; the `mic` profile
  now allows mode 3 on that carrier, so `--auto` uses it). After fixing the
  Digirig (above):

  | Direction | Mode 2 | Mode 3 |
  |---|---|---|
  | TIDRADIO -> Baofeng | 5/5, 20.6 dB, sync 1.00 | 5/5, 11.4 dB, sync 0.97 |
  | Baofeng -> TIDRADIO | 5/5, 12.4 dB, sync 0.97 | 5/5, 7.8 dB, sync 0.92 |

- **Both directions were distortion-limited, not noise-limited.** Baofeng
  -> TIDRADIO rolls off about 6 dB per octave above 1 kHz (de-emphasis in
  the receiver without matching pre-emphasis at the sender, by the look of
  it). Without an equalizer the tilt smeared the symbols and the SNR stopped
  near 13 dB (mode 2) whatever the TX level, while the noise in that
  direction is low (55-65 dB in the sweep).

### Receive equalizer (2026-10-08)

The receiver now equalizes each burst: a 9-tap FIR (taps every half
symbol, +-2 symbols) fitted by least squares to the burst's known marker,
applied before the tracking loop (`fmlink_audio.demodulate`, on by
default). Same on-air recordings (5 bursts per row, `radio_link_test.py
--keep`), decoded with it off and on; SNR over every symbol after the
marker, against what was transmitted:

| Direction | Mode | Equalizer off | On | Gain |
|---|---|---|---|---|
| TIDRADIO -> Baofeng | 2 | 19.4 dB | 27.3 dB | +7.9 dB |
| TIDRADIO -> Baofeng | 3 | 11.6 dB | 22.2 dB | +10.7 dB |
| Baofeng -> TIDRADIO | 2 | 14.4 dB | 17.8 dB | +3.3 dB |
| Baofeng -> TIDRADIO | 3 | 7.6 dB | 13.3 dB | +5.7 dB |

Mode 3 now has 5-15 dB of margin over its ~8 dB cliff in both directions.
The SNR a station reports (from the marker, corrected for the fitted taps)
matches the whole-burst figure within ~0.5 dB, so `--auto` sees the
equalized link. Through `ota_sim`'s FM model (a flat channel) it changes
nothing: static, Rayleigh fading at 30/80 km/h and 100 ppm clock offset all
delivered the same with it off and on.

### K0MDT-2 on an AIOC (2026-10-09)

K0MDT-2's Digirig was replaced by an AIOC (both stations are AIOCs now;
`radio_link_test.py` names them `aioc` and `aioc@lenovo`). The first
baseline was poor in both directions - mode 3 at 8.2 dB into the Baofeng
and 4.4 dB (1/5 delivered) out of it - with two symptoms: the TIDRADIO's
bursts arrived ~18 dB weaker relative to the Baofeng's hiss than before,
and the Baofeng's carrier carried ~28 dB more noise with no audio playing.
Moving the Baofeng fixed both (its volume jumped at the same moment): most
likely the AIOC's K1 plug wasn't fully seated, or the cable was picking
something up. After that, modes 2 and 3, 5 bursts each way, all delivered:

| Direction | Mode 2 | Mode 3 | Noise under a silent carrier |
|---|---|---|---|
| TIDRADIO -> Baofeng | 18.7 dB | 15.2 dB | 31 dB below the bursts |
| Baofeng -> TIDRADIO | 18.5 dB | 13.3 dB | 40 dB below the bursts |

(Whole-burst SNR, equalizer on; it added 2-5 dB.) Both AIOCs were at
their default mixer settings (input and output 100 %, 0 dB).

**Re-levelled, then the same test again:** both radios' open-squelch hiss
raised to about -15 dBFS rms (TIDRADIO peaks -6.6 dBFS, Baofeng -3.3 dBFS -
don't go higher on the Baofeng). Baofeng -> TIDRADIO stayed at mode 2
18.5 dB / mode 3 13.4 dB (that direction is limited by its treble tilt,
which the equalizer undoes, not by level); TIDRADIO -> Baofeng, held back
by the Baofeng's quiet input before, gained ~2 dB:

| Direction | Mode 2 | Mode 3 | Equalizer gain (mode 2 / 3) |
|---|---|---|---|
| TIDRADIO -> Baofeng | 20.8 dB | 16.8 dB | +3.0 / +5.9 dB |
| Baofeng -> TIDRADIO | 18.5 dB | 13.4 dB | +3.1 / +4.9 dB |

All 20 bursts delivered. Mode 3 has ~9 dB of margin over its ~8 dB cliff
one way and ~5-6 dB the other. Baofeng -> TIDRADIO matches the Digirig at
mode 3; TIDRADIO -> Baofeng is a few dB under the Digirig's best (22.2 dB).

### Airtime by mode and message size

How long a message is on the air at each mode the mic port carries, with
the FEC `--auto` picks for it (`MODE_SNR_TABLE`). Computed from the modem
code (burst airtime including the 250 ms lead-in), not measured:

| Characters | Mode 0 (BPSK 600, `r1_4_k9`) | Mode 1 (BPSK 1200, `r1_2_k3`) | Mode 2 (QPSK 1200, `r1_2_k3`) | Mode 3 (QPSK 2400, `r1_2_k3`) |
|---|---|---|---|---|
| 10 | 2.5 s | 0.9 s | 0.6 s | 0.4 s |
| 100 | 7.3 s | 2.1 s | 1.2 s | 0.7 s |
| 250 | 15.3 s | 4.1 s | 2.2 s | 1.2 s |
| 500 | 28.6 s | 7.4 s | 3.8 s | 2.1 s |
| 1000 | 55.3 s | 14.1 s | 7.2 s | 3.7 s |
| 2500 | 2 min 19 s | 35.5 s | 18.2 s | 9.5 s |
| 4000 | 3 min 41 s | 56.3 s | 28.7 s | 14.9 s |
| 8000 | 7 min 22 s | 1 min 53 s | 57.4 s | 29.8 s |
| Long messages | ~18 chars/s | ~71 chars/s | ~139 chars/s | ~269 chars/s |

Real delivery adds ~2.5-3 s per exchange (PTT key-up, the other station's
turnaround, the ACK back): with burst batching (the default) about once per
message up to a 30 s transmission (`--max-tx-s`), without it once per
1024-character segment. Measured at mode 3 (below): 8000 characters in
31.5 s batched against 29.8 s of airtime, 50 s stop-and-wait. At mode 0 a
full 1024-character segment is ~55 s on its own, past the 30 s cap, so
each segment goes as its own transmission.

### Message length test (2026-10-08)

Ten chat messages from K0MDT-1 (AIOC + TIDRADIO) to K0MDT-2 (Digirig +
Baofeng), 10 to 8000 characters, each sent after the previous one was
acknowledged. Both stations ran the plain live-station command
(`--phy audio --audio-port mic --radio --ack --auto`, default levels and
turnaround); `--auto` picked mode 3 (QPSK 2400 baud, `r1_2_k3`) on the first
message. **All ten delivered and arrived intact, with no retries;** every
real burst decoded both ways (38 each), at ~11.5 dB into the Baofeng and
~7.6 dB for the ACKs into the TIDRADIO.

| Length (chars) | Segments | Typed to ACK | Rate |
|---|---|---|---|
| 10 | 1 | 12.5 s (includes ~9 s of mode negotiation) | - |
| 40 | 1 | 3.0 s | 13 chars/s |
| 100 | 1 | 3.5 s | 29 chars/s |
| 250 | 1 | 4.0 s | 63 chars/s |
| 500 | 1 | 5.0 s | 100 chars/s |
| 1000 | 1 | 6.5 s | 154 chars/s |
| 1500 | 2 | 11.0 s | 136 chars/s |
| 2500 | 3 | 17.5 s | 143 chars/s |
| 4000 | 4 | 25.5 s | 157 chars/s |
| 8000 | 8 | 50.0 s | 160 chars/s |

Each message costs ~2.5-3 s of fixed overhead (key-up, lead-in, preamble,
the ACK back) plus ~3.5 s per 1000 characters. Long messages settle near
160 chars/s (~1280 bit/s of text, against mode 3's 2400 bit/s after FEC):
each 1024-character segment waits for its own ACK.

**With burst batching (2026-10-08, same radios, same messages, mode 3 /
`r1_2_k3`):** several segments per transmission and one selective ACK per
batch (BURST_BATCHING_PROPOSAL.md). All ten delivered intact, no resends;
the 8000-character message went as one ~28 s transmission of all 8
segments.

| Length (chars) | Segments | Stop-and-wait | Batched | Faster |
|---|---|---|---|---|
| 10-1000 | 1 | 3.0-6.5 s | 3.0-6.5 s | - (one segment: unchanged) |
| 1500 | 2 | 11.0 s | 8.5 s | 23% |
| 2500 | 3 | 17.5 s | 12.0 s | 31% |
| 4000 | 4 | 25.5 s | 17.5 s | 31% |
| 8000 | 8 | 50.0 s | 31.5 s | 37% |

Long messages now run at ~254 chars/s (the in-memory radio predicted 257).
On the first batched run `--auto` chose mode 2 / `r1_2_k7` instead (its
probe read 4% of the burst in dropouts, just over the 3% fading threshold,
on a static link); batched at mode 2, 8000 characters took 62 s. The run
above was after switching both stations to mode 3 / `r1_2_k3` with `/mode 3`
and `/fec r1_2_k3`.

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
| `level_meter.py`: `audio open error: Device or resource busy` | a station, TNC or test is using that sound card: stop it first |
| SNR into the Digirig suddenly much lower (e.g. 20 -> 7 dB) | the Digirig was replugged and its AGC came back on: `amixer -c Device sget "Auto Gain Control"`, then the settings above |
| Both directions of one station 10+ dB worse, its transmissions noisy even with no audio, its received bursts weak | the AIOC's (or any K1 cable's) plug isn't fully seated in the HT, or the cable is picking something up: push it home or move the radio/cable (fixed it on 2026-10-09 - the radio's audio got audibly louder at that moment) |
| `radio_stations.py` shows nothing | check `lsusb` for the adapter and `arecord -l` for its sound card |
| stations hear nothing from each other | wrong PTT line (try `--ptt-line dtr` or `rts`; the radio's TX light shows it keying), TX level too low or too high, different frequencies, or squelch closed |
| SignaLink doesn't key up, or clips the start of each burst | VOX needs a moment of audio energy before it engages, which is why `ptt_play.py` gives it a short tone first instead of relying on the lead-in's silence. The "PTT test" (silent carrier) won't key a VOX radio at all - use the test tone instead |
| receiver goes completely deaf after a wall of `overrun!!!` lines and `arecord: pcm_read: read error` in the log - every mode then fails, even ones that worked moments before | the capture hit a fatal ALSA error and `_rx_loop` gave up on it for good (fixed: it now reopens the capture after `RadioTransport.CAPTURE_RETRY_S` (2 s) instead); on an older checkout, restart the station. The recurring overrun itself is usually a CPU-starved receiver (see "high CPU on a small computer" below). This looked exactly like a SignaLink-specific mode 3 bandwidth limit on 2026-10-24 until both stations were confirmed running this fix - see `fmlink_audio.py`'s PORT_PROFILES comment |
| the sender keeps retrying and the peer reports "duplicate" | the peer's ACK is not getting back: check the levels and PTT in that direction; the transport already waits a 0.6 s turnaround before replying |
| high CPU on a small computer | the receiver re-decodes its audio buffer every 0.5 s (about half a core with 2-3 candidate modes) |
| `[radio tx error]` | the line after it names the `aplay` or serial error |
| something odd on the link | read the `logs/` file on both stations and compare the timestamps: who keyed when, what each side heard and at what SNR |

## How it works (short)

`fmlink_radio.py` gives `ota_station.py` a radio in place of the simulator,
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

## Winlink peer-to-peer (Pat) over FMlink

`fmlink_tnc.py` runs FMlink as a KISS TNC on the radio, Linux's AX.25
stack attaches to it, and Pat (a Winlink client) sends Winlink messages
station to station over it. **Works on the air (2026-10-07):** Pat K0MDT-1
(AIOC + TIDRADIO HT) -> Pat K0MDT-2 (Digirig + Baofeng) at mode 2, low power
into dummy loads, message delivered in 42 s (18.6 s over the simulator).
Design and status: [KISS_TNC_SCOPE.md](KISS_TNC_SCOPE.md).

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
printf 'ow\tK0MDT-1\t9600\t255\t2\tFMlink TNC\n' | sudo tee -a /etc/ax25/axports

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
git clone https://github.com/md-thomas/fmlink.git ~/Projects/fmlink
cd ~/Projects/fmlink
sudo usermod -aG dialout $USER          # PTT serial port; log out and back in
sudo modprobe ax25 && sudo modprobe mkiss
printf 'ax25\nmkiss\n' | sudo tee /etc/modules-load.d/ax25.conf
printf 'ow\tK0MDT-2\t9600\t255\t2\tFMlink TNC\n' | sudo tee -a /etc/ax25/axports
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
./fmlink_tnc.py --callsign K0MDT-1 --radio --mode 2 --kiss-port 0 --pty-link ~/kiss-ow \
    --log-file ~/tnc-K0MDT-1.log
# 2. Attach it to the kernel (any KISS framing mode works, including the default)
sudo kissattach ~/kiss-ow ow
```

Frames the kernel hands over together go out in one key-up (burst
batching, `--max-tx-s`, default 30 s), so a larger AX.25 window pays off:
the window is the fifth column of `axports` (2 above; up to 7).

As run on 2026-10-07: K0MDT-1 added `--tx-level 0.6 --turnaround-ms 3000`
(the TIDRADIO, see "Turnaround" above), K0MDT-2 used the defaults.

- **Keep the pty link in your home directory, not `/tmp`:** the kernel won't
  let root follow a symlink you own in `/tmp` (`fs.protected_symlinks`), so
  `kissattach` fails with "Permission denied".
- **`--log-file`** records every burst heard (decoded or not, SNR, sync
  quality) and every transmission, timestamped. Compare both stations' logs
  when something stalls.
- **Restarting a TNC gives it a new pty:** `sudo pkill kissattach; sleep 2;
  sudo kissattach ~/kiss-ow ow`, then restart the Pat listener. Without the
  pause, the old `kissattach` can take the new port down as it exits.

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
| `kissattach ... Permission denied` on the pty | The pty link is in `/tmp`; use `--pty-link ~/kiss-ow` |
| `cannot find port ow in axports`, or the port has the wrong callsign | One line per port name in `/etc/ax25/axports` with this station's own callsign; a duplicate `ow` line makes `kissattach` take the first |
| Pat connects and sends `>FD ...` / `>F>`, then stalls; the TNC logs show the first frame of each retry never arrives | The sending radio's audio is late after receiving (the TIDRADIO): raise `--turnaround-ms` on that station (3000 worked) |
| A new `pat connect` gets no greeting | The listener is still in the stalled session: restart `pat --listen` |
