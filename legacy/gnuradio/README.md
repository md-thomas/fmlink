OpenWave - Archived GNU Radio Chain
====================================

This directory holds the project's original GNU Radio pi/4-QPSK PHY chain -
the initial testbench OpenWave was built and validated against. The project
has since repositioned around a link-layer/framing protocol (header, CRC,
ACK, fragmentation, FEC - see ../../openwave_link.py) that works over a
*pluggable* PHY rather than being tied to one modulation or to GNU Radio at
all (see ../../openwave_link.py's WAVEFORM_MODES and ../../tasklist.txt).
The pure-Python CLI pipeline at the repo root (modulate.py/demodulate.py,
supporting BPSK/QPSK/16-QAM) is now the actively developed PHY.

Everything here is kept, not deleted, in case it's useful again - e.g. as a
future GNU Radio PHY plugin behind the same link-layer framing.

File map
--------
phy_sim_loopback.grc (formerly test1.grc / test2.grc)
    Simulated loopback testbench: TX -> AWGN channel model -> RX, all in one
    GNU Radio process, no hardware involved. Detailed below.

tx_pluto.grc / rx_rtlsdr.grc (formerly test3_tx.grc / test3_rx.grc)
    Real SDR hardware bring-up, split into independent TX and RX stations:
    tx_pluto.grc drives an ADALM-Pluto (iio_pluto_sink), rx_rtlsdr.grc
    receives on an RTL-SDR (osmosdr). Detailed below.

test.grc, test1.grc, test1_*.py, test2.py, test2_*.py, test3_rx.py, test3_rx_*.py
    Earlier prototypes, superseded by phy_sim_loopback.grc. test2.grc itself
    was removed before archiving; test2.py (its last compiled output) and its
    companion epy_block .py files are orphaned leftovers. Left here for
    history but not maintained.

default.py
    An untitled, orphaned compiled GNU Radio flowgraph with no matching
    .grc anywhere in the repo - provenance unknown. Archived as-is rather
    than deleted; a candidate for outright removal in a future pass if
    nobody identifies what it was for.

tests/
    The two GNU-Radio-dependent test files, moved here from the main
    tests/ directory so the project's default `./run_tests.sh` no longer
    needs a GNU Radio install:
      test_phy_loopback.py     - headless GNU Radio runs of phy_sim_loopback.grc
      test_flowgraphs_compile.py - grcc compile check for all three .grc files
    Both have unittest skipIf guards and no-op cleanly if GNU Radio isn't
    installed. Run them explicitly, from the actual repository root (NOT
    this directory - see each file's module docstring for why):

        cd <repo-root>
        python3 -m unittest discover -s legacy/gnuradio/tests -v


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
