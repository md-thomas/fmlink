# OpenWave - Implementation Overview

This file explains what's actually in this repo and how the pieces fit
together. OpenWave is a link-layer/framing protocol (header, CRC, ACK,
fragmentation, FEC) designed to work over a pluggable PHY rather than being
tied to any one modulation - see `openwave_link.py`'s `WAVEFORM_MODES`. The
pure-Python CLI pipeline below (BPSK/QPSK/16-QAM today) is the actively
developed PHY implementation. For the protocol/wire-format spec itself
(frame layout, header fields, Waveform Modes), see
[OpenWave_Specification.txt](OpenWave_Specification.txt). For the current
status of each piece and what's not done yet, see
[tasklist.txt](tasklist.txt).

## File map

**`openwave_link.py`**
The link layer. Header build/parse, CRC32, ACK/retry, fragmentation, the
preamble/sync-word, and a full RX dispatch implementation (`LinkReceiver`)
- all pure Python, zero GNU Radio dependency. This is the single source of
truth for the framing format, and the `WAVEFORM_MODES` table is the
PHY-plugin contract (modulation/symbol rate/FEC preset per Waveform Mode
ID). See its module docstring and `tests/test_openwave_link.py` for the
full API.

**`openwave_cli.py`**
Command-line, file-based testing of the link layer - build and parse real
frame bytes with no GNU Radio and no hardware. `send` writes a frame to a
file (plain text, `--json`, `--file` for raw binary payloads like a PNG, or
`--attach` for a text message plus a file attachment together, with options
to deliberately corrupt it for negative testing); `dump` hex-dumps and
labels every field of every frame in a file; `recv` decodes a file through
the full `LinkReceiver` pipeline (`--output-file` / `--save-attachment` to
write `--file`/`--attach` content back out without lossy text decoding).
Both `dump`/`recv` take an optional `--correlate` flag (see
`openwave_sync.py` below) for data that came through a noisy channel. Run
`python3 openwave_cli.py help` (or `send -h` / `recv -h`) for full usage
and examples.

**`openwave_sync.py`**
Tolerant frame synchronization: finds the preamble+sync-word via a sliding
Hamming-distance correlator (like the archived flowgraphs'
`digital_correlate_access_code_tag_bb`) instead of requiring an exact byte
match. This is what actually lets the PRN preamble's noise resilience do
anything in this file-based pipeline - see its module docstring. Used via
`openwave_cli.py`'s `--correlate` flag.

**`openwave_modem.py`**
PHY-layer bits <-> symbols mapping: BPSK, QPSK, and 16-QAM today (see
`tasklist.txt` Phase 6 for the eventual multi-mode plan, e.g. 8PSK/OFDM).
Pure Python, zero dependencies. Deliberately has no symbol-rate concept -
see its module docstring for why that's a hard boundary until pulse
shaping.

**`openwave_pulse.py`**
RRC pulse shaping: symbols <-> real timed samples. Needs numpy - the first
stage where a real DSP dependency became unavoidable. `alpha=0.35` and an
11-symbol filter span match the convention used by the archived
flowgraphs' `firdes.root_raised_cosine()` calls.

**`openwave_channel.py`**
AWGN channel simulation - add noise to a sample file, either by direct
`noise_voltage` or a target SNR in dB measured against the file's own
signal power. Sample-level, same reasoning as `openwave_pulse.py` for why
this only makes sense after pulse shaping, not before.

**`openwave_audio.py`**
Audio-subcarrier PHY: symbols <-> real 48 kHz audio on a carrier tone, for
driving a radio's audio port instead of exchanging complex baseband IQ.
The receiver recovers timing, carrier phase and level by correlating
against the frame's known preamble and sync word, then a phase-locked
loop tracks sound-card clock drift. It also gives a data-aided SNR
estimate. Used by `ota_station.py --phy audio`; see "Audio PHY over a
simulated FM radio" below.

**`modulate.py` / `pulseshape.py` / `channel.py` / `matchedfilter.py` / `demodulate.py` / `plot_constellation.py`**
CLI stages built on the modules above, chained together into a full bits ->
symbols -> RRC samples -> (optionally noisy) -> symbols -> bits pipeline
with a visualization step in the middle. See "PHY-Layer Pipeline" below for
the full command sequence, what each stage does, and why matched filtering
is required (not optional) for a clean constellation.

**`openwave_fec.py`**
Forward error correction: convolutional encoding + hard-decision Viterbi
decoding (numpy-vectorized - see its module docstring), fully parameterized
by generator polynomials/constraint length K rather than hardcoded to one
scheme. `PRESETS` holds 13 named configurations (`r1_2_k3` through
`r1_6_k9` - rate 1/N at constraint length K), plus an empirically-measured
"which preset for which SNR" reference table right after `PRESETS`.
`free_distance()`/`find_generators()` are how the presets were chosen
(Dijkstra over the trellis, not hand-picked - see the module docstring for
why that replaced an earlier hand-picked preset that had
silently-truncated, out-of-range generator values). Each FEC block's
coded bits are interleaved (`DEFAULT_INTERLEAVER = 'golden'`; the exact
rule is in the spec's FEC section), so a burst of channel errors such as
an FM fade reaches the decoder as scattered errors it can correct.
`fec_encode.py`/`fec_decode.py` take `--interleaver none|block|golden`
for comparisons; both ends must match.

**`fec_encode.py` / `fec_decode.py`**
CLI wrappers around `openwave_fec.py`: encode a built frame (or a whole
`build_fragmented_frames()` run - every fragment is protected
independently) before `modulate.py`, and decode+correct a possibly-noisy
received frame after `demodulate.py`, using `openwave_sync.py`'s tolerant
correlator to find each fragment's sync (falling back to an unbounded
search and reporting an explicit `gap` status if a fragment's marker didn't
survive the channel at all - see `openwave_sync.py`'s
`FRAGMENT_GAP_SEARCH_BITS` for why this matters: an unbounded-only search
would silently lock onto a LATER fragment instead, with no indication the
one in between was lost). See `-h` on either for full usage; see "FEC +
Full File Transfer Testing" below for how they fit into the full pipeline.

**`openwave_fec_generate_presets.py`**
Regenerates/extends `openwave_fec.py`'s `PRESETS` table via
`find_generators()`'s free-distance search - prints ready-to-paste Python
source, doesn't edit the file for you. Run with `-h` for options (which
rates/K values to search, candidate count, RNG seed).

**`pipeline_test.sh` / `file_xfer_test.sh`**
Turnkey end-to-end test scripts - see "FEC + Full File Transfer Testing"
below.

**`ota_station.py`**
Live, interactive, bidirectional station process - talks to a peer station
either through a simulated over-the-air network link (path loss, fading,
AWGN, latency) or, with `--radio`, over a real FM radio through its audio
interface, pushing real frames through the actual modem chain instead of a
one-shot file pipeline. It speaks either complex IQ (the default) or, with
`--phy audio`, real audio. See "Over-The-Air (OTA) Simulator" below for the
simulator (`fm_test.sh` launches a full FM test setup) and
[RADIO_SETUP.md](RADIO_SETUP.md) for real radios.

