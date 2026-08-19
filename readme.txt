OpenWave - Implementation Overview
===================================

This file explains what's actually in this repo and how the pieces fit
together. For the protocol/wire-format spec itself (frame layout, header
fields, Waveform Modes), see OpenWave_Description.txt. For the current
status of each piece and what's not done yet, see tasklist.txt.

File map
--------
openwave_link.py
    The link layer. Header build/parse, CRC32, ACK/retry, fragmentation,
    the preamble/sync-word, and a full RX dispatch implementation
    (LinkReceiver) - all pure Python, zero GNU Radio dependency. This is
    the single source of truth for the framing format; phy_sim_loopback.grc,
    tx_pluto.grc, and rx_rtlsdr.grc's embedded blocks all import it so they
    can't drift out of sync with each other. See its module docstring and
    tests/test_openwave_link.py for the full API.

openwave_cli.py
    Command-line, file-based testing of the link layer - build and parse
    real frame bytes with no GNU Radio and no hardware. `send` writes a
    frame to a file (plain text, --json, --file for raw binary payloads
    like a PNG, or --attach for a text message plus a file attachment
    together, with options to deliberately corrupt it for negative
    testing); `dump` hex-dumps and labels every field of every frame in a
    file; `recv` decodes a file through the full LinkReceiver pipeline
    (--output-file / --save-attachment to write --file/--attach content
    back out without lossy text decoding). Both dump/recv take an optional
    --correlate flag (see openwave_sync.py below) for data that came
    through a noisy channel. Run `python3 openwave_cli.py help` (or `send
    -h` / `recv -h`) for full usage and examples.

openwave_sync.py
    Tolerant frame synchronization: finds the preamble+sync-word via a
    sliding Hamming-distance correlator (like the real flowgraphs'
    digital_correlate_access_code_tag_bb) instead of requiring an exact
    byte match. This is what actually lets the PRN preamble's noise
    resilience do anything in this file-based pipeline - see its module
    docstring. Used via openwave_cli.py's --correlate flag.

openwave_modem.py
    PHY-layer bits <-> symbols mapping (currently BPSK only - see
    tasklist.txt Phase 6 for the eventual multi-mode plan). Pure Python,
    zero dependencies. Deliberately has no symbol-rate concept - see its
    module docstring for why that's a hard boundary until pulse shaping.

openwave_pulse.py
    RRC pulse shaping: symbols <-> real timed samples. Needs numpy - the
    first stage where a real DSP dependency became unavoidable. alpha=0.35
    and an 11-symbol filter span match the convention already used in
    tx_pluto.grc/phy_sim_loopback.grc's firdes.root_raised_cosine() calls.

openwave_channel.py
    AWGN channel simulation - add noise to a sample file, either by direct
    noise_voltage (matching channels_channel_model_0's parameter) or a
    target SNR in dB measured against the file's own signal power. Sample-
    level, same reasoning as openwave_pulse.py for why this only makes
    sense after pulse shaping, not before.

modulate.py / pulseshape.py / channel.py / matchedfilter.py / demodulate.py / plot_constellation.py
    CLI stages built on the modules above, chained together into a full
    bits -> symbols -> RRC samples -> (optionally noisy) -> symbols -> bits
    pipeline with a visualization step in the middle. See "PHY-Layer
    Pipeline" below for the full command sequence, what each stage does,
    and why matched filtering is required (not optional) for a clean
    constellation.

openwave_fec.py
    Forward error correction: convolutional encoding + hard-decision
    Viterbi decoding (numpy-vectorized - see its module docstring), fully
    parameterized by generator polynomials/constraint length K rather than
    hardcoded to one scheme. PRESETS holds 13 named configurations
    (r1_2_k3 through r1_6_k9 - rate 1/N at constraint length K), plus an
    empirically-measured "which preset for which SNR" reference table
    right after PRESETS. free_distance()/find_generators() are how the
    presets were chosen (Dijkstra over the trellis, not hand-picked - see
    the module docstring for why that replaced an earlier hand-picked
    preset that had silently-truncated, out-of-range generator values).

fec_encode.py / fec_decode.py
    CLI wrappers around openwave_fec.py: encode a built frame (or a whole
    build_fragmented_frames() run - every fragment is protected
    independently) before modulate.py, and decode+correct a possibly-noisy
    received frame after demodulate.py, using openwave_sync.py's tolerant
    correlator to find each fragment's sync (falling back to an unbounded
    search and reporting an explicit 'gap' status if a fragment's marker
    didn't survive the channel at all - see openwave_sync.py's
    FRAGMENT_GAP_SEARCH_BITS for why this matters: an unbounded-only
    search would silently lock onto a LATER fragment instead, with no
    indication the one in between was lost). See -h on either for full
    usage; see "FEC + Full File Transfer Testing" below for how they fit
    into the full pipeline.

