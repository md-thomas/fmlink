# OpenWave — TODO / Status

Condensed pointer into [`tasklist.txt`](tasklist.txt), which is this
project's own exhaustive, phase-by-phase status log (1000+ lines,
reconciled multiple times as the project's direction shifted) — this file
does not attempt to reproduce that detail, only to surface what's
genuinely still open right now in one place, per
[REQUIREMENTS.md](REQUIREMENTS.md)'s companion pattern. When this file and
`tasklist.txt` disagree, `tasklist.txt` is authoritative — it's updated
far more frequently and carries the full reasoning behind each item.

## Where we are (2026-10-04)

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
- **Lessons learned:** a laptop headphone jack has no usable audio input; the
  CM108's Auto Gain Control must be off (it pumps and clips); the Digirig
  plays only stereo, so playback goes through ALSA `plughw:`; a replying
  station must wait a turnaround or its ACK lands while the peer is deaf.
- Earlier: audio-subcarrier PHY (preamble-correlation sync, PLL drift
  tracking), golden-ratio FEC interleaver (in the spec), fading-aware
  `--auto`, frames decoded at the correlation's marker position, live
  simulator `/commands`, station `/ack`, `/auto`, `/lead-in`.

Next, roughly in order (pending: are radios + an audio/PTT interface
available?):

1. **Phase 5 on real radios - the rest.** Done: audio I/O, PTT and an
   over-the-air test on the `mic` port. Still to do: the faster `data`
   port profile (needs a radio data connector), the closed-squelch case
   (everything so far used open squelch), link-margin/range tests, and
   cutting the receiver's CPU use (it re-decodes its buffer every 0.5 s,
   about half a core) before a Raspberry Pi.
2. **Automatic re-negotiation mid-session.** Stations pick a mode once;
   they should adapt as the link changes (pairs with live control).
3. **Soft-decision Viterbi** (~2 dB at the fringe).
4. **Smarter ARQ for slow fading.** Lost ACKs make a sender give up on a
   message that was actually delivered.
5. **Data-port mode 3 fading table**, and possibly a faster data-port
   mode (16-QAM @ 2400 baud - a protocol decision).
6. **GUI `--phy audio` support.**

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
