# OpenWave — Requirements

This document summarizes what `open_wave` is for and what it must do. It
sits alongside two existing, more detailed docs already in this repo:
[`OpenWave_Specification.txt`](OpenWave_Specification.txt) is the
authoritative wire-format spec (frame layout, header fields, Waveform
Modes table, worked byte-dump example), and [`README.md`](README.md) is
the detailed usage/file-map guide. This document is the higher-level
"what must this project do" summary; when it disagrees with the spec or
the code, they win. See [TODO.md](TODO.md) for status — this project
already tracks its own status in exhaustive detail in
[`tasklist.txt`](tasklist.txt); TODO.md is a condensed pointer into that,
not a replacement for it.

## 1. Purpose

A link-layer/framing protocol (header, CRC, ACK, fragmentation, FEC)
designed to work over a **pluggable PHY** rather than being tied to one
modulation — originally built around a GNU Radio pi/4-QPSK chain, now
repositioned so the pure-Python CLI pipeline (BPSK/QPSK/16-QAM today) is
the actively developed PHY. Two real usage shapes exist side by side:

- A **one-shot, file-based pipeline** — build a frame, push it through
  modulation/pulse-shaping/(optional noise)/matched-filtering/
  demodulation by hand or via a script, inspect/verify the result. No
  process stays running.
- A **live, interactive, bidirectional station** (`ota_station.py`) —
  two processes exchange real frames through the actual modem chain in
  real time, relayed over the network through a simulated RF channel
  (the separate sibling project `ota_sim`), with automatic mode/FEC
  negotiation, per-fragment ACK/retry, and a small set of interactive
  `/`-commands for live link diagnostics.

Both paths share one framing/modem/FEC implementation — nothing is
duplicated between the file-based and live-station code paths.

## 2. Public API / module map

