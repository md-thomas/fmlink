# OpenWave — TODO / Status

Condensed pointer into [`tasklist.txt`](tasklist.txt), which is this
project's own exhaustive, phase-by-phase status log (1000+ lines,
reconciled multiple times as the project's direction shifted) — this file
does not attempt to reproduce that detail, only to surface what's
genuinely still open right now in one place, per
[REQUIREMENTS.md](REQUIREMENTS.md)'s companion pattern. When this file and
`tasklist.txt` disagree, `tasklist.txt` is authoritative — it's updated
far more frequently and carries the full reasoning behind each item.

## Where we are (2026-10-02)

The full FM chain now works end to end in simulation. Two `ota_station.py`
stations (`--phy audio`) talk through `ota_sim`'s FM transceiver model
over the TCP relay. `./fm_test.sh` launches the whole setup, and the
channel can be changed live with `/cnr`, `/fading`, etc. typed into the
simulator's window. Done most recently:

- Audio-subcarrier PHY (`openwave_audio.py`): preamble-correlation sync,
  PLL drift tracking, no frame padding.
- Golden-ratio FEC interleaver, now defined in the spec.
- Fading-aware `--auto` (`FADING_MODE_SNR_TABLE`).
- Frames decoded at the correlation's marker position.
- Live simulator `/commands`, plus station `/ack`, `/auto`, `/lead-in`.
- Spec brought up to date: worked example, Control payloads, audio PHY.

First real-radio steps (2026-10-03/04):

- `audio_wav_test.py gen|decode` makes a burst WAV and decodes a
  recording, offline.
- **Over-the-air reception works (2026-10-04).** An HT on an AIOC
  (USB audio `hw:AllInOneCable,0`, PTT = DTR on `/dev/ttyACM0`) sent a
  mode 0 burst; a second HT on a Digirig (CM108 USB sound card) on
  another laptop recorded it, and `decode` returned the message: sync
  quality 1.00, 21.3 dB SNR (46.6 dB in software loopback).
- Modes 1 and 2 also decode over the air on the same pair (17.2 dB and
  15.6 dB SNR, sync quality 0.99). Modes 3-5 need the `data` port
  profile; the `mic` port caps at 1200 baud.
- `radio_link_test.py` runs the whole over-the-air test in one command
  (keys PTT, plays the burst, records on the other station, decodes,
  prints pass/fail and SNR per burst), in either direction:
  `./radio_link_test.py --src K0MDT --tx aioc --rx digirig -m 0 1 2`.
  Radio interfaces (AIOC, Digirig) are auto-detected on this computer and
  the `[scan]` hosts in `stations.toml` (`./radio_stations.py` lists
  them). `./level_meter.py` is a live level bar for setting the radio's
  volume (an HT mutes its speaker when the cable is plugged in). The
  Digirig's PTT line (RTS) and the digirig-to-aioc direction are still
  untested over the air.
- Receive setup lessons: the MacBook's headphone jack has no usable
  audio input, so a USB sound card is needed; turn the CM108's Auto Gain
  Control off (it pumps and clips); keep capture gain low and the radio
  volume moderate, with squelch open so the start isn't clipped.
- Next: real-time audio I/O and PTT in `ota_station.py`.

Next, roughly in order (pending: are radios + an audio/PTT interface
available?):

1. **Phase 5 on real radios** (sound-card I/O, PTT, over-the-air test).
   Reception of modes 0-2 is confirmed over the air; next is real-time
   audio I/O and PTT in `ota_station.py`.
2. **Automatic re-negotiation mid-session.** Stations pick a mode once;
   they should adapt as the link changes (pairs with live control).
3. **Soft-decision Viterbi** (~2 dB at the fringe).
4. **Smarter ARQ for slow fading.** Lost ACKs make a sender give up on a
   message that was actually delivered.
5. **Data-port mode 3 fading table**, and possibly a faster data-port
   mode (16-QAM @ 2400 baud - a protocol decision).
6. **GUI `--phy audio` support.**

## Open (actively relevant — pluggable-PHY / OTA track)

- [ ] **Phase 5 — audio-interfaced radio deployment (5.3 done in
      simulation).** The actual eventual deployment target: driving real
      VHF/UHF FM transceivers through their audio ports (not SDR
      hardware). 5.3 (a real radio's audio passband/deviation vs. direct
      complex baseband) is handled in simulation as of 2026-10-02:
      - `openwave_audio.py` puts symbols on an audio carrier, with
        preamble-correlation sync and PLL drift tracking.
      - `ota_station.py --phy audio --audio-port mic|data` uses it.
      - `ota_sim`'s FM transceiver model (`--channel fm`) is the channel.
      
      Still needed: real-time audio I/O without buffer underruns (5.1),
      PTT control to key the transceiver (5.2, e.g. CAT/serial
      RTS-DTR/VOX), and an actual over-the-air test between two radios
      (5.4).
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
