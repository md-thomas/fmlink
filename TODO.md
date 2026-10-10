# OpenWave — TODO / Status

Condensed pointer into [`tasklist.txt`](tasklist.txt), which is this
project's own exhaustive, phase-by-phase status log (1000+ lines,
reconciled multiple times as the project's direction shifted) — this file
does not attempt to reproduce that detail, only to surface what's
genuinely still open right now in one place, per
[REQUIREMENTS.md](REQUIREMENTS.md)'s companion pattern. When this file and
`tasklist.txt` disagree, `tasklist.txt` is authoritative — it's updated
far more frequently and carries the full reasoning behind each item.

## Where we are (2026-10-09)

**Stopped here for the night (2026-10-09).** All repos on current `main` on
both laptops. The user's two web stations (`openwave_web/run.sh --radio`,
K0MDT-1 on server1, K0MDT-2 on lenovo) were left running - stop them with
Ctrl-C when done. Pick up with: the Radio
screen's PTT test and test tone on the real radios (both web stations up,
watch each radio and the other station's level), then web step 5 or the
open items below.

**Web interface: [openwave_web](../openwave_web) (2026-10-09).** A new repo
(MIT) that runs one station with a browser page, opened from any computer
or phone on the network: chat with delivery progress, the SNR of received
bursts and the constellation (each with a Y-scale control), the radio's
receive level and its last 30 s, every setting changed while running
(callsigns, mode, FEC, bandwidth, ACK, auto, batching, lead-in, max
transmission, TX level, turnaround), PTT and test-tone checks, a log;
collapsible and resizable panels, phone layout, dark mode. Built on two
additions here: the station's event feed (`on_event`) and
`Station.update_settings()` (plus `/tx-level`, `/turnaround`, `/max-tx`,
`/batch`). Plan and status: openwave_web's SCOPE.md. **Still to do:** try
the Radio screen's PTT / test tone on the radios; step 5 (test tools in
the page, a TNC screen).

**Renamed (2026-10-09):** the GitHub repos are `openwave`, `openwave_ui`,
`openwave_web`, and the local folders match (`~/Projects/openwave`, ...).
server1 keeps a link `/data/Projects/open_wave -> openwave` for old paths.

**K0MDT-2 now uses an AIOC instead of the Digirig (2026-10-09).** After
re-seating it in the Baofeng and re-levelling both radios (hiss ~-15 dBFS):
modes 2 and 3 delivered 5/5 each way, mode 3 at 16.8 dB (TIDRADIO ->
Baofeng) and 13.4 dB (Baofeng -> TIDRADIO); RADIO_SETUP.md "K0MDT-2 on an
AIOC". With the Digirig gone, the Pat re-test
at the default turnaround (below) can now settle whether the "TIDRADIO
delay" was really the Digirig's AGC.

Other open items, roughly in order: the over-eager fading detector (it can
halve the speed on a static link), Pat with the `axports` window at 7
(burst batching for AX.25), digipeating, weak-link tests, soft-decision
Viterbi, CPU use before a Raspberry Pi.

## Where we were (2026-10-08)

**Stopped here for the night (2026-10-08, later).** Nothing running on
either laptop; both on current `main`. Done today: receive equalizer, burst
batching (parts 1 and 2, on air: 8000 characters in 31.5 s instead of 50 s),
the modem diagram (MODEM_ARCHITECTURE.md). Pick up with: the over-eager
fading detector (it can halve the speed - see below), then Pat with the
`axports` window at 7. To run the stations: RADIO_SETUP.md "Running the
live stations" (check the Digirig's AGC is off first).

**Receive equalizer done (2026-10-08, `9d78fc7`).** A 9-tap FIR fitted to
each burst's marker undoes the HTs' audio tilt. On the same on-air
recordings, mode 3 went from 11.6 to 22.2 dB (TIDRADIO -> Baofeng) and
from 7.6 to 13.3 dB (Baofeng -> TIDRADIO); mode 2 gained 3-8 dB. Table in
RADIO_SETUP.md "Receive equalizer". Also 2026-10-08: the message length
test (all intact at mode 3, ~160 chars/s on long messages).