**Real-radio support** (all described in [RADIO_SETUP.md](RADIO_SETUP.md)):
`openwave_radio.py` (the radio transport `ota_station.py --radio` uses:
continuous burst detection, queued PTT transmit, half-duplex muting, and an
in-memory `SimulatedAir` for hardware-free tests), `radio_stations.py`
(auto-detects AIOC and Digirig interfaces; `stations.toml` holds per-interface
overrides), `ptt_play.py` (key PTT, play a WAV, release), `level_meter.py`
(live receive-level bar for setting the radio volume), `radio_send.py` /
`radio_listen.py` (one-way burst test tools, one per station),
`radio_link_test.py` (one machine drives both stations over ssh),
`air_loopback_test.py` (two full stations over a simulated air, no
hardware), and `audio_wav_test.py` (generate/decode burst WAV files
offline).

**`openwave_kiss.py` / `openwave_tnc.py`**
OpenWave as a KISS TNC, so standard packet software can use it as its
modem. `openwave_kiss.py` is the KISS framing codec (plain KISS plus the
SMACK and FlexNet CRC variants Linux's `mkiss` driver uses) and AX.25
address helpers. `openwave_tnc.py` serves KISS over TCP and a
pseudo-terminal (for `kissattach`) on top of the station's modem, over the
simulator or a real radio. See "Packet radio and Winlink: OpenWave as a KISS
TNC" below, and [KISS_TNC_SCOPE.md](KISS_TNC_SCOPE.md) for the design and
milestones.

**[MODEM_ARCHITECTURE.md](MODEM_ARCHITECTURE.md)**
Diagrams of the modem: the layers, a burst's on-air layout, the transmit
and receive chains, inside the demodulator (correlation, equalizer, PLL),
the link layer's negotiation and ACK flow, and the modes and carriers.

**Design records**
- [PROTOCOL_V2_PROPOSAL.md](PROTOCOL_V2_PROPOSAL.md): frame format v2,
  accepted and implemented (now in the spec).
- [PAYLOAD_COMPRESSION_PROPOSAL.md](PAYLOAD_COMPRESSION_PROPOSAL.md):
  payload compression with a preset dictionary. On hold, on the local
  `compression-draft` branch only.
- [KISS_TNC_SCOPE.md](KISS_TNC_SCOPE.md): the KISS TNC.

**`legacy/gnuradio/`**
The project's original GNU Radio pi/4-QPSK PHY chain (`phy_sim_loopback.grc`,
`tx_pluto.grc`, `rx_rtlsdr.grc`, and early prototypes) - the initial
testbench OpenWave was built and validated against, archived (not deleted)
once the project repositioned around a pluggable PHY. See
[legacy/gnuradio/README.md](legacy/gnuradio/README.md) for the full
flowgraph walkthrough and how to run its GNU-Radio-dependent tests.

**`tests/`**
Regression suite - run everything with `./run_tests.sh`, or a single module
with `./run_tests.sh tests.test_openwave_link`. All of these are fast,
pure-Python, and need no GNU Radio:
- `test_openwave_link.py` - link layer: header/CRC/ACK/fragmentation
- `test_openwave_modem.py` - bits<->symbols mapping (BPSK/QPSK/16-QAM)
- `test_openwave_pulse.py` - RRC pulse shaping/matched filtering
- `test_openwave_channel.py` - AWGN channel simulation
- `test_openwave_sync.py` - tolerant Hamming-distance sync correlator
- `test_openwave_fec.py` - convolutional encode/Viterbi decode, FEC
  presets, multi-fragment FEC, sync-gap handling

The GNU-Radio-dependent tests (former `test_phy_loopback.py`/
`test_flowgraphs_compile.py`) now live under `legacy/gnuradio/tests/` - see
`legacy/gnuradio/README.md` for how to run them.

**`lorem.txt`**
Generated plain-text test asset for `file_xfer_test.sh`'s `FILE`
argument/`--attach`.

**`../web_siggen/images/colorbar_test.png`, `../web_siggen/images/test_image.png`**
Synthetic (not personal) PNG test assets for `file_xfer_test.sh`'s `FILE`
argument/`--attach`, sized to safely clear the 64-fragment/~64KB ceiling
even after `--attach`'s base64 inflation (~33%). Live in the sibling
`web_siggen` project, not this repo - regenerate with a short PIL/numpy
script if ever needed again at a different size.

## openwave_cli.py - File-Based Link-Layer Testing

Run `python3 openwave_cli.py help` (or with no arguments) for full usage.
Quick reference:

**`send`: build a frame and write it to a file**
```bash
python3 openwave_cli.py send "hello world" -o burst.bin
python3 openwave_cli.py send --json '{"cmd": "ping"}' -o burst.bin
python3 openwave_cli.py send "for w1aw" -o net.bin --src K0MDT --dst W1AW
python3 openwave_cli.py send "long msg..." -o frag.bin --chunk-size 4
```

Negative testing - inject a specific kind of bad frame on purpose:
```bash
python3 openwave_cli.py send "x" -o bad.bin --corrupt-crc
python3 openwave_cli.py send "x" -o bad.bin --corrupt-payload
python3 openwave_cli.py send "x" -o bad.bin --corrupt-header
python3 openwave_cli.py send "x" -o bad.bin --bad-length 9999
```

Send a whole binary file (e.g. a PNG/JPG) as the payload, no text -
exercises fragmentation on real, non-repeating binary content instead of
just text (a good stress test - a several-hundred-KB image needs dozens of
fragments):
```bash
python3 openwave_cli.py send --file photo.png -o img.bin
```

Send a text message WITH a file attached - a JSON envelope
(`{text, filename, attachment_b64}`) over the existing `--json` payload
path, since the link layer only ever sees opaque bytes either way:
```bash
python3 openwave_cli.py send "check this out" --attach photo.png -o msg.bin
```

**`dump`: hex-dump and label every field of every frame in a file**
```bash
python3 openwave_cli.py dump -i burst.bin
```

**`recv`: decode a file through the full LinkReceiver pipeline**
```bash
python3 openwave_cli.py recv -i burst.bin --station-id W1AW
```

Recover a `send --file` binary payload - plain `recv` would corrupt it via
lossy UTF-8 text decoding, so this writes the raw undecoded bytes instead
of printing them:
```bash
python3 openwave_cli.py recv -i img.bin --output-file recovered.png
```

Recover a `send --attach` message: text still prints normally, and
`--save-attachment` extracts+writes the attachment (base64-decoded):
```bash
python3 openwave_cli.py recv -i msg.bin --save-attachment recovered.png
```

None of this touches GNU Radio or any hardware - it exercises
`openwave_link.py` directly, which is useful both for quick sanity-checking
the framing logic and for offline analysis of a real capture (see the
archived `rx_rtlsdr.grc`'s `capture_file` parameter,
[legacy/gnuradio/README.md](legacy/gnuradio/README.md)).