Only eight modules are actually installed as importable Python packages
(`pyproject.toml`'s `py-modules`); everything else in this repo is a CLI
script or test, run directly rather than imported.

**Installed (`pip install`-able) modules:**

| Module | Provides |
|---|---|
| `openwave_link.py` | The link layer — the single source of truth for framing. `build_header`/`parse_header`, `build_frame`/`FrameParser`, `build_ack_frame`/`RetryManager` (ACK/retry state machine), `build_fragmented_frames`/`FragmentReassembler` (fragmentation), `build_mode_request_frame`/`build_mode_grant_frame`/`build_mode_set_frame`/`build_mode_set_ack_frame`/`build_ping_frame`/`build_pong_frame`/`parse_control_payload` (in-band control messages), `iter_frames`/`describe_frames` (byte-level frame scanning), `LinkReceiver` (full RX dispatch: addressing, ACK, fragment reassembly, seq gap/dup tracking, stats — usable standalone, no GNU Radio/hardware), `WAVEFORM_MODES`/`MODE_SNR_TABLE`/`select_mode_for_snr`/`mode_raw_bps` (the PHY-plugin contract and mode-selection logic). |
| `openwave_modem.py` | PHY-layer bits↔symbols mapping: `modulate_bpsk`/`demodulate_bpsk`, `modulate_qpsk`/`demodulate_qpsk`, `modulate_qam16`/`demodulate_qam16` (all Gray-coded, unit average symbol energy), `estimate_snr_db` (blind EVM-style SNR estimator, no pilot symbols), `bytes_to_bits`/`bits_to_bytes`, `symbols_to_iq_bytes`/`iq_bytes_to_symbols`. Deliberately has no symbol-rate concept — that's `openwave_pulse.py`'s job. |
| `openwave_pulse.py` | RRC pulse shaping: `rrc_taps`, `pulse_shape` (symbols → oversampled samples), `matched_filter` (samples → symbols), `decimate`, `group_delay`, `expected_symbol_count`. Needs numpy — the first stage where a real DSP dependency becomes unavoidable. |
| `openwave_channel.py` | AWGN channel simulation: `add_awgn` (direct noise voltage), `snr_db_to_noise_voltage` (target-SNR convenience). |
| `openwave_sync.py` | Tolerant frame synchronization: `find_sync_offsets`/`best_sync_offset` (sliding Hamming-distance correlator for the preamble+sync-word, not an exact-byte match), `iter_frames_correlated`/`describe_frames_correlated`, `extract_frame_at`. |
| `openwave_audio.py` | Audio-subcarrier PHY for a radio's audio port: `modulate` (symbols → real 48 kHz audio on a carrier, with lead-in), `demodulate` (preamble correlation for timing/phase/level, PLL drift tracking, data-aided SNR), `PORT_PROFILES` (carrier and max symbol rate per radio connection), PCM16 helpers. |
| `openwave_kiss.py` | KISS framing for the KISS TNC: `encode`, an incremental `Decoder` (escapes, commands, ports, and the SMACK/FlexNet CRC variants Linux's mkiss uses - `crc_smack`, `crc_flexnet`), and AX.25 address helpers (`ax25_endpoints`, `ax25_ui_frame`). |
| `openwave_fec.py` | Forward error correction: `convolutional_encode`/`viterbi_decode` (numpy-vectorized, parameterized by generator polynomials/constraint length K, not hardcoded to one scheme), `encode_frame`/`decode_frames` (frame-level wrappers used by the CLI), `free_distance`/`find_generators` (principled, Dijkstra-based preset selection), `PRESETS` (13 named rate-1/N configurations, `r1_2_k3` through `r1_6_k9`, plus an empirically-measured SNR-to-preset reference table), coded-bit interleaving per FEC block (`interleave_permutation`, golden-ratio by default - see the spec's FEC section). |

**CLI-only (not pip-installed as a library, run as scripts):**
`openwave_tnc.py` (OpenWave as a KISS TNC for packet software - see
[KISS_TNC_SCOPE.md](KISS_TNC_SCOPE.md)),
`openwave_cli.py` (file-based link-layer testing — `send`/`dump`/`recv`),
`modulate.py`/`demodulate.py`/`pulseshape.py`/`channel.py`/
`matchedfilter.py`/`plot_constellation.py` (the individual PHY-pipeline
stages, chainable by hand), `fec_encode.py`/`fec_decode.py` (FEC wrappers
around the pipeline), `openwave_fec_generate_presets.py` (regenerates the
`PRESETS` table), `ota_station.py` (the live interactive station — see
§3), `pipeline_test.sh`/`file_xfer_test.sh` (turnkey end-to-end test
scripts), and the real-radio tools: `openwave_radio.py` (the radio
transport), `radio_stations.py`, `ptt_play.py`, `level_meter.py`,
`radio_send.py`, `radio_listen.py`, `radio_link_test.py`,
`air_loopback_test.py`, `audio_wav_test.py` (see [RADIO_SETUP.md](RADIO_SETUP.md)). See `README.md` for full usage of each.

## 3. `ota_station.py` — the live station process

Worth its own section: unlike everything else here, this is a real
long-running interactive process, not a one-shot pipeline stage.

- **Interface**: stdin/stdout. Type a line, press Enter — it's framed,
  modulated, and sent; incoming decoded messages from the peer print as
  they arrive. Lines starting with `/` are commands, not messages:
  `/renegotiate`, `/mode [N]`, `/modes`, `/fec [PRESET]`,
  `/bandwidth [narrow|wide]`, `/ping`, `/ping survey`, `/help`/`/?`,
  `/quit`/`/exit`. Up/down-arrow history and line editing work (stdlib
  `readline`, degrades gracefully where unavailable e.g. Windows).
- **Network**: talks to a peer station through `ota_sim` (a separate
  sibling project at `/data/Projects/ota_sim`, its own package, zero
  OpenWave-specific code) — a small TCP relay that models path loss,
  Rayleigh fading, AWGN, and configurable latency between two peers
  exchanging raw IQ samples. Its `--channel fm` mode instead models an FM
  transceiver pair's audio path, carrying PCM audio, which the station
  speaks with `--phy audio --audio-port mic|data` (`openwave_audio.py`).
  Each peer identifies itself by callsign on connect; no static address
  configuration. With `--radio` instead of the simulator, the station runs
  over a real radio's audio interface (`openwave_radio.py`,
  `radio_stations.py`; see [RADIO_SETUP.md](RADIO_SETUP.md)) and needs
  neither `ota_sim` nor any network.
- **Mode/FEC negotiation** (`--auto`): initiator-driven and symmetric —
  whichever station gets a message to send first measures the link
  itself and picks ONE mode/FEC both directions use, via a two-phase
  handshake (`MODE_REQUEST`/reply for measurement, then confirmed
  `MODE_SET`/`MODE_SET_ACK`) over a fixed, always-robust control mode
  (BPSK + `r1_2_k9`). Listen-first, not eager — a station transmits
  nothing until it has its own message to send.
- **Reliability**: `--ack` gives per-fragment stop-and-wait ACK/retry (a
  lost fragment costs only that fragment a retry, not the whole
  message). Duplicate-delivery and fragment-loss are both made visible
  rather than silent.
- **Diagnostics**: `/ping` (5 round trips at the current mode: success
  count, RTT range, measured SNR/EVM%, on-air burst size, failure-reason
  breakdown) and `/ping survey` (genuinely tests every Waveform Mode via
  real transmissions, not a prediction, then restores the original mode
  and prints a consolidated summary with a "best fully-reliable mode"
  pick).

## 4. Signal/protocol format

(Full detail lives in `OpenWave_Specification.txt` — summary here.)

- **Frame format version 2** (since 2026-10; v1 and v2 stations can't
  decode each other - see `PROTOCOL_V2_PROPOSAL.md` for the change and its
  measurements).
- **Preamble**: LFSR pseudo-noise sequence (9-bit register, taps 1/2/5/9),
  32 bytes on the IQ PHY and the first 16 on the audio PHY
  (`link.IQ_MARKER` / `link.AUDIO_MARKER`) — sharp autocorrelation (~9%
  worst sidelobe) for reliable frame-start detection under noise.
- **Sync word**: `0x1ACFFC1D` (`SYNC_WORD`).
- **Header**: 20 bytes, `struct '>BBBH6s6sHB'`:
  - Version and Packet Type, 4 bits each in one byte.
  - Waveform Mode ID, Flags, Sequence Number.
  - Source ID and Destination ID: 6-byte packed callsigns, up to 9
    characters of A-Z, 0-9, `/`, `-`, `.`.
  - Payload Length, Fragment Index.

  There are no reserved bytes. Flags bit 5 marks an optional
  header-extension block at the start of the payload.
- **Integrity**: CRC32 over Header+Payload; RX knows exact frame length
  from the header's Payload Length field, not a marker scan.
- **ACK/retry**: `RetryManager` — pure state machine, no RF knowledge,
  max_retries/timeout.
- **Fragmentation**: `MAX_PAYLOAD_LEN = 1024` bytes/fragment,
  `MAX_FRAGMENTS = 64` (defensive cap per reassembly run); each fragment
  is an independent burst with its own Fragment Index.
- **FEC**: rate-1/N convolutional coding + vectorized Viterbi decoding,
  13 named presets (`r1_2_k3`…`r1_6_k9`); each fragment of a
  multi-fragment message is FEC-protected independently.
- **Modulation (PHY today)**: BPSK, QPSK, 16-QAM — Gray-coded, unit
  average symbol energy. Zero carrier phase recovery (no Costas loop, no
  differential encoding) — see TODO.md, this is a known, real limitation
  for QPSK/16-QAM specifically.
- **Waveform Modes** (`WAVEFORM_MODES`, the PHY-plugin contract —
  modulation + symbol rate + FEC preset per mode ID): 0 bpsk/600,
  1 bpsk/1200, 2 qpsk/1200, 3 qpsk/2400, 4 qpsk/4800, 5 16qam/9600.
- **Pulse shaping**: RRC, `alpha=0.35`, `sps=4`, `span=11` symbols by
  default (must match between TX pulse-shaping and RX matched filtering).

## 5. Dependencies

- `numpy`, `scipy` (`pyproject.toml`) for the installed modules.
- `plotly` for `plot_constellation.py` only (not a core dependency).
- `ota_station.py` depends on the separate sibling package `ota_sim`
  (`pip install -e ../ota_sim`) for the network/channel-simulation layer,
  but only when talking to a simulator; `--radio` needs neither `ota_sim`
  nor a network. The real-radio tools need `pyserial` (PTT) and ALSA's
  `arecord`/`aplay`.
- No dependency on any `general_xxx` sibling modem package — OpenWave's
  BPSK/QPSK/16-QAM implementations are entirely its own
  (`openwave_modem.py`), independent of `general_bpsk`/`general_qpsk`/
  `general_16qam` etc. used by `web_sdr`/`web_siggen`.
- Zero GNU Radio dependency anywhere in the actively developed path — the
  original GNU Radio pi/4-QPSK chain is archived under `legacy/gnuradio/`
  (own README, own tests), kept for the historical record, not deleted.

## 6. Consumers

**Not currently imported by either `web_sdr` or `web_siggen`**, despite
being listed as a shared dependency (`-e ../open_modem/open_wave` in both
projects' `requirements.txt`) — verified directly: no `import openwave`/
`from openwave` anywhere in `web_sdr/backend/sdr_service.py` or
`web_siggen/backend/app.py`. It's installed as a submodule/dependency but
not yet actually wired into either app's own code path. This is worth
knowing before assuming any change here affects either running service —
today, it doesn't; this project is self-contained (its own CLI pipeline
and `ota_station.py`/`ota_sim` live-link testing).

## 7. Non-goals / explicit scope limits

- **Not tied to any one modulation or transport.** The pluggable-PHY
  design (`WAVEFORM_MODES`) is deliberate — adding a new modulation
  means adding a constellation dict entry, not restructuring the link
  layer.
- **Not a bulk-file-transfer tool via `ota_station.py`.** Use
  `file_xfer_test.sh` (the file-based pipeline) for bulk transfers.
- **Negotiation is setup-only, not continuous mid-session adaptation.**
  A bad turn partway through a session doesn't trigger an automatic
  re-probe (though `/renegotiate` can be triggered manually).
- **The archived GNU Radio chain (`legacy/gnuradio/`) is not the active
  development target** — kept for history/reference, not being extended.
- **Real-radio operation is early.** `ota_station.py --radio` works over
  FM radios through an AIOC or Digirig (see [RADIO_SETUP.md](RADIO_SETUP.md)),
  verified between two stations on the `mic` audio port (modes 0-2) with
  `--ack --auto`. Not yet done: the faster `data` port, link-margin and
  range testing, and low-power-computer tuning (TODO.md, Phase 5).