**Burst batching (BURST_BATCHING_PROPOSAL.md, accepted 2026-10-08).**
Part 1 done: bursts queued together share one key-up (`--max-tx-s`, 30 s),
so an AX.25 window goes out in one transmission. Part 2 done: batched
message segments with one selective ACK per batch, out-of-order
reassembly, negotiated `CAP_BATCH` (on by default; `--no-batch`). Over the
simulated radio, 8000 characters at mode 3 take 31 s instead of 51 s.
**On the air (2026-10-08): 8000 characters in 31.5 s instead of 50.0 s**
(~254 chars/s, all 10 test messages intact, no resends). Still to try: Pat
with the `axports` window raised to 7 (part 1 for AX.25).

**Open: the fading detector is too eager on a static link.** On one
negotiation the probe read 4% of the burst in dropouts (threshold 3%) with
the radios on dummy loads, so `--auto` used the fading table and picked
mode 2 / `r1_2_k7` instead of mode 3 / `r1_2_k3` - half the speed. Look at
`openwave_audio.burst_quality` on real recordings (maybe since the
equalizer, or the 5 ms window is too short at 600 baud) before trusting
it on air.

Other candidates: re-test Pat at the default turnaround (AGC theory),
digipeating/gateway, weak-link tests.


**Stopped here for the night (2026-10-07); pick up tomorrow.** Mode 3 on the
mic port is live on air: `ota_station.py --phy audio --audio-port mic
--radio --ack --auto` negotiated mode 3 both ways with no extra switches
(TIDRADIO -> Baofeng 11.4 dB, Baofeng -> TIDRADIO 7.6 dB). **Next: the
receive equalizer** - the Baofeng -> TIDRADIO direction's treble tilt costs
~7 dB that negotiation can't see (its 600 baud probe read 20.6 dB, so it
predicted ~14.6 dB at mode 3), leaving mode 3 thin margin there. Station
setup to resume: K0MDT-1 = server1 (AIOC + TIDRADIO, `--tx-level 0.6
--turnaround-ms 3000`), K0MDT-2 = `lenovo` (Digirig + Baofeng then; an
AIOC + Baofeng since 2026-10-09).

**Winlink over the air works (2026-10-07).** Pat on K0MDT-1 (server1, AIOC +
TIDRADIO HT) sent a peer-to-peer message to Pat on K0MDT-2 (`lenovo`, Digirig
+ Baofeng) through two OpenWave TNCs at mode 2: handshake to disconnect in
42 s, message intact in K0MDT-2's inbox. Radios on low power into dummy
loads about 2 m apart; SNR about 13-14 dB each way. KISS TNC Milestone 2 is
done. Raw bursts (9/9 each way) and `axcall` to `ax25d` passed first.