## PHY-Layer Pipeline: Bits to Samples and Back

A file-based, GNU-Radio-free path from a real link-layer frame all the way
down to RRC-shaped baseband samples and back, mirroring what the archived
`tx_pluto.grc`/`rx_rtlsdr.grc` do internally - useful for
testing/understanding the modem in isolation, and a step toward the
eventual GNU-Radio-free stack (see `tasklist.txt`'s "No GNU Radio,
eventually" note under Phase 5).

Requires numpy (`openwave_pulse.py`) and plotly (`plot_constellation.py`
only). Neither is a GNU Radio dependency - if the system Python is
externally managed (PEP 668, e.g. Debian/Ubuntu) and you don't want a venv:
```bash
pip install --user --break-system-packages plotly
```

All intermediate files below go to a scratch directory under `/tmp`, not
the repo, so nothing generated by hand-testing ends up tracked or clutters
`git status`:
```bash
mkdir -p /tmp/openwave-manual && cd /tmp/openwave-manual
```
(Run the `python3 ...` commands below from the repo root as usual - just
point their `-i`/`-o` paths at `/tmp/openwave-manual/...` as shown.)

Full pipeline, one stage at a time:

```bash
# 1. Build a real OpenWave frame (see openwave_cli.py above)
python3 openwave_cli.py send "hello world" -o /tmp/openwave-manual/test.bin --src K0MDT --dst W1AW

# 2. Inspect the frame's fields by hand
python3 openwave_cli.py dump -i /tmp/openwave-manual/test.bin

# 3. Modulate: bits -> BPSK symbols (no sample rate involved yet)
python3 modulate.py -t bpsk -i /tmp/openwave-manual/test.bin -o /tmp/openwave-manual/symbols.cfile

# 4. Pulse-shape: symbols -> oversampled RRC baseband samples
python3 pulseshape.py -i /tmp/openwave-manual/symbols.cfile -o /tmp/openwave-manual/shaped.cfile

# 5. Plot BEFORE matched filtering - expect a smeared line, not clean
#    points (a single RRC filter alone is NOT zero-ISI)
python3 plot_constellation.py -i /tmp/openwave-manual/shaped.cfile -o /tmp/openwave-manual/before_mf.html \
    --title "before matched filter"

# 6. (Optional) simulate a noisy channel - insert here, between pulse
#    shaping and the matched filter (noise is a sample-level thing).
#    Skip this step entirely for a clean round trip.
python3 channel.py -i /tmp/openwave-manual/shaped.cfile -o /tmp/openwave-manual/noisy.cfile --snr-db 10
#    (or: --noise-voltage 0.05 for a direct value instead of target SNR)

# 7. Matched filter + decimate: back down to one sample/symbol.
#    Input is noisy.cfile if you did step 6, shaped.cfile if you skipped it.
python3 matchedfilter.py -i /tmp/openwave-manual/noisy.cfile -o /tmp/openwave-manual/recovered_symbols.cfile

# 8. Plot AFTER matched filtering - expect two clean dots at +/-1.0 with
#    no noise, or two fuzzy clusters around +/-1.0 with noise added
#    (TX RRC + RX RRC in cascade = a full raised-cosine response,
#    which IS zero-ISI at the correct symbol-spaced sampling instant)
python3 plot_constellation.py -i /tmp/openwave-manual/recovered_symbols.cfile -o /tmp/openwave-manual/after_mf.html \
    --title "after matched filter"

# 9. Demodulate: symbols -> bits/bytes
python3 demodulate.py -t bpsk -i /tmp/openwave-manual/recovered_symbols.cfile -o /tmp/openwave-manual/out.bin

# 10. Verify the round trip reproduced the original frame exactly
#     (expect this to fail at low enough SNR - that's the point)
cmp /tmp/openwave-manual/test.bin /tmp/openwave-manual/out.bin && echo "IDENTICAL"

# 11. Confirm it still decodes correctly through the link layer
python3 openwave_cli.py recv -i /tmp/openwave-manual/out.bin
```

Notes:
- `openwave_cli.py`'s `dump`/`recv` only understand byte-level frame files
  (`test.bin`/`out.bin`) - never point them at a `.cfile` (raw complex
  samples).
- Steps 3+9 alone (skip the rest) also round-trip correctly, if you just
  want to test the bits<->symbols mapping without pulse shaping.
- `--alpha`/`--sps`/`--span` on `pulseshape.py` and `matchedfilter.py` must
  match between the two (`matchedfilter.py` can't know what the sender
  used - see each script's `-h` for defaults, currently `alpha=0.35`,
  `sps=4`, `span=11`).
- `plot_constellation.py` writes a self-contained interactive HTML file
  (zoom/pan/hover for exact I/Q values) - open it in any browser, no
  display/X11 forwarding required on a remote box. It deliberately uses
  plain SVG rendering (Plotly's `go.Scatter`, not `go.Scattergl`) rather
  than WebGL, so it works even in browsers/VMs without WebGL support.
- By default, `openwave_cli.py recv`/`dump` use exact byte matching to find
  a frame (`openwave_link.iter_frames()`/`describe_frames()`) - a single
  bit error anywhere in the 36-byte preamble+sync-word breaks that match
  completely, so at low SNR you'll see `OK:0` with no CRC-fail count
  either (frame not found at all), not a rising CRC-fail count. Pass
  `--correlate` to use `openwave_sync.py`'s tolerant Hamming-distance
  correlator instead (matching what the archived flowgraphs'
  `digital_correlate_access_code_tag_bb` did) - it finds frames the
  exact-match path misses entirely, informally down to roughly ~2dB SNR
  for BPSK at these filter settings before header/payload bit errors start
  causing CRC failures even once sync is found - see "FEC + Full File
  Transfer Testing" below for how FEC pushes that floor much lower.
  Example:
  ```bash
  python3 channel.py -i /tmp/openwave-manual/shaped.cfile -o /tmp/openwave-manual/noisy.cfile --snr-db 1.5 --seed 4
  python3 matchedfilter.py -i /tmp/openwave-manual/noisy.cfile -o /tmp/openwave-manual/recovered_symbols.cfile
  python3 demodulate.py -t bpsk -i /tmp/openwave-manual/recovered_symbols.cfile -o /tmp/openwave-manual/out.bin
  python3 openwave_cli.py recv -i /tmp/openwave-manual/out.bin              # OK:0 - not found
  python3 openwave_cli.py recv -i /tmp/openwave-manual/out.bin --correlate  # OK:1 - decoded
  ```

## FEC + Full File Transfer Testing

Everything above tests the modem chain one stage at a time by hand. FEC
(`openwave_fec.py`) sits between send/fec_encode and demodulate/fec_decode
in that same chain, and `pipeline_test.sh` / `file_xfer_test.sh` wrap the
*whole* thing - frame build through FEC/modem/noise/FEC-decode back to a
verified round trip - into single commands, so you don't have to chain
seven scripts by hand every time.

`pipeline_test.sh` - runs one fixed synthetic message ("hello world", sent
`K0MDT -> W1AW`) through every stage of the full pipeline in order, printing
each stage's own output as it goes rather than just a final pass/fail. Use
this to see *how* the pipeline works or to debug a specific stage - for
real payloads and pass/fail sweeps across SNR/FEC combinations, use
`file_xfer_test.sh` instead (below).

What it actually runs, stage by stage:
1. `openwave_cli.py send` builds the frame (`--mode` sets its Waveform
   Mode ID - header metadata only, does not itself select the modulation
   used in step 3; see `--modtype` below), then `openwave_cli.py dump`
   hex-dumps it.
2. *(only with `--fec`)* `fec_encode.py` encodes the frame with the given
   preset; prints the byte overhead added (e.g. `+66.7%`) since that
   bandwidth-for-resilience cost is the whole point of `--fec`.
3. `modulate.py` (bits -> symbols, `--modtype` picks `bpsk`/`qpsk`) then
   `pulseshape.py` (symbols -> RRC baseband samples), then a "before
   matched filter" constellation plot.
4. *(only with `--snr`)* `channel.py` injects AWGN at that SNR between
   pulse shaping and the matched filter. Omit `--snr` for a clean round
   trip with no channel step at all.
5. `matchedfilter.py` (samples -> symbols) then an "after matched filter"
   constellation plot, then `demodulate.py` (symbols -> bits).
6. *(only with `--fec`)* `fec_decode.py` Viterbi-decodes and error-corrects
   the frame, using the tolerant sync correlator internally to find it.
7. Verification: without `--fec`, `cmp`s the recovered bytes against the
   original and prints `IDENTICAL` on an exact match (fec_decode.py instead
   rebuilds a fresh canonical frame on success, so this byte-for-byte check
   only applies to the uncoded path). Finally `openwave_cli.py recv`
   confirms the link layer itself decodes it - when noisy and uncoded, this
   step runs *twice*, once with plain exact-byte-match sync and once with
   `--correlate`, so the tolerant correlator's benefit is visible in every
   noisy run, not just implied.

Every run gets its own `mktemp -d` working directory under `/tmp` (never
the repo), labeled with its mode/snr/fec/modtype so concurrent runs are
both collision-safe and easy to tell apart in `ls /tmp` - the script prints
that "Working dir:" path and a "Total time:" line at the end.

```bash
./pipeline_test.sh                                     # clean, no FEC
./pipeline_test.sh --snr -6                             # noisy, no FEC (expect failure)
./pipeline_test.sh --snr -6 --fec r1_3_k7               # noisy, FEC-protected
./pipeline_test.sh --snr 5 --mode 2 --fec r1_2_k7        # a specific Waveform Mode ID
./pipeline_test.sh --snr -6 --fec r1_3_k7 --modtype qpsk # QPSK instead of BPSK
./pipeline_test.sh -h                                   # full flag reference
```

`file_xfer_test.sh` - a real message and/or file (e.g. `lorem.txt`, or
`../web_siggen/images/colorbar_test.png`) through send/`--attach` -> the
same pipeline -> recv, with pass/fail verification (message text match,
byte-identical attachment) and transmitted-size/timing reporting. Use this
for realistic payloads and for sweeping SNR/FEC combinations.
```bash
./file_xfer_test.sh lorem.txt --message "check this out"
./file_xfer_test.sh ../web_siggen/images/colorbar_test.png --snr -6 --fec r1_3_k7
./file_xfer_test.sh -h                                  # full flag reference
```

Both scripts write every intermediate file (frames, symbols, plots,
recovered attachment) to a fresh `mktemp -d` directory under `/tmp` - never
to the repo - named like
`/tmp/openwave_file_xfer_test.mode<M>_snr<SNR>_fec<PRESET>.<random>`, so
several runs (including different configs) can run at once without
clobbering each other, and each run prints its own "Working dir:" line so
you can go find the plots/files afterward.

Choosing a preset: FEC is a bandwidth-for-resilience trade - stronger
presets (higher constraint length K, lower rate 1/N) correct more errors
but cost more transmitted bytes and more Viterbi decode time (state count
scales as `2**(K-1)`). Don't reach for a stronger preset than your SNR
actually needs - see the empirically-measured SNR-to-preset table in
`openwave_fec.py` right after `PRESETS` for concrete measured pass rates
(e.g. `r1_2_k3` is already 100% reliable at 3dB; -6dB needs at least
`r1_4_k9`). Sweep your own SNR/preset combination with a shell loop over
`file_xfer_test.sh`'s exit code (0 = pass) - see
`openwave_fec_generate_presets.py` if you need a preset that isn't in the
table at all.

Sync gaps: if a fragment's marker doesn't survive the noise,
`fec_decode.py` (and `openwave_cli.py recv --correlate`, for the non-FEC
path) reports an explicit `gap` status for it rather than silently
skipping straight to the next fragment - see `openwave_sync.py`'s
`FRAGMENT_GAP_SEARCH_BITS`. A lost fragment's data is still unrecoverable
either way (that's real information loss, not a bookkeeping problem), but
a gap is now always visible in `fec_decode.py`'s per-fragment status
output instead of an unexplained "attachment doesn't match" at the end.

## Processing Gain: How a Weak Signal Is Still Recovered

A single received symbol can be badly corrupted by noise, but the
receiver never decides anything from a single sample or a single bit. It
gathers a signal's energy from many places that are all known to belong
together, and the noise in those places doesn't line up the way the
signal does. Combining N samples, symbols or coded bits adds the signal
up N times over, but the noise only about sqrt(N) times. That improvement
is the processing gain, 10*log10(N) dB. OpenWave stacks several stages
of it:

- **The matched filter** combines the samples within each symbol. With
  4 samples per symbol on the IQ path that's 6 dB.
- **FEC** sends each information bit as N coded bits at rate 1/N. The
  Viterbi decoder weighs all of them, plus the code's structure across
  neighbouring bits. r1_4_k9 gives each bit 4 times the energy (6 dB)
  before the coding gain itself.
- **Finding the frame** correlates against the known preamble and sync
  word all at once: 288 symbols on the IQ path (about 24.6 dB of gain), and
  160 at the audio path's 600 baud (its 16-byte preamble, about 22 dB). So
  the receiver
  locks on even when individual bits are coming out wrong.

The same idea, seen from the bandwidth side: what counts is energy per
information bit, Eb/N0 = SNR + 10*log10(B / bit rate). Sending fewer bits
per second through the same bandwidth B gives each bit more energy.

A worked example: mode 0 with r1_4_k9 carries 150 bit/s of information
through the mic port's ~2.7 kHz passband, a ratio of 18, or 12.6 dB. If
the audio arriving from the radio had 0 dB SNR across that whole
passband, each information bit would still arrive at about 12.6 dB
Eb/N0. The decoder needs about 4-6 dB in our measurements. Mode 2 with
r1_2_k7 (1200 bit/s) has only 3.5 dB of that headroom. That's why the
slow, strongly coded modes survive conditions the fast ones can't, and
why `--auto` steps down to them as the link degrades.

Measured examples:

| Situation | Result |
|---|---|
| IQ path, BPSK + r1_4_k9, at -6 dB SNR per sample (noise louder than the signal) | 7.7% of raw bits wrong, but 100% of frames decoded |
| Same, at -8 dB per sample | ~13% raw bit errors, 88% of frames decoded |
| FM mic port at 8 dB CNR (audio SINAD ~14.5 dB) | every mode 0-2 delivered 100% |
| FM bursts carrying 114-286 FM threshold "clicks" each (15 dB CNR, 80 km/h fading) | all decoded first time |
| A 30% (IQ bits) or 40% (audio) wiped-out marker | frame still decoded, because the correlation finds it anyway |

Two limits on all this:

- **Gain against burst errors needs interleaving.** Processing gain
  assumes the noise is spread out. A fade that knocks out a run of
  consecutive bits concentrates the damage where the code can't reach
  it. The interleaver spreads those bursts back out: under mobile fading,
  mode 2 at 15 dB CNR went from 38% of frames delivered to 100%.
- **Over FM there is a hard floor before the modem ever sees the
  signal.** An FM receiver's discriminator stops working below roughly
  6-10 dB carrier-to-noise. The audio falls apart into clicks and noise,
  and the squelch closes (about 6 dB here). Processing gain applies to
  the audio that comes out of the radio, so it can't recover a signal the
  radio itself has lost. On FM, OpenWave's gain goes into surviving
  fades, clicks and interference near that floor, not into going below
  it. Getting far below the noise, as FT8 or WSPR do, needs an SSB
  (linear) radio path.

## Over the air with real radios

`ota_station.py --radio` runs the same live station over real FM radios, one
per computer, with no simulator and no link between the computers except the
radio. Verified with an HT on an AIOC and an HT on a Digirig, in both
directions. The short version, on each computer (full details, the audio
setup and leveling, results and troubleshooting are in
[RADIO_SETUP.md](RADIO_SETUP.md)):

```
./radio_stations.py       # is the AIOC / Digirig detected?
./level_meter.py          # open squelch, turn the radio volume until it says "good"
./ota_station.py --station-id K0MDT-1 --dst K0MDT-2 \
    --phy audio --audio-port mic --radio --ack --auto
```

On the other computer swap the two callsigns. This transmits: use a clear
frequency and low power. Each run writes a session log to `logs/`.

## Packet radio and Winlink: OpenWave as a KISS TNC

`openwave_tnc.py` makes OpenWave a **KISS TNC**: the modem that packet
software expects underneath it. Linux's AX.25 stack talks to it through
`kissattach`, and through that stack Pat (Winlink client, including
peer-to-peer), digipeating and the Linux RMS Gateway can all run over
OpenWave's modem (FEC, interleaving, audio PHY) instead of 1200-baud AFSK.
The TNC is just a modem: AX.25 does the addressing, connections and
retries, so OpenWave's own chat link layer isn't used.

```
python3 -m ota_sim.simulator --channel fm --port mic --base-snr-db 15
./openwave_tnc.py --callsign K0MDT-1 --sim-host 127.0.0.1 --mode 2 --pty-link ~/kiss-a
./openwave_tnc.py --callsign K0MDT-2 --sim-host 127.0.0.1 --mode 2 --pty-link ~/kiss-b
sudo kissattach ~/kiss-a owa      # ports owa/owb defined in /etc/ax25/axports
sudo kissattach ~/kiss-b owb
```

Use `--radio` instead of `--sim-host` on a real radio. Both TNCs must use
the same `--mode` and `--fec-preset`. KISS is also served over TCP
(`--kiss-port`, default 8001).

**Status:** Milestone 1 is done. Linux's AX.25 stack runs unmodified over
two TNCs and the simulated FM link: `beacon` frames and full
`axcall`/`ax25d` connected sessions, in every KISS framing the kernel uses.
**Pat peer-to-peer works over the air (2026-10-07):** a Winlink message
from K0MDT-1 to K0MDT-2 over two HTs in 42 s (18.6 s over the simulator).
The setup for laptops and a Raspberry Pi is in
[RADIO_SETUP.md](RADIO_SETUP.md#winlink-peer-to-peer-pat-over-openwave).
[KISS_TNC_SCOPE.md](KISS_TNC_SCOPE.md) has the setup (including
`axports`/`ax25d` configuration), the test notes and the remaining
milestones (digipeating, a gateway).

## Over-The-Air (OTA) Simulator

Everything above is a one-shot, file-based, single-process pipeline.
`ota_station.py` is different: it's a live, interactive process that
builds/parses real OpenWave frames and pushes them through the actual
modem chain (modulate -> pulse-shape on TX, matched-filter -> demodulate
-> sync/FEC on RX) to/from a peer station, in real time - type a line,
press Enter, it's framed, modulated, and sent; incoming decoded messages
from the peer print as they arrive.

The link between two stations is relayed through `ota_sim` - a small,
separate, protocol-agnostic network simulator (`../ota_sim`, alongside
this repo) that models path loss, fading, AWGN, and configurable latency
between two peers talking in raw IQ samples. With `--channel fm`, it
instead models a pair of FM transceivers carrying audio between their
audio ports (see "Audio PHY over a simulated FM radio" below). `ota_sim` has no
OpenWave-specific code at all - it only relays whatever bytes a peer sends
it, so it's reusable by any project that needs two simulated radios
talking to each other, not just this one. See `../ota_sim/README.md` for
the simulator itself.

### Setup

```bash
pip install -e ../ota_sim --break-system-packages   # once, from this repo
```

### Running it

Three processes make up a full simulated link: the `ota_sim` simulator,
and two `ota_station.py` stations. All three can run on one machine, or be
split across two or three separate machines - each process just needs to
be told where the others are reachable over the network.

**All on one machine** (three terminals):
```bash
python3 -m ota_sim.simulator --distance-km 1 --base-snr-db 15 --latency-ms 50

python3 ota_station.py --station-id K0MDT --dst W1AW --sim-host 127.0.0.1

python3 ota_station.py --station-id W1AW --dst K0MDT --sim-host 127.0.0.1
```
Type a message in either station's terminal and press Enter - it should
appear, decoded, in the other station's terminal shortly after (delayed by
the simulator's configured latency).

Add `--auto --ack` to both stations instead of matching `--mode`/`--fec` by
hand - each negotiates its own mode/FEC the first time *it* has a message
to send (not eagerly at startup - see `--auto` below), from the other's
measured link quality.

`--base-snr-db` is the SNR at `--reference-distance-km` (default 1.0, so
the command above gives a clean ~15dB link). **`--distance-km` further
than that costs real path-loss dB** - e.g. 5km against a 1km reference
costs ~14dB, so `--distance-km 5 --base-snr-db 15` only nets ~1dB, too
weak for uncoded BPSK to reliably decode (most bursts will fail CRC or
sync entirely - watch for `[rx]` lines on the receiving station, or the
simulator's own startup line, which always prints the resulting
`nominal_snr`). Either keep `--distance-km` close to
`--reference-distance-km`, raise `--base-snr-db` to compensate, or add
`--fec` on both stations.

**Two machines**: run the simulator alongside one station on machine 1,
and the other station on machine 2 with `--sim-host <machine-1's address>`.

**Three machines**: run the simulator on its own machine; each station
points `--sim-host` at it, from wherever it happens to be running.

Each station connects to the simulator over TCP (`--sim-host`/`--sim-port`)
and identifies itself by callsign as soon as it connects, so the simulator
needs no address configuration beyond its own
`--listen-host`/`--listen-port`. Only the simulator's port has to be
reachable through firewalls.

### Options

Run `python3 ota_station.py -h` for the full reference, including a
table of the Waveform Modes and FEC presets available. The audio-PHY flags
(`--phy`, `--audio-port`, `--lead-in-ms`) are covered in "Audio PHY over
a simulated FM radio" below. Key flags:

`--mode N` (0-5) selects a Waveform Mode ID - modulation and symbol rate
per `openwave_link.py`'s `WAVEFORM_MODES` (0/1 BPSK, 2/3/4 QPSK, 5 16-QAM,
each step roughly doubling raw throughput). `--fec` FEC-encodes/decodes
using `--fec-preset` (default: `r1_4_k7`, independent of the mode's own
documented default preset - see `openwave_fec.py`'s `PRESETS` for the
full list). `--ack` requests ACKs and retries on timeout, **per fragment**
(stop-and-wait): a multi-fragment message sends one fragment, waits for
its own ACK (retrying just that fragment on timeout), then sends the
next - so a single lost fragment only costs *that fragment* a retry, not
the whole message. `--verbose` prints each fragment's outcome as it
happens; either way, a final line reports the whole message
(`[ack] delivered - N fragment(s) acknowledged by <peer> (mode M/modulation,
fec-preset)` - the mode/FEC actually used for that send, useful since
negotiation is asymmetric and otherwise not obvious from this line alone -
or `[ack] gave up on fragment i/N - message incomplete` if one fragment
truly never gets through). This costs one round trip of latency per
fragment (more fragments = slower, not less reliable) rather than the
old whole-message-retry behavior, where a single lost fragment doomed the
*entire* multi-attempt send. Without `--ack`, there's no delivery
confirmation at all - that's inherent to the protocol (no ACK requested,
none sent back), not a missing feature. If a fragment's own ACK gets lost
on the way back (a real, expected channel event, not a bug - the
fragment itself was fine), the sender retries just that fragment - the
receiver recognizes the retransmit, re-acks it, and does not re-deliver
or corrupt the in-progress reassembly. **Without `--auto`, both stations in
a session must be launched with the same `--mode`/`--fec`/`--fec-preset`** -
the receiver has to already know which modulation to demodulate with.

`--auto` negotiates mode/FEC automatically instead of requiring matching
flags on both ends, over a fixed, always-robust control channel (BPSK +
`r1_2_k9`) distinct from whatever data mode ends up negotiated. It's
**listen-first, not eager**: a station with `--auto` transmits nothing on
its own at startup and just listens - negotiation only happens once THIS
station actually has a message to send, triggered lazily by its own first
`send_text()` call (typing a line and pressing Enter).

Negotiation is a two-phase handshake, and the **initiator's own
measurement decides the mode for both directions** - not two independent
opinions. A station can only ever measure what arrives *at* it, never
what it sends, so: (1) the initiator (whichever station's operator typed
first) probes the peer, purely to get something back to measure; (2)
having now measured its own reception of that reply (a blind EVM-style
estimate - no pilot symbols, computed straight off the received symbols),
it picks the best mode/FEC from `MODE_SNR_TABLE` (`openwave_link.py`) and
authoritatively tells the peer to use it too, waiting for confirmation
before either side trusts the link is in sync. A station that never
initiates (never had its own message to send first) never negotiates on
its own - it just adopts whatever the initiator sets, and considers the
session already negotiated from then on. Implies FEC is in play even
without `--fec`. Negotiated **once** per session (attempted by the
initiator regardless of outcome) - not continuous mid-session adaptation
(a bad turn partway through a session won't trigger a re-probe automatically).
Known gap: if both stations get their own first message to send within the
same handshake window, both may start as their own initiator concurrently
- not handled.

Since it's not automatic, several commands are available at the
interactive prompt (any line starting with `/`, instead of a chat
message):

- **`/renegotiate`** - re-runs the full handshake right now, regardless of
  `--auto` or whether this station already negotiated once. Useful after
  changing the simulator's channel settings mid-session.
- **`/modes`** - lists the Waveform Modes table (same as `-h`'s epilog),
  so you can see what's available without restarting.
- **`/mode N`** - manually switches to Waveform Mode `N` right now, no
  restart needed - and tells the peer, the same confirmed `MODE_SET`/
  `MODE_SET_ACK` exchange `--auto` itself uses, so both stations actually
  end up on the mode you picked, not just this one. Bare `/mode` (no
  number) reports the current mode/FEC instead of changing anything.
  Counts as already-negotiated, so `--auto`'s lazy first-send trigger
  won't immediately override a manual choice made before your first
  message. Rejected if `N`'s symbol rate is over the current
  `--bandwidth narrow` ceiling - narrow is a hard limit on what the
  deployment can carry, not just a hint for automatic selection, so it
  can't be bypassed by hand either.
- **`/fec PRESET`** - same idea, but for the FEC preset alone (keeps the
  current mode) - see `openwave_fec.py`'s `PRESETS` for the list, or run
  `/fec` with no argument to see the current preset and the full list.
  Turns FEC on if this station was started without `--fec`/`--auto` -
  otherwise the preset you just set would silently go unused.
- **`/bandwidth [narrow|wide]`** - reports or changes the `--bandwidth`
  ceiling live, without restarting. Unlike `/mode`/`/fec`, this doesn't
  touch the currently active mode or send anything to the peer by itself
  - it only affects *future* automatic selection, so follow it with
  `/renegotiate` (or `/mode`) to actually apply it.
- **`/ack [on|off]`**, **`/auto [on|off]`**, **`/lead-in [MS]`** - change
  this station's `--ack`, `--auto` and `--lead-in-ms` live (no argument
  shows the current value):
  - `/ack` only affects this station's own sends; a receiver always
    acknowledges frames that ask for it.
  - `/auto on` re-arms negotiation for the next send, even if the
    session already negotiated.
  - `/lead-in` applies to `--phy audio` only.
- **`/help`** (or `/?`) - prints the full command reference.
- **`/quit`** (or `/exit`) - closes this station cleanly, same as EOF/Ctrl+C
  (still prints the session summary on the way out).

Up/down-arrow command history and basic line editing work at the prompt
(both for chat messages and `/` commands) - the standard Python
`readline` module is imported for exactly this.
- **`/ping`** - a quick round-trip timing/quality check at whatever mode
  is currently active: 5 ping/pong round trips, reporting success count,
  RTT range and average, measured SNR *and* EVM% (the same blind
  estimate `estimate_snr_db()` already makes, just also expressed in the
  conventional RF unit - see its comment in `ota_station.py` for the
  direct SNR-to-EVM conversion), on-air burst size at the current mode,
  and - if any trials failed - a breakdown of *why*: `no_reply` (nothing
  came back at all) is tracked separately from a reply that arrived but
  couldn't be used (`no_sync`, or a decode failure like `crc_fail`).
- **`/ping survey`** (or `/ping --survey`) - really tests *every* waveform
  mode this station is willing to use against the peer, one at a time -
  not a prediction from a single measurement, actual transmissions at
  each mode (reusing the same confirmed `MODE_SET`/`MODE_SET_ACK`
  mechanism `--auto` itself uses to switch modes, just to an explicitly
  chosen mode each time instead of one picked from `MODE_SNR_TABLE`).
  Respects `--bandwidth`: with `narrow`, only modes at or below the
  ceiling get tested at all - unlike `--auto` (which just declines to
  *pick* a mode above the ceiling), there's no point spending several
  seconds surveying a mode this deployment can't actually use. Takes
  several seconds; restores the original mode when done, and finishes
  with a consolidated summary of every mode's results together plus a
  pick for the best *fully*-reliable one (highest throughput with zero
  dropped pings) - the per-mode rows printed live while it runs scroll
  away, so this is the look-back-at-everything-at-once view. Don't send a
  real chat message while a survey is running - the active mode is being
  changed underneath it and a message would land using the wrong one.
`MODE_SNR_TABLE`'s BPSK (modes 0/1) and QPSK (modes 2/3/4, all one
reliability tier - see below) thresholds are grounded in real round-trip
reliability measurements (see its comment in `openwave_link.py`). The
16-QAM (mode 5) threshold is deliberately conservative for the same
reason: under real fading, the control probe's own SNR reading is noisy
enough that it can't reliably distinguish a link where 16-QAM will
actually hold up from one where it won't - so 16-QAM now only gets picked
on genuinely excellent links (~21dB+), trading away some throughput for
not repeating a real failure mode this table used to have (`--auto`
picking 16-QAM, the message actually arriving but its ACK failing to get
back, so the sender reports failure despite delivery succeeding).

`--bandwidth {narrow,wide}` (default `wide`) caps what `--auto` is
willing to pick, independent of SNR - `narrow` limits selection to modes
at or below 1200 sym/s (`NARROWBAND_MAX_SYMBOL_RATE`, `ota_station.py`),
for a link that physically can't carry a faster symbol rate no matter how
good its measured SNR is (e.g. a narrowband/audio-bandwidth channel) -
something this simulator's own channel model has no concept of on its
own. `MODE_SNR_TABLE` has one row per throughput tier within the QPSK
family specifically so a narrow ceiling still lands on the best QPSK mode
that fits (mode 2, 1200 sym/s) instead of falling through past QPSK
entirely. Doesn't affect manual `--mode`/`--fec` or `/ping survey`, only
what `--auto`/`/renegotiate` will choose automatically.

`--verbose`/`-v` prints per-message stats as they happen: on send, text
size/fragment count/on-air byte count; on `--ack` delivery, round-trip
time and text-level throughput; on receive, a short `[stats]` block -
```
[stats]
  frames:  1 ok
  session: seq 1, mode 3 (qpsk), K0MDT -> W1AW
```
(zero-value counters omitted, unlike the single dense line
`LinkReceiver.stats_line()` itself prints - that's still what
`openwave_cli.py` uses, unchanged). Per-burst `[rx] SNR ~XdB (modulation)`
lines (a blind EVM-style estimate off whatever actually decoded the
burst) also print under `--verbose` for every received burst, not just
during negotiation. A session summary - messages/bytes sent and received
(both text-level and actual on-air bytes), elapsed time, session-average
throughput, and a compact link-total line - always prints on exit (Ctrl+C
or EOF) regardless of `--verbose`, so you get the totals even from a
quiet run:
```
[K0MDT] session summary (4.8s):
  sent      1 message, 65 B text -> 77.22 KB on-air (108 bps)
  received  0 messages
  link      0 ok, 1 acks rx
```

**FEC preset vs. modulation is a real size tradeoff, not just a dial**:
a stronger preset (lower rate, e.g. `r1_4_k7`) multiplies the coded
frame's size, and a lower-order modulation (BPSK) needs more symbols -
and therefore more samples - per coded byte than QPSK/16-QAM. A single
`--mode 0` + `r1_4_k9` burst at the full `MAX_PAYLOAD_LEN` is several
hundred KB of samples. The relay is TCP, so there's no per-burst size
ceiling: every message fragments only at the protocol's own
`MAX_PAYLOAD_LEN`, whatever the mode or FEC. (The relay used to be UDP,
whose ~65KB datagram limit cut the default `--mode 0` + `r1_4_k7` combo
down to 5 characters per burst.)

`ota_sim.simulator`'s channel knobs (`--distance-km`, `--freq-mhz`,
`--base-snr-db`, `--no-fading`, `--drop-prob`, `--latency-ms`,
`--jitter-ms`, `--seed`) are documented in `../ota_sim/README.md` and
`python3 -m ota_sim.simulator -h`. Its `--channel fm` mode (an FM
transceiver audio-path model) carries audio rather than IQ - see the next
section.
A tighter/noisier link is a good way to see the real link layer at work:
higher `--drop-prob` or lower `--base-snr-db` should never produce garbled
text (CRC-verified frames only), just gaps or delivery failures - the same
"solid link layer" property `tests/test_ota_station.py` and the archived
`test_phy_loopback.py`'s noisy-channel test both check for.

### Audio PHY over a simulated FM radio (`--phy audio`)

The real deployment target is VHF/UHF FM transceivers driven through
their audio ports. `--phy audio` makes a station send real 48 kHz audio
instead of IQ:

- **TX:** each burst is RRC-shaped symbols on a carrier tone inside the
  radio's passband (`openwave_audio.py`), preceded by a lead-in of silence
  (`--lead-in-ms`, default 250) that covers transmitter key-up and the far
  radio's squelch opening. Frames go out without `build_frame()`'s 24+24
  bytes of zero padding, which only the IQ path needs, and with a 16-byte
  preamble instead of 32 (`openwave_audio.frame_for_audio()`). With frame
  format v2's 20-byte header, a mode 0 ACK takes 1.9 s (it was 3.4 s).
- **The channel:** pair it with `ota_sim`'s `--channel fm`, which models
  the two radios' whole audio-to-audio path: filters, emphasis, deviation
  limiter, FM threshold and clicks, fading, squelch, sound-card clock
  offset, and half-duplex airtime.

```bash
python3 -m ota_sim.simulator --channel fm --port mic --base-snr-db 15

python3 ota_station.py --station-id K0MDT --dst W1AW --sim-host 127.0.0.1 \
    --phy audio --audio-port mic --auto --ack
python3 ota_station.py --station-id W1AW --dst K0MDT --sim-host 127.0.0.1 \
    --phy audio --audio-port mic --auto --ack
```

Or let `./fm_test.sh` open all three in their own terminal windows, with
matching settings. For example, `./fm_test.sh --port data --cnr 10` or
`./fm_test.sh --fading rayleigh --speed 80 --freq-mhz 440`; see
`./fm_test.sh --help`.

Once it's running, the channel can be changed without restarting
anything: type commands like `/cnr 10`, `/fading rayleigh 80` or `/show`
into the simulator's window (see `../ota_sim/README.md`).

**Both stations and the simulator must agree.** Use the same port on all
three, and `--phy audio` with `--channel fm` (or `iq` with `iq`). Nothing
on the wire says which format a burst is in.

`--audio-port` picks the radio connection, and is a **hard** ceiling on
which modes can be used (unlike `--bandwidth`, which only steers
`--auto`):

| Port | Carrier | Max symbol rate | Usable modes |
|---|---|---|---|
| `mic` (speaker/mic, 300-3000 Hz, emphasis) | 1650 Hz (2000 Hz for mode 3) | 2400 | 0, 1, 2, 3 |
| `data` (6-pin data port, flat to ~5 kHz) | 2000 Hz | 2400 | 0, 1, 2, 3 |

Modes 4 and 5 (4800/9600 sym/s) don't fit any FM radio's audio passband.
Mode 3 on the mic port overhangs the nominal 300-3000 Hz, but delivered
5/5 each way on the air through two HTs (2026-10-07; RADIO_SETUP.md
"Frequency response and faster modes"), so the mic port allows it and
`--auto` picks it when the link is good enough (a 600 baud probe at about
15 dB or more).

**Measured through the FM model** (narrow channel, r1_2_k7, 10 bursts
per point, % delivered):

| Port | Mode | Airtime | 30 dB CNR | 15 dB | 10 dB | 8 dB |
|---|---|---|---|---|---|---|
| mic | 0, 1, 2 | 5.0 / 2.6 / 1.5 s | 100% | 100% | 100% | 100% |
| data | 0, 1 | 5.0 / 2.6 s | 100% | 100% | 100% | 100% |
| data | 2 | 1.5 s | 100% | 100% | 100% | 60% |
| data | 3 | 0.9 s | 100% | 100% | 30% | 0% |

Below about 6 dB CNR the radio's squelch closes, so nothing gets through
on either port. The mic path holds up better at the fringe than the data
port, because de-emphasis and the voice band filter out much of FM's
high-frequency noise. The data port's advantage is mode 3's extra speed
on a strong link.

The receiver recovers each burst's timing, carrier phase and level by
correlating against the frame's known preamble and sync word, then
decodes the frame at exactly that position. It doesn't re-find the
marker in demodulated bits, where a fade across it could fail an
otherwise decodable frame. That also
resolves QPSK's phase ambiguity and gives `--auto` a data-aided SNR
estimate. A phase-locked loop then tracks the sound cards' clock offset
across the burst; without it, about 20 ppm breaks a multi-second QPSK
burst. With it, tested up to 300 ppm.

`--auto` uses the same `MODE_SNR_TABLE` as the IQ path: the data-aided
estimate measures the received symbols' SNR, the same quantity the
table's thresholds are about. **Unless the link is fading.** Mobile
fading on FM concentrates the noise into short dropouts below the FM
threshold, many per burst. The weak r1_2_k3 code the table uses can't
repair those, so a link that averages a healthy SNR can still lose most
bursts.

To catch this:

1. The receiver rebuilds each CRC-verified probe frame exactly, so it has
   the error on every symbol.
2. If more than 3% of the burst's 5 ms stretches sit 6 dB or more below
   its median, the link is treated as fading.
3. `--auto` then picks from `FADING_MODE_SNR_TABLE` instead: K=7/K=9
   codes, chosen by the probe's whole-burst average SNR.

The negotiation log shows it, e.g. `measured our own link at ~12.0dB
(fading: 17% of the burst in dropouts, ...)`.

This works together with the FEC interleaver, which turns each fade's
burst of errors into scattered ones. At 15 dB CNR, 80 km/h on 440 MHz,
mode 2 went from about 6% of bursts delivered (r1_2_k3, no interleaving)
to 100% (r1_2_k7, interleaved). Fading-table rows, measured at 30 and
80 km/h with interleaving:

| Probe burst average SNR | Pick | Delivered |
|---|---|---|
| below 7 dB | mode 0 / r1_4_k9 | 100% down to 8 dB CNR |
| 7-9.5 dB | mode 1 / r1_2_k7 | 100% |
| 9.5 dB and up | mode 2 / r1_2_k7 | 100% |

That's about 660 bps of goodput, against 240 bps for the
pre-interleaving fading table at the same 100% delivery.

Bursts now take real airtime: seconds per burst at modes 0-1. The ACK,
`/ping` and negotiation reply timeouts stretch by twice the last burst's
airtime, so a station waits for the reply to finish airing instead of
retrying into it on the half-duplex channel.

Not yet handled on the audio path:

- **Slow fading (walking speed).** Fades last ~100 ms, long enough to
  wipe out large parts of a burst even with interleaving: about half of
  bursts get through at 8-20 dB. ACK retries carry it, and the fading
  table still gives more throughput there than a slow-fading-safe one
  would.
- **The fading table stops at mode 2.** It was measured on the mic port
  only, so the data port's mode 3 is never picked on a fading link.
- **The GUI** (`openwave_ui`) has no `--phy audio` option yet.

### Known limitations

This is an interactive-messaging tool, not a bulk-file-transfer one -
use `file_xfer_test.sh` for bulk transfers. `--auto`'s
negotiation (see above) is one-time at startup, not continuous.

**More fragments means a real cost either way - latency with `--ack`,
reliability without it.** With `--ack`, each fragment is individually
retried (see above), so more fragments just means more round trips, not
more risk. **Without `--ack`, a lost fragment has no retry at all** - a
message needing many fragments is meaningfully less reliable than one
that fits in a single burst, since losing *any one* fragment loses the
whole message. `--mode 0`/`1` (BPSK) paired with `--fec-preset r1_4_k7`
is the extreme case (as low as 5 chars/burst - a full sentence can need
20+ fragments); `--mode 2` or `4` (QPSK/16-QAM) needs far fewer fragments
for the same message. A lost fragment's data is genuinely unrecoverable
without `--ack`, but it's no longer silent either way: the receiving
station logs a clear reason when a fragmented message gets abandoned
mid-reassembly instead of just never showing up.