openwave_fec_generate_presets.py
    Regenerates/extends openwave_fec.py's PRESETS table via
    find_generators()'s free-distance search - prints ready-to-paste
    Python source, doesn't edit the file for you. Run with -h for options
    (which rates/K values to search, candidate count, RNG seed).

pipeline_test.sh / file_xfer_test.sh
    Turnkey end-to-end test scripts - see "FEC + Full File Transfer
    Testing" below.

phy_sim_loopback.grc (formerly test1.grc / test2.grc)
    Simulated loopback testbench: TX -> AWGN channel model -> RX, all in one
    GNU Radio process, no hardware involved. The GUI test bench for
    exercising the full modem+link-layer chain end to end. Detailed below.

tx_pluto.grc / rx_rtlsdr.grc (formerly test3_tx.grc / test3_rx.grc)
    Real SDR hardware bring-up, split into independent TX and RX stations:
    tx_pluto.grc drives an ADALM-Pluto (iio_pluto_sink), rx_rtlsdr.grc
    receives on an RTL-SDR (osmosdr). This is a validation step on the way
    to the actual deployment target (driving real transceivers through
    their audio ports - see tasklist.txt Phase 5), not the destination
    itself. Detailed below.

tests/
    Regression suite - run everything with `./run_tests.sh`, or a single
    module with `./run_tests.sh tests.test_openwave_link`. All of these
    except test_phy_loopback.py/test_flowgraphs_compile.py are fast,
    pure-Python, and need no GNU Radio:
      test_openwave_link.py    - link layer: header/CRC/ACK/fragmentation
      test_openwave_modem.py   - bits<->BPSK symbols mapping
      test_openwave_pulse.py   - RRC pulse shaping/matched filtering
      test_openwave_channel.py - AWGN channel simulation
      test_openwave_sync.py    - tolerant Hamming-distance sync correlator
      test_openwave_fec.py     - convolutional encode/Viterbi decode, FEC
                                  presets, multi-fragment FEC, sync-gap
                                  handling
      test_phy_loopback.py     - headless GNU Radio runs of phy_sim_loopback.grc
      test_flowgraphs_compile.py - grcc compile check for all three .grc files

lorem.txt, colorbar_test.png, test_image.png
    Generated test assets for file_xfer_test.sh's FILE argument/--attach -
    lorem.txt is plain text, the two PNGs are synthetic (not personal
    files) sized to safely clear the 64-fragment/~64KB ceiling even after
    --attach's base64 inflation (~33%). Regenerate colorbar_test.png/
    test_image.png with a short PIL/numpy script if they're ever needed
    again at a different size.

test.grc, test1.grc, test1_*.py, test2.py, test2_*.py
    Earlier prototypes, superseded by phy_sim_loopback.grc. test2.grc itself
    was removed; test2.py (its last compiled output) and its companion
    epy_block .py files are orphaned leftovers. Left in the repo for history
    but not maintained.


phy_sim_loopback.grc - Interactive pi/4-QPSK Modem Loopback
=============================================================

Type a message into the "Send" box, it gets framed (real header, CRC32,
optionally fragmented) and modulated, pushed through a simulated noisy
channel, then demodulated, parsed, and shown in the "Received" box, with a
running "Link Stats" line (frame counts, sequence/flag info, mode, src/dst).
There's no radio hardware involved - the "channel" is a software noise
model - so this is for exercising/demonstrating the modem+link-layer chain,
not a real transmit/receive system.

Variables & Parameters
-----------------------
msg (Parameter, -m/--msg)
    Startup default text for the Send box. Only used once, when the flow
    graph first constructs the GUI - after that, whatever you type in the
    Send box is what gets transmitted.

symbol_rate (Parameter, -r/--symbol-rate)
    Baud rate in symbols/sec - one of the OpenWave modes 600-9600.
    Command-line configurable.

sps (Variable = 4)
    Samples per symbol used throughout the modulator/demodulator.