What it took (details in RADIO_SETUP.md's Pat section):

- **Re-test in a few days (noted 2026-10-08, low priority):** was it really the
  TIDRADIO, or the Digirig's AGC? The Digirig's AGC was found on later the
  same evening (it resets on replug), and on 2026-10-08 a live chat session
  with AGC off ran fine at the default turnaround. Repeat the Pat test with
  K0MDT-1's TNC at the default `--turnaround-ms`; if it delivers, drop the
  3000 ms workaround and correct the notes below and in RADIO_SETUP.md.
- **The TIDRADIO HT sends a carrier without its mic audio for 2-3 s after it
  has been receiving.** The first frames K0MDT-1 sent after hearing K0MDT-2
  arrived as a blank carrier, so the 73-byte I-frame of the B2F proposal never
  got through and the session stalled at `>F>`. Fixed for now with a longer
  turnaround on that station: `--turnaround-ms 3000` (new option on
  `openwave_tnc.py` and `ota_station.py`; default 600). Not caused by dual
  watch, squelch, STE, VOX, mic gain or battery save. **Open:** find the
  radio setting (firmware HAM_230923, probably a TD-H8) - the 3 s costs time
  at every turn.
- **Receiver fix:** with two bursts back to back in the buffer the detector
  could decode the second (stronger marker) and drop the first. It now looks
  for an earlier burst before the one it found (`tests/test_openwave_radio.py`).
- `openwave_tnc.py --log-file` writes the per-burst trace (heard or not, SNR,
  sync quality) - how the above was found.
- Setup gotchas: the pty link must not be in `/tmp` (root can't follow a
  user's symlink there), one `axports` line per port name, restart Pat's
  listener after a stalled session.
- **Digirig reset (fixed):** replugging the Digirig turned its AGC back on
  (capture 100 %), which dropped K0MDT-1 -> K0MDT-2 from ~20 to ~7 dB. AGC
  off + capture 2 brought mode 2 back to 20.6 dB.

**Radio audio response and mode 3 (2026-10-07).** Tone sweeps through both
directions: the HTs pass ~300-3000 Hz and nothing above ~4 kHz, so **modes 4
and 5 can't work through an HT's mic/speaker path.** **Mode 3 (2400 baud)
does work on these HTs** with the data profile (2000 Hz carrier): 5/5 at
11.4 dB (TIDRADIO -> Baofeng) and 5/5 at 7.8 dB (Baofeng -> TIDRADIO). The
Baofeng -> TIDRADIO direction rolls off ~6 dB/octave above 1 kHz and is
**distortion-limited**: an 18 dB TX-level sweep moved its mode 2 SNR only
12.5 -> 13.6 dB (this explains the earlier "SNR ~14 dB whatever the TX level"
note). Details: RADIO_SETUP.md "Frequency response and faster modes".
Follow-ups:
- ~~Receive equalizer~~ - done 2026-10-08: +5.7 dB (weak direction) and
  +10.7 dB (strong direction) at mode 3 on air.
- ~~Allow mode 3 on the mic port~~ - done 2026-10-07: the `mic` profile
  carries up to 2400 baud, mode 3 on a 2000 Hz carrier (modes 0-2 stay on
  1650 Hz; `openwave_audio.carrier_for`). The existing MODE_SNR_TABLE row
  fits: software noise sweep cliffs for mode 3 (all delivered at 8.2 dB,
  ~half at 5 dB) match mode 2's, so `--auto` picks mode 3 from a 600 baud
  probe of ~15 dB. Still to confirm on the air with `--auto`.
- **Message length test on the air (2026-10-08):** ten messages, 10 to 8000
  characters, all delivered intact at mode 3 with no retries; ~3 s for a
  short message, 6.5 s for 1000 characters, 50 s for 8000 (~160 chars/s,
  each 1024-char segment waiting for its own ACK - burst batching would
  help). Table in RADIO_SETUP.md "Message length test".
- ~~`radio_link_test.py` `--post` with a remote transmitter~~ - defaults to 5 s
  then. `radio_response.py` added (the sweep as a tool).

**The live station now runs over real radios.** Two computers, each with an
HT on a USB audio interface (an AIOC and a Digirig), run
`ota_station.py --phy audio --audio-port mic --radio --ack --auto` and
exchange messages in both directions over the air, with mode negotiation
and ACKs working. Setup, per-station commands, audio leveling, results and
troubleshooting are in [RADIO_SETUP.md](RADIO_SETUP.md). Before that, the
full FM chain worked in simulation (two stations through `ota_sim`'s FM
transceiver model; `./fm_test.sh`).

Done most recently:

- **Real-radio transport** (`openwave_radio.py`): continuous burst
  detection using the station's current mode/FEC attempts, queued PTT
  transmit, receive paused while keyed, a turnaround before replying, and
  extra reply-timeout allowance. `--radio [aioc|digirig]` on
  `ota_station.py`; `ota_sim` is no longer needed unless used.
- **Interface detection** (`radio_stations.py`): AIOC (audio + serial DTR
  PTT) and Digirig (CM108 audio + serial RTS PTT) found by USB ID;
  per-interface overrides in `stations.toml`. No ssh unless `[scan]` hosts
  are set.
- **Tools:** `level_meter.py` (live receive-level bar - an HT mutes its
  speaker when the cable is plugged in), `radio_send.py` / `radio_listen.py`
  (one-way burst tests, one command per station), `radio_link_test.py`
  (one machine drives both stations over ssh), `air_loopback_test.py` (two
  full stations over an in-memory simulated air, no hardware), and
  `audio_wav_test.py` (burst WAVs offline).
- **One-way over-the-air bursts** (AIOC HT to Digirig HT): modes 0, 1, 2
  decoded at 21.3, 17.2, 15.6 dB SNR (sync quality 0.99-1.00).
- **Mode-table margin (2026-10-04):** a faster mode sees more of an
  audio channel's noise, so negotiation now judges the 600 baud probe's SNR
  at each candidate's own symbol rate on `--phy audio`
  (`select_mode_for_snr(probe_symbol_rate=...)`; the fading table is left
  as it was, since it was calibrated against mode-0 probes already).
  Measured cliffs: mode 0 ~-1 dB, mode 1 ~4.5 dB, mode 2 ~7 dB
  (`audio_margin_sweep.py`). Still open: re-measure under real FM noise
  and fading, and the data port's modes 3-5 (penalties of 6-12 dB).
  **Flagged 2026-10-06, unresolved:** a fresh run of this same script
  didn't reproduce modes 1/2's numbers (mode 0 matched) - see 5.5 in
  `tasklist.txt` for the cross-check and candidates.
- **Lessons learned:** a laptop headphone jack has no usable audio input; the
  CM108's Auto Gain Control must be off (it pumps and clips); the Digirig
  plays only stereo, so playback goes through ALSA `plughw:`; a replying
  station must wait a turnaround or its ACK lands while the peer is deaf.
- Earlier: audio-subcarrier PHY (preamble-correlation sync, PLL drift
  tracking), golden-ratio FEC interleaver (in the spec), fading-aware
  `--auto`, frames decoded at the correlation's marker position, live
  simulator `/commands`, station `/ack`, `/auto`, `/lead-in`.

**KISS TNC (2026-10-05, in progress).** `openwave_tnc.py` makes OpenWave a
KISS TNC, so standard packet software can use it as its modem: the Linux
kernel AX.25 stack via `kissattach`, and through it Pat (Winlink
peer-to-peer), digipeating and Linux RMS Gateway. Two TNCs already
exchange AX.25 frames over the simulator. Plan,
milestones and setup: [KISS_TNC_SCOPE.md](KISS_TNC_SCOPE.md).
**Milestone 1 done (2026-10-05):** the Linux kernel AX.25 stack runs
unmodified over two OpenWave TNCs and the simulated FM link - `beacon`
frames and full `axcall`/`ax25d` connected sessions, in plain, SMACK and
FlexNet KISS framing. **Milestone 2 done on the air (2026-10-07):** Pat
K0MDT-1 -> K0MDT-2 over two HTs (see "Where we are").

**Frame format v2 (2026-10-05).** Shorter bursts: a 20-byte header (was 32),
a 16-byte audio preamble (was 32) and 1-byte FEC preset IDs in negotiation
(`PROTOCOL_V2_PROPOSAL.md`, now in the spec). A mode 0 message + ACK is
4.95 s of airtime (was 6.65 s). **v1 and v2 can't decode each other: update
both laptops before the next over-the-air test.** The weak-link figures
below were measured with v1 bursts.

**Pick up here - weak-link testing (planned 2026-10-04, not started).**
Everything on the air so far was a strong link (about 15-21 dB SNR, 8 dB
or more above mode 2's cliff). To see how the modes and `--auto` behave
near their limits:

- **Get both laptops current first:** `macbookair2` is on `8fb78bd`, behind
  the mode-table fix (`4f6619c`) and the docs.
- **TX-level sweep (start here, controlled and repeatable):** lowering the
  TX audio level lowers the received audio SNR with the RF link unchanged.
  `./radio_send.py --src K0MDT -m 1 2 --count 10 --level 0.5` then 0.25,
  0.12, 0.06, 0.03, with `./radio_listen.py` on the other station (or
  `ota_station.py --tx-level`). Record delivered counts and mean SNR per mode
  per level and compare with the software cliffs (mode 0 ~-1 dB, mode 1
  ~4.5 dB, mode 2 ~7 dB; `audio_margin_sweep.py` - modes 1/2 flagged
  2026-10-06 as unreproduced on another machine, see 5.5 in
  `tasklist.txt`). It tests the real receive
  chain's noise floor, not FM's RF threshold effect.
- **RF-weak link (uncontrolled):** radios on dummy loads a few feet apart
  will stay very strong (leakage is only tens of dB down from the carrier,
  and the receiver hears about -117 dBm), so distance alone won't weaken it.
  Options: separate rooms/floors at the lowest power with the receiver on a
  dummy load, a grounded metal box around one radio, or conducted attenuators
  (60-90 dB, rated above the radio's power). Read SNR from the logs instead of
  trusting a calibration; expect burst-to-burst scatter.
- **Then:** `ota_station.py --auto` at a weak position - does negotiation pick
  a mode that works, and does it survive the next outlier probe? Averaging
  several probes (see the single-probe item below) is the likely fix if not.
- Also still open from the radio work: soft-decision Viterbi, the `data` port,
  closed squelch, CPU use before a Raspberry Pi.

**Payload compression: on hold.** Drafted (DEFLATE with a preset dictionary,
36% smaller chat messages) but Winlink, the expected main traffic, already
compresses its messages. Kept on the local `compression-draft` branch
([PAYLOAD_COMPRESSION_PROPOSAL.md](PAYLOAD_COMPRESSION_PROPOSAL.md) there).
OFDM was discussed and deferred: a throughput gain on strong links, at the
cost of high peak power through the FM limiter.

Next, roughly in order:

1. ~~Update both laptops to current `main` and install `ax25-tools` and
   Pat~~ - done 2026-10-05.
2. ~~KISS TNC Milestone 2: Pat peer-to-peer over the radios~~ - done on the
   air 2026-10-07. Next: the TIDRADIO turnaround setting, then digipeating
   and a gateway ([KISS_TNC_SCOPE.md](KISS_TNC_SCOPE.md)). Burst batching
   would also help: a turn currently costs a turnaround per frame.
3. **Weak-link testing over the air** (planned above, now with v2 bursts).
4. **Phase 5 on real radios - the rest.** Done: audio I/O, PTT and an
   over-the-air test on the `mic` port. Still to do: the faster `data`
   port profile (needs a radio data connector), the closed-squelch case
   (everything so far used open squelch), link-margin/range tests, and
   cutting the receiver's CPU use (it re-decodes its buffer every 0.5 s,
   about half a core) before a Raspberry Pi.
5. **Automatic re-negotiation mid-session.** Stations pick a mode once;
   they should adapt as the link changes (pairs with live control).
6. **Soft-decision Viterbi** (~2 dB at the fringe).
7. **Smarter ARQ for slow fading.** Lost ACKs make a sender give up on a
   message that was actually delivered.
8. **Data-port mode 3 fading table**, and possibly a faster data-port
   mode (16-QAM @ 2400 baud - a protocol decision).
9. **GUI `--phy audio` support.**

## Open (actively relevant — pluggable-PHY / OTA track)

- [ ] **Phase 5 — audio-interfaced radio deployment (5.1, 5.2, 5.3 and
      5.4 done on the `mic` port, 2026-10-04).** Driving real VHF/UHF FM
      transceivers through their audio ports works end to end between two
      computers (AIOC and Digirig interfaces; see
      [RADIO_SETUP.md](RADIO_SETUP.md)):
      - `openwave_audio.py` puts symbols on an audio carrier, with
        preamble-correlation sync and PLL drift tracking.
      - `openwave_radio.py` + `ota_station.py --radio` do the real-time
        audio I/O (5.1) and serial PTT (5.2, DTR on the AIOC, RTS on the
        Digirig); the over-the-air test (5.4) passed in both directions.
      - `ota_sim`'s FM transceiver model remains the hardware-free test.

      Still open: the `data` port profile (modes 3-5, needs a radio data
      connector), the closed-squelch case, CAT/VOX PTT variants,
      range/link-margin tests (7.4), and receiver CPU use on small
      computers (7.5).
- [ ] **6.3 — design an OFDM mode.** A bigger lift than the existing
      constellation-dict approach — needs new sync/framing, not a
      drop-in replacement for the single-carrier chain.
- [ ] **6.4 — link-layer mode-selection/negotiation design** (an in-band
      FEC-preset header field + a bootstrapping rule for how a TX picks a
      scheme for a link it hasn't heard back about yet). Note: Phase 8's
      `ota_station.py` negotiation (8.16/8.21) substantially addresses
      this operationally already — worth checking whether 6.4 is really
      still open or just not marked done in `tasklist.txt`.
- [ ] **6.5 — `WAVEFORM_MODES` isn't wired into the CLI pipeline's own
      flag defaults.** `modulate.py`/`demodulate.py`'s `-t` flag and
      `fec_encode.py`/`fec_decode.py`'s `--preset` flag remain independent
      choices from the Waveform Mode ID table — either wire Mode ID into
      their defaults (with explicit override still allowed) or decide to
      keep them permanently independent.
- [x] **3.2 — bit interleaver/de-interleaver.** Done 2026-10-02:
      `openwave_fec.py` interleaves each FEC block's coded bits with a
      golden-ratio permutation by default (`DEFAULT_INTERLEAVER`,
      specified in OpenWave_Specification.txt's FEC section).
      - Under mobile FM fading at 15 dB CNR, mode 2/r1_2_k7 went from 38%
        to 100% of frames delivered.
      - `FADING_MODE_SNR_TABLE` was recalibrated around it: about 2.75x
        the goodput of the pre-interleaving table at 30 and 80 km/h.
      - Slow fading (5 km/h, ~100 ms fades) still loses about half of
        bursts; ARQ carries it.
      
      Original note: Current FEC
      handles scattered bit errors within a fragment; a burst that takes
      out a large contiguous run within one fragment's coded block is a
      different, unaddressed failure mode.
- [x] **Audio path re-finds sync in hard bits.** Fixed 2026-10-02:
      `fec.decode_frames(first_marker_bit=...)` decodes at the position
      the sample-domain correlation found, and `ota_station.py --phy audio`
      passes it.
      - Over 1,728 fading trials it rescued 25 of 37 sync failures (the
        other 12 had header/payload damage too) and broke none.
      - Mode 0 at 5 km/h and 12 dB went from 41/48 to 48/48.
      - The uncoded (no-FEC) path still re-searches.
      
      Original note: `openwave_audio.demodulate()`
      already locates the marker by sample-domain correlation, but
      `fec.decode_frames()` then searches for it again in demodulated
      bits and rejects any candidate with more than 15% of the marker's
      bits wrong (`DEFAULT_MAX_ERROR_FRACTION`). A fade landing on the
      marker can fail a frame whose header and payload would have
      decoded. Measured as ~1% of frames under mobile fading (8 of 800,
      the same with or without interleaving), about a third of the
      failures left once interleaving is on. Fix: pass the correlation's
      marker position into the decoder instead of re-searching.
- [ ] **No carrier phase recovery for QPSK/16-QAM (found via 8.10).**
      `openwave_modem.py`'s demodulators do plain per-axis hard-decision
      slicing with no Costas loop/differential encoding — measured
      directly: QPSK survives only ±45° of phase rotation before 100%
      BER, BPSK only ±90°. A real fix (differential encoding, or a
      pilot/training sequence for the non-BPSK modes) is still open; the
      OTA simulator is the natural place to verify it once built.
      **Solved on the audio path (2026-10-02):** `openwave_audio.py`
      takes phase from the preamble correlation (no ambiguity) and tracks
      drift with a PLL. Still open for the IQ path, where `ota_sim`'s IQ
      channel deliberately applies no phase rotation.
- [x] **UDP ~65KB single-datagram ceiling on `ota_station.py` (found via
      8.6).** Resolved 2026-10-02: the `ota_sim` relay is now TCP with
      length-prefixed messages, so there is no per-burst ceiling and
      `max_fragment_chars` is simply `MAX_PAYLOAD_LEN`.
      `CONTROL_FEC_PRESET` (r1_2_k9) was chosen only because r1_4_k9
      didn't fit; revisiting it is now a protocol decision, not a size
      constraint.
- [ ] **Negotiation trusts a single SNR probe (found via 8.19).**
      `estimate_snr_db()`'s one-shot reading is noisy enough under fading
      that adjacent reliability tiers' estimate ranges overlap
      substantially — averaging multiple probes in `negotiate()` instead
      of trusting one would shrink this directly. Noted but not done.
      Real-radio evidence (2026-10-04, K0MDT-2's log): two BPSK 600 bursts
      from the same station 6 s apart read 21.4 dB and 12.9 dB, then the
      QPSK 1200 data bursts after them read 16-17 dB (about 3 dB below
      where a 600 baud burst would). One burst was an 8.5 dB outlier, so a
      single reading can mislead negotiation on a link with less margin.
      Averaging a few probes, or taking the minimum, would guard against it.
- [ ] **Concurrent-initiator race in negotiation (known gap, 8.21).** If
      both stations get their own first message to send within the same
      handshake window, both could start as their own initiator
      concurrently — no tie-breaking implemented.
- [ ] **`/ping survey` doesn't guard against a real message mid-survey
      (known limitation, 8.22).** A chat message sent by either operator
      during an active survey would be misinterpreted, since the active
      mode is being changed underneath it.
- [ ] **2.7 — dynamic ACK timeout.** `RetryManager.timeout_s` is
      currently a fixed default, not derived from the active Waveform
      Mode's actual bit rate.
- [ ] **7.3 — formal BER-vs-SNR curves per Waveform Mode**, in simulation
      and over the air (beyond the informal pass/fail sweeps already done
      via `file_xfer_test.sh`/live `/ping survey` runs).
- [ ] **7.4 — two-radio range/link-margin test** with attenuators or
      dummy loads (real hardware, not the OTA simulator).
- [ ] **7.5 — profile/optimize for low-power SBCs** (e.g. Raspberry Pi)
      as a deployment target.

## Open (archived/paused GNU Radio track — low priority)

Superseded by the pluggable-PHY direction; kept for completeness, not
expected to be picked up unless the GNU Radio track is revisited:

- [ ] 2.9 — closing the ACK loop for `tx_pluto.grc`/`rx_rtlsdr.grc` (needs
      a full-duplex hardware pairing, not just software).
- [ ] 2.10 follow-up — `rx_rtlsdr.grc`'s `epy_block` has its own separate
      copy of `LinkReceiver`'s dispatch logic, predating that class, not
      yet refactored to share it (real duplication, low-priority cleanup).
- [ ] 4.3-4.5 — an actual over-the-air test between Pluto TX and RTL-SDR
      RX, AGC/gain/frequency-offset tuning for real-world conditions, and
      validation across all defined symbol rates/channel constraints.

## Done (by phase — see `tasklist.txt` for the full narrative)

- [x] **Phase 1** (archived) — GNU Radio pi/4-QPSK PHY chain, validated
      in a loopback testbench. Superseded by the pluggable-PHY direction.
- [x] **Phase 2** (mostly done) — link-layer framing: LFSR PN preamble,
      real 32-byte header, CRC32, `LinkReceiver` RX dispatch, ACK/retry,
      fragmentation with a Fragment Index field (fixed a real silent-
      truncation bug this uncovered).
- [x] **Phase 3** (mostly done, CLI pipeline) — convolutional FEC +
      vectorized Viterbi decoding, 13-preset table via principled
      Dijkstra-based generator search, empirically-measured
      SNR-to-preset reference table.
- [x] **Phase 4** (archived/paused) — GNU Radio SDR hardware bring-up
      (Pluto TX / RTL-SDR RX flow graphs). Superseded.
- [x] **Phase 6** (in progress, mostly shipped) — multi-modulation:
      pluggable modulator/demodulator dict, QPSK and 16-QAM added
      alongside BPSK, both Gray-coded with verified real-world Eb/N0
      tradeoffs (~3dB per step-up, matching theory).
- [x] **Phase 7** (in progress, mostly shipped) — 224-test regression
      suite (framing/modem/pulse-shaping/channel/sync/FEC, all
      GNU-Radio-free), `openwave_cli.py` file-based testing, turnkey
      `pipeline_test.sh`/`file_xfer_test.sh` end-to-end scripts.
- [x] **Phase 8** (in progress, ~33 sub-items shipped, 8.1-8.33) — the
      live OTA simulator track: `ota_sim` sibling project (channel
      physics + TCP relay, plus an FM transceiver audio-path model), `ota_station.py` interactive station,
      per-fragment ACK/retry, initiator-driven symmetric mode/FEC
      negotiation (redesigned once after live testing exposed an
      asymmetric-grant problem), `/ping`+`/ping survey` diagnostics with
      EVM%/RTT/failure-reason detail, `/mode`/`/fec`/`/bandwidth`/
      `/renegotiate`/`/help` interactive commands, and numerous real bugs
      found only by live bidirectional testing (wrong-mode demod crashes,
      a missing `'truncated'` counter key, an `OTALink` fading model that
      was invalidating the phase-recovery-free modem's constellation
      entirely, a `WAVEFORM_MODES` throughput-ordering gap, MODE_SNR_TABLE
      recalibration against the real channel more than once).

## Backlog / considered and not pursued

- [ ] LDPC was considered as an FEC alternative (Phase 3) — would need a
      new codec built from scratch (parity-check matrix design, encoder,
      iterative belief-propagation decoder) for a coding-gain benefit,
      not a speed one. Convolutional + vectorized Viterbi already meets
      current needs; not pursued.
- [ ] pi/4-QPSK's differential ring variant (Phase 1, archived) is not
      currently planned as an addition to the pluggable-PHY track.
- [ ] True windowed/selective-repeat ACK (multiple fragments in flight,
      retry only actual gaps) instead of per-fragment stop-and-wait — would
      also need `FragmentReassembler` reworked to tolerate out-of-order
      arrival (today any out-of-sequence fragment aborts the run). A
      possible future *latency* optimization only; stop-and-wait already
      fully solves the reliability problem it was introduced for.

## How to use this file

- This file is a summary layer — when adding a genuinely new open item,
  add it to `tasklist.txt` first (it's the authoritative, detailed
  record), then mirror a one-line pointer here if it's significant enough
  to want visible without reading the full tasklist.
- Move an item to Done with a one-line summary when it ships, same as
  `tasklist.txt`'s own `[x]` convention — but leave the deep reasoning in
  `tasklist.txt`, not here.
