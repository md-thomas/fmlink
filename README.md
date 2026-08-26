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
silently-truncated, out-of-range generator values).

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
through a simulated over-the-air network link (path loss, fading, AWGN,
latency), pushing real frames through the actual modem chain instead of a
one-shot file pipeline. See "Over-The-Air (OTA) Simulator" below.

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
between two peers talking in raw IQ samples. `ota_sim` has no
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

The simulator learns each station's real network address from the first
datagram it sends (a small "hello" `ota_station.py` sends automatically at
startup) - there's no static address configuration needed on the simulator
side beyond its own `--listen-host`/`--listen-port`.

### Options

Run `python3 ota_station.py -h` for the full reference, including a
table of the Waveform Modes and FEC presets available. Key flags:

`--mode N` (0-4) selects a Waveform Mode ID - modulation and symbol rate
per `openwave_link.py`'s `WAVEFORM_MODES`. `--fec` FEC-encodes/decodes
using `--fec-preset` (default: `r1_4_k7`, independent of the mode's own
documented default preset - see `openwave_fec.py`'s `PRESETS` for the
full list). `--ack` requests ACKs on sends and retries on timeout
(single-fragment messages only, matching `build_fragmented_frames()`'s
existing fragmentation/ACK rule) - the sender prints `[ack] delivered`
once the peer's ACK actually resolves the pending send, so you know a
message got through without `--ack`'s only other feedback being silence
or `[ack] gave up`. Without `--ack`, there's no delivery confirmation at
all - that's inherent to the protocol (no ACK requested, none sent back),
not a missing feature. If a peer's own ACK gets lost on the way back (a
real, expected channel event, not a bug), the sender retries the same
message - the receiver recognizes the retransmit and prints
`[rx] duplicate of the last message (already delivered)` instead of
showing the message a second time. **Both stations in a session must be
launched with the same `--mode`/`--fec`/`--fec-preset`** - there's no
in-band mode negotiation yet (that's tasklist.txt Phase 6.4), so the
receiver has to already know which modulation to demodulate with.

**FEC preset vs. modulation is a real size tradeoff, not just a dial**:
a stronger preset (lower rate, e.g. `r1_4_k7`) multiplies the coded
frame's size, and a lower-order modulation (BPSK) needs more symbols -
and therefore more samples - per coded byte than QPSK/16-QAM. Combining
the two can push a single burst over UDP's ~65KB single-datagram limit
even for a short message - e.g. `--mode 0` (BPSK, the default) +
`--fec-preset r1_4_k7` (also the default when `--fec` is set) only fits
**5 characters** per burst. Every station prints its actual per-burst
character budget at startup (`max ~N chars per burst at these settings`)
so this is visible up front instead of discovered message-by-message; a
longer message just auto-fragments into multiple bursts, each
independently bounded the same way. `ota_station.py` refuses to send an
oversized burst and logs a clear `[tx error]` instead of silently
failing - if you see one, use a higher `--mode` (QPSK/16-QAM) and/or a
lighter FEC preset (e.g. `r1_2_k7`) together.

`ota_sim.simulator`'s channel knobs (`--distance-km`, `--freq-mhz`,
`--base-snr-db`, `--no-fading`, `--drop-prob`, `--latency-ms`,
`--jitter-ms`, `--seed`) are documented in `../ota_sim/README.md` and
`python3 -m ota_sim.simulator -h`.
A tighter/noisier link is a good way to see the real link layer at work:
higher `--drop-prob` or lower `--base-snr-db` should never produce garbled
text (CRC-verified frames only), just gaps or delivery failures - the same
"solid link layer" property `tests/test_ota_station.py` and the archived
`test_phy_loopback.py`'s noisy-channel test both check for.

### Known limitations

This is an interactive-messaging tool, not a bulk-file-transfer one -
UDP's ~65KB single-datagram ceiling bounds worst-case on-air sample counts
(low-order modulation + strong FEC + a near-`MAX_PAYLOAD_LEN` fragment can
exceed it). Use `file_xfer_test.sh` for bulk transfers. There's also no
mode negotiation (see above) and no retry across fragmented multi-frame
messages (matching the rest of the project's current ACK/fragmentation
behavior).
