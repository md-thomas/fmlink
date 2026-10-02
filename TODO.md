# OpenWave — TODO / Status

Condensed pointer into [`tasklist.txt`](tasklist.txt), which is this
project's own exhaustive, phase-by-phase status log (1000+ lines,
reconciled multiple times as the project's direction shifted) — this file
does not attempt to reproduce that detail, only to surface what's
genuinely still open right now in one place, per
[REQUIREMENTS.md](REQUIREMENTS.md)'s companion pattern. When this file and
`tasklist.txt` disagree, `tasklist.txt` is authoritative — it's updated
far more frequently and carries the full reasoning behind each item.

## Open (actively relevant — pluggable-PHY / OTA track)

- [ ] **Phase 5 — audio-interfaced radio deployment (NOT STARTED).** The
      actual eventual deployment target: driving real VHF/UHF
      transceivers through their audio ports (not SDR hardware). Needs
      real-time audio I/O without buffer underruns (5.1), PTT control to
      key the transceiver (5.2, e.g. CAT/serial RTS-DTR/VOX), handling a
      real radio's audio passband/deviation characteristics vs. Pluto/
      RTL-SDR's direct complex baseband (5.3), and an actual over-the-air
      test between two audio-interfaced radios (5.4).
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
- [ ] **3.2 — bit interleaver/de-interleaver not built.** Current FEC
      handles scattered bit errors within a fragment; a burst that takes
      out a large contiguous run within one fragment's coded block is a
      different, unaddressed failure mode.
- [ ] **No carrier phase recovery for QPSK/16-QAM (found via 8.10).**
      `openwave_modem.py`'s demodulators do plain per-axis hard-decision
      slicing with no Costas loop/differential encoding — measured
      directly: QPSK survives only ±45° of phase rotation before 100%
      BER, BPSK only ±90°. A real fix (differential encoding, or a
      pilot/training sequence for the non-BPSK modes) is still open; the
      OTA simulator is the natural place to verify it once built.
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