samp_rate (Variable = symbol_rate * sps)
    The complex sample rate the TX/channel/RX chain actually runs at. Feeds
    the Throttle block - makes the simulation run at a realistic pace
    instead of as fast as the CPU allows.

ring_points / qpsk_const-equivalent (variable_constellation)
    An 8-point ring constellation, differentially encoded (modulus 8) on
    TX and un-wound with a differential phasor on RX. This is the standard
    trick for pi/4-QPSK: alternating between two 4-point QPSK constellations
    45 degrees apart, which is what actually produces the "pi/4" phase
    rotation between symbols. Also resolves the Costas loop's phase
    ambiguity, same as plain differential QPSK does.

Transmit chain
--------------
QT GUI Message Edit Box ("Send")
    Posts a message with the typed text every time you press Enter (or the
    box loses focus).

str_to_pdu_0 (embedded Python block)
    Wraps the message into a full OpenWave link-layer frame (or several, if
    it's long enough to need fragmenting) using openwave_link.build_fragmented_frames -
    real preamble, sync word, header (Sequence Number, Flags, Payload
    Length, etc.), payload, and CRC32. If ack_requested is enabled, engages
    openwave_link.RetryManager and retransmits on timeout (driven by a
    500ms message-strobe tick) until acknowledged or max_retries is
    exhausted. See the block's own docstring in the .grc for the full
    fragmentation/ACK interaction.

PDU to Tagged Stream / Unpack K Bits / Pack K Bits / Differential Encoder
    Converts the frame byte PDU into a bitstream, then 8-bit-to-symbol
    groups, then differentially encodes each symbol (modulus 8) relative to
    the previous one - same phase-ambiguity fix described above.

Chunks to Symbols / Interpolating FIR Filter (RRC)
    Maps symbols to the ring constellation's complex points, then upsamples
    by sps and applies root-raised-cosine pulse shaping (0.35 roll-off) to
    produce the transmitted complex baseband signal.

Throttle
    Paces the flow graph to samp_rate instead of free-running as fast as
    the CPU allows.

Channel simulation
-------------------
Channel Model
    Adds a small amount of AWGN noise to the transmitted signal - the only
    impairment currently simulated (frequency/timing offset left at
    none/1.0).

Receive chain
-------------
Symbol Sync / Costas Loop / Constellation Decoder / Differential Phasor
    Timing recovery (Modified M&M TED, MMSE 8-tap interpolator), carrier
    phase recovery (order-4 Costas loop), slicing to the nearest ring point,
    and differential decoding to resolve the Costas loop's phase ambiguity -
    same roles as in the TX chain, run in reverse.

Correlate Access Code Tag
    Finds the sync word in the recovered bitstream and tags exactly where
    the header starts (tag name "sync") - this replaces any zero-run/marker
    guessing from earlier prototype versions.

stream_display_sink_0 (embedded Python block)
    Uses the "sync" tag to know exactly where the header starts, then
    openwave_link.FrameParser to collect Header+Payload+CRC32 (per the
    header's real Payload Length field) and verify the CRC. Tracks Sequence
    Number (gap/duplicate detection), decodes Mode ID and Source/
    Destination ID, handles ACK frames and fragment reassembly, and
    publishes a formatted stats line. See openwave_link.LinkReceiver for the
    GNU-Radio-free version of this same dispatch logic.

ack_responder_0 (embedded Python block)
    Turns a needed-ACK notification from stream_display_sink_0's
    'ack_needed' port into a real ACK frame PDU. Feeds back through the same
    TX chain and channel, closing a real ACK/retry loop entirely within this
    one flow graph - something tx_pluto.grc/rx_rtlsdr.grc structurally can't
    do, since an RTL-SDR receiver has no transmit capability at all.

QT GUI Message Edit Box ("Received") / ("Link Stats")
    Display the decoded message text and the running stats line,
    respectively.

QT GUI Constellation Sink
    Plots the complex samples after carrier recovery (before slicing) - the
    "is the modem actually locked" visual. A clean run shows eight tight
    clusters (the ring constellation); a still-acquiring or badly-tuned loop
    shows a smeared ring or a rotating blob instead.


tx_pluto.grc / rx_rtlsdr.grc - Real SDR Hardware Bring-Up
=============================================================

Same modem and link-layer chain as phy_sim_loopback.grc, but split into two
independent stations talking over real RF instead of a simulated channel -
tx_pluto.grc transmits on an ADALM-Pluto, rx_rtlsdr.grc receives on an
RTL-SDR. Run them as two separate processes (optionally on two different
machines).

tx_pluto.grc parameters
------------------------
msg (-m/--msg), symbol_rate (-r/--symbol-rate)
    Same as phy_sim_loopback.grc.

center_freq_default (-f/--center-freq-default)
    Center frequency in Hz (default 433 MHz, 70cm amateur band). Must match
    rx_rtlsdr.grc's center_freq_default.

pluto_uri (-u/--pluto-uri)
    IIO URI for the Pluto device (blank = auto-detect).

tx_atten (-a/--tx-atten)
    Pluto TX attenuation in dB (higher = less power).

rx_rtlsdr.grc parameters
--------------------------
symbol_rate (-r/--symbol-rate), center_freq_default (-f/--center-freq-default)
    Same as above - center_freq_default must match tx_pluto.grc's.

rtl_gain (-g/--rtl-gain), rtl_ppm (-c/--rtl-ppm)
    Manual RF gain in dB, and frequency correction in ppm (calibrate with
    `rtl_test -p` or equivalent before first use - RTL-SDR crystal error can
    be tens of ppm, which at UHF can exceed the whole channel width).

station_id (-s/--station-id)
    This receiver's own callsign. Blank (default) = accept every frame
    unfiltered - the only sane default today since no TX chain populates a
    real Destination ID yet. Once set, only frames addressed to station_id
    or a broadcast (the literal 'ALL' callsign, or that same placeholder)
    are processed further; everything else is dropped and counted under
    'not_for_us' in the stats line. See openwave_link.is_for_station().

capture_file (-o/--capture-file)
    Path to append every completed frame attempt to - successful, CRC-
    failed, and bad-length alike - in openwave_link.build_frame()'s exact
    byte format. Blank (default) = disabled. A capture file can be inspected
    later with no conversion:
        python3 openwave_cli.py dump -i capture.bin
        python3 openwave_cli.py recv -i capture.bin
    This is the tool for "something went wrong on the air, let me look at
    exactly what was received afterward."

What's not simulated here that phy_sim_loopback.grc doesn't have to deal
with: real RF impairments (frequency offset, multipath, actual noise floor),
and the fact that RTL-SDR is receive-only, so rx_rtlsdr.grc structurally
cannot transmit an ACK back even when a received frame requests one - the
ACK/retry loop only closes in phy_sim_loopback.grc today (see tasklist.txt
2.9 for the sketch of what full-duplex hardware would take).


openwave_cli.py - File-Based Link-Layer Testing
====================================================

Run `python3 openwave_cli.py help` (or with no arguments) for full usage.
Quick reference:

    send: build a frame and write it to a file
        python3 openwave_cli.py send "hello world" -o burst.bin
        python3 openwave_cli.py send --json '{"cmd": "ping"}' -o burst.bin
        python3 openwave_cli.py send "for w1aw" -o net.bin --src K0MDT --dst W1AW
        python3 openwave_cli.py send "long msg..." -o frag.bin --chunk-size 4

        Negative testing - inject a specific kind of bad frame on purpose:
        python3 openwave_cli.py send "x" -o bad.bin --corrupt-crc
        python3 openwave_cli.py send "x" -o bad.bin --corrupt-payload
        python3 openwave_cli.py send "x" -o bad.bin --corrupt-header
        python3 openwave_cli.py send "x" -o bad.bin --bad-length 9999

        Send a whole binary file (e.g. a PNG/JPG) as the payload, no text -
        exercises fragmentation on real, non-repeating binary content
        instead of just text (a good stress test - a several-hundred-KB
        image needs dozens of fragments):
        python3 openwave_cli.py send --file photo.png -o img.bin

        Send a text message WITH a file attached - a JSON envelope
        ({text, filename, attachment_b64}) over the existing --json payload
        path, since the link layer only ever sees opaque bytes either way:
        python3 openwave_cli.py send "check this out" --attach photo.png -o msg.bin

    dump: hex-dump and label every field of every frame in a file
        python3 openwave_cli.py dump -i burst.bin

    recv: decode a file through the full LinkReceiver pipeline
        python3 openwave_cli.py recv -i burst.bin --station-id W1AW

        Recover a `send --file` binary payload - plain recv would corrupt
        it via lossy UTF-8 text decoding, so this writes the raw undecoded
        bytes instead of printing them:
        python3 openwave_cli.py recv -i img.bin --output-file recovered.png

        Recover a `send --attach` message: text still prints normally, and
        --save-attachment extracts+writes the attachment (base64-decoded):
        python3 openwave_cli.py recv -i msg.bin --save-attachment recovered.png

None of this touches GNU Radio or any hardware - it exercises
openwave_link.py directly, which is useful both for quick sanity-checking
the framing logic and for offline analysis of a real capture (see
rx_rtlsdr.grc's capture_file parameter above).


PHY-Layer Pipeline: Bits to Samples and Back
=================================================

A file-based, GNU-Radio-free path from a real link-layer frame all the way
down to RRC-shaped baseband samples and back, mirroring what tx_pluto.grc/
rx_rtlsdr.grc do internally - useful for testing/understanding the modem in
isolation, and a step toward the eventual GNU-Radio-free stack (see
tasklist.txt's "No GNU Radio, eventually" note under Phase 5).

Requires numpy (openwave_pulse.py) and plotly (plot_constellation.py only).
Neither is a GNU Radio dependency - if the system Python is externally
managed (PEP 668, e.g. Debian/Ubuntu) and you don't want a venv:
    pip install --user --break-system-packages plotly

Full pipeline, one stage at a time:

    # 1. Build a real OpenWave frame (see openwave_cli.py above)
    python3 openwave_cli.py send "hello world" -o test.bin --src K0MDT --dst W1AW

    # 2. Inspect the frame's fields by hand
    python3 openwave_cli.py dump -i test.bin

    # 3. Modulate: bits -> BPSK symbols (no sample rate involved yet)
    python3 modulate.py -t bpsk -i test.bin -o symbols.cfile

    # 4. Pulse-shape: symbols -> oversampled RRC baseband samples
    python3 pulseshape.py -i symbols.cfile -o shaped.cfile

    # 5. Plot BEFORE matched filtering - expect a smeared line, not clean
    #    points (a single RRC filter alone is NOT zero-ISI)
    python3 plot_constellation.py -i shaped.cfile -o before_mf.html \
        --title "before matched filter"

    # 6. (Optional) simulate a noisy channel - insert here, between pulse
    #    shaping and the matched filter (noise is a sample-level thing).
    #    Skip this step entirely for a clean round trip.
    python3 channel.py -i shaped.cfile -o noisy.cfile --snr-db 10
    #    (or: --noise-voltage 0.05 for a direct value instead of target SNR)

    # 7. Matched filter + decimate: back down to one sample/symbol.
    #    Input is noisy.cfile if you did step 6, shaped.cfile if you skipped it.
    python3 matchedfilter.py -i noisy.cfile -o recovered_symbols.cfile

    # 8. Plot AFTER matched filtering - expect two clean dots at +/-1.0 with
    #    no noise, or two fuzzy clusters around +/-1.0 with noise added
    #    (TX RRC + RX RRC in cascade = a full raised-cosine response,
    #    which IS zero-ISI at the correct symbol-spaced sampling instant)
    python3 plot_constellation.py -i recovered_symbols.cfile -o after_mf.html \
        --title "after matched filter"

    # 9. Demodulate: symbols -> bits/bytes
    python3 demodulate.py -t bpsk -i recovered_symbols.cfile -o out.bin

    # 10. Verify the round trip reproduced the original frame exactly
    #     (expect this to fail at low enough SNR - that's the point)
    cmp test.bin out.bin && echo "IDENTICAL"

    # 11. Confirm it still decodes correctly through the link layer
    python3 openwave_cli.py recv -i out.bin

Notes:
  - openwave_cli.py's dump/recv only understand byte-level frame files
    (test.bin/out.bin) - never point them at a .cfile (raw complex samples).
  - Steps 3+9 alone (skip the rest) also round-trip correctly, if you just
    want to test the bits<->symbols mapping without pulse shaping.
  - --alpha/--sps/--span on pulseshape.py and matchedfilter.py must match
    between the two (matchedfilter.py can't know what the sender used - see
    each script's -h for defaults, currently alpha=0.35, sps=4, span=11).
  - plot_constellation.py writes a self-contained interactive HTML file
    (zoom/pan/hover for exact I/Q values) - open it in any browser, no
    display/X11 forwarding required on a remote box. It deliberately uses
    plain SVG rendering (Plotly's go.Scatter, not go.Scattergl) rather than
    WebGL, so it works even in browsers/VMs without WebGL support.
  - By default, openwave_cli.py recv/dump use exact byte matching to find
    a frame (openwave_link.iter_frames()/describe_frames()) - a single bit
    error anywhere in the 36-byte preamble+sync-word breaks that match
    completely, so at low SNR you'll see OK:0 with no CRC-fail count
    either (frame not found at all), not a rising CRC-fail count. Pass
    --correlate to use openwave_sync.py's tolerant Hamming-distance
    correlator instead (matching what digital_correlate_access_code_tag_bb
    does in the real flowgraphs) - it finds frames the exact-match path
    misses entirely, informally down to roughly ~2dB SNR for BPSK at these
    filter settings before header/payload bit errors start causing CRC
    failures even once sync is found - see "FEC + Full File Transfer
    Testing" below for how FEC pushes that floor much lower. Example:
        python3 channel.py -i shaped.cfile -o noisy.cfile --snr-db 1.5 --seed 4
        python3 matchedfilter.py -i noisy.cfile -o recovered_symbols.cfile
        python3 demodulate.py -t bpsk -i recovered_symbols.cfile -o out.bin
        python3 openwave_cli.py recv -i out.bin              # OK:0 - not found
        python3 openwave_cli.py recv -i out.bin --correlate  # OK:1 - decoded


FEC + Full File Transfer Testing
=================================================

Everything above tests the modem chain one stage at a time by hand. FEC
(openwave_fec.py) sits between send/fec_encode and demodulate/fec_decode
in that same chain, and pipeline_test.sh / file_xfer_test.sh wrap the
*whole* thing - frame build through FEC/modem/noise/FEC-decode back to a
verified round trip - into single commands, so you don't have to chain
seven scripts by hand every time.

pipeline_test.sh - single synthetic frame ("hello world"), instrumented
with a dump/byte-count/IDENTICAL check at every stage. Use this to see
*how* the pipeline works or to debug a specific stage.

    ./pipeline_test.sh                                  # clean, no FEC
    ./pipeline_test.sh --snr -6                          # noisy, no FEC (expect failure)
    ./pipeline_test.sh --snr -6 --fec r1_3_k7             # noisy, FEC-protected
    ./pipeline_test.sh -h                                 # full flag reference

file_xfer_test.sh - a real message and/or file (e.g. lorem.txt,
colorbar_test.png) through send/--attach -> the same pipeline -> recv,
with pass/fail verification (message text match, byte-identical
attachment) and transmitted-size/timing reporting. Use this for realistic
payloads and for sweeping SNR/FEC combinations.

    ./file_xfer_test.sh lorem.txt --message "check this out"
    ./file_xfer_test.sh colorbar_test.png --snr -6 --fec r1_3_k7
    ./file_xfer_test.sh -h                                # full flag reference

Both scripts write every intermediate file (frames, symbols, plots,
recovered attachment) to a fresh `mktemp -d` directory under /tmp - never
to the repo - named like
/tmp/openwave_file_xfer_test.mode<M>_snr<SNR>_fec<PRESET>.<random>, so
several runs (including different configs) can run at once without
clobbering each other, and each run prints its own "Working dir:" line so
you can go find the plots/files afterward.

Choosing a preset: FEC is a bandwidth-for-resilience trade - stronger
presets (higher constraint length K, lower rate 1/N) correct more errors
but cost more transmitted bytes and more Viterbi decode time (state count
scales as 2**(K-1)). Don't reach for a stronger preset than your SNR
actually needs - see the empirically-measured SNR-to-preset table in
openwave_fec.py right after PRESETS for concrete measured pass rates
(e.g. r1_2_k3 is already 100% reliable at 3dB; -6dB needs at least
r1_4_k9). Sweep your own SNR/preset combination with a shell loop over
file_xfer_test.sh's exit code (0 = pass) - see openwave_fec_generate_presets.py
if you need a preset that isn't in the table at all.

Sync gaps: if a fragment's marker doesn't survive the noise, fec_decode.py
(and openwave_cli.py recv --correlate, for the non-FEC path) reports an
explicit 'gap' status for it rather than silently skipping straight to
the next fragment - see openwave_sync.py's FRAGMENT_GAP_SEARCH_BITS. A
lost fragment's data is still unrecoverable either way (that's real
information loss, not a bookkeeping problem), but a gap is now always
visible in fec_decode.py's per-fragment status output instead of an
unexplained "attachment doesn't match" at the end.
