#!/bin/bash

usage() {
    cat <<'EOF'
Usage: ./pipeline_test.sh [--snr SNR_DB] [--mode WAVEFORM_MODE] [--fec PRESET] [--modtype TYPE]

Pushes a real FMlink frame through the full modem pipeline: send -> (fec
encode) -> modulate -> pulseshape -> (channel) -> matchedfilter ->
demodulate -> (fec decode) -> recv. See fmlink_cli.py/fmlink_fec.py for
what each stage does.

  --snr SNR_DB    Inject AWGN at that SNR (dB) between pulse shaping and
                  the matched filter. Omit for a clean round trip (no
                  channel.py step at all).
  --mode MODE     Waveform Mode ID, 0-4 (default: 0) - passed straight to
                  fmlink_cli.py send's --mode. Header metadata only -
                  does NOT select the modulation actually used (see
                  --modtype).
  --fec PRESET    FEC configuration to use: r1_2_k3, r1_2_k7 (NASA/Voyager
                  standard), r1_2_k9, r1_3_k7, etc (see fmlink_fec.py's
                  PRESETS, or run fmlink_fec_generate_presets.py -h to
                  search for/add more). Only meaningful combined with
                  --snr. Omit to skip FEC entirely and run the raw
                  (uncoded) pipeline.
  --modtype TYPE  Modulation scheme to actually use: bpsk (default) or
                  qpsk - passed straight to modulate.py/demodulate.py's -t.
  -h, --help      Show this help and exit.

Examples:
  ./pipeline_test.sh
  ./pipeline_test.sh --snr -6
  ./pipeline_test.sh --snr -6 --fec r1_3_k7
  ./pipeline_test.sh --snr 5 --mode 2 --fec r1_2_k7
  ./pipeline_test.sh --snr -6 --fec r1_3_k7 --modtype qpsk
EOF
}

SNR_DB=""
MODE=0
FEC_PRESET=""
MOD_TYPE=bpsk

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --snr) SNR_DB="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --fec) FEC_PRESET="$2"; shift 2 ;;
        --modtype) MOD_TYPE="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; echo >&2; usage >&2; exit 1 ;;
    esac
done

# All intermediate files (frames, symbols, plots) go to a fresh /tmp
# directory rather than cwd - keeps the repo clean and, as a bonus, gives
# every run its own directory so concurrent runs can never clobber each
# other's files (see file_xfer_test.sh's stale-file/race lessons). The
# trailing XXXXXX still guarantees uniqueness (so running the SAME config
# twice in parallel is still safe) - the mode/snr/fec label in front of it
# is purely so several concurrent runs are tellable apart by name in `ls
# /tmp` or a job list, not what makes this collision-safe.
LABEL="mode${MODE}_snr${SNR_DB:-none}_fec${FEC_PRESET:-none}_mod${MOD_TYPE}"
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/fmlink_pipeline_test.${LABEL}.XXXXXX")

echo "mode: $MODE  snr: ${SNR_DB:-none}  fec: ${FEC_PRESET:-none}  modtype: $MOD_TYPE"
echo "Working dir: $WORKDIR"
echo ""

# Seconds -> human-readable duration, e.g. "12s" or "2m 5s". Uses bash's
# built-in $SECONDS (elapsed since this script started, no external tools
# needed) - see the final "Total time" line at the end of the script.
human_duration() {
    local total=$1
    if [ "$total" -ge 60 ]; then
        echo "$((total / 60))m $((total % 60))s"
    else
        echo "${total}s"
    fi
}

# Bytes -> human-readable B/KB/MB, e.g. "1.35 KB (1382 bytes)".
human_size() {
    python3 -c "
size = $1
label = f'{size} B' if size < 1024 else (f'{size/1024:.2f} KB' if size < 1048576 else f'{size/1048576:.2f} MB')
print(f'{label} ({size} bytes)')
"
}

# 1. Build a real FMlink frame (see fmlink_cli.py above)
python3 fmlink_cli.py send "hello world" -o "$WORKDIR/test.bin" --src K0MDT --dst W1AW --mode "$MODE"
echo ""

# 2. Inspect the frame's fields by hand
python3 fmlink_cli.py dump -i "$WORKDIR/test.bin"
echo ""

ORIG_BYTES=$(wc -c < "$WORKDIR/test.bin" | tr -d ' ')
echo "Bytes before FEC: $(human_size "$ORIG_BYTES")"

# 2b. (Optional) FEC-encode the frame before modulation - protects the
#     Header+Payload+CRC only, preamble/sync-word stay raw (see
#     fmlink_fec.py's module docstring). Reports what FEC does to the
#     transmitted size - that bandwidth-for-resilience tradeoff is the
#     whole point of --fec, so it should be visible.
if [ -n "$FEC_PRESET" ]; then
    echo "FEC-encoding with preset $FEC_PRESET"
    python3 fec_encode.py -i "$WORKDIR/test.bin" -o "$WORKDIR/fec_test.bin" --preset "$FEC_PRESET"
    MODULATE_INPUT="$WORKDIR/fec_test.bin"
    FEC_BYTES=$(wc -c < "$WORKDIR/fec_test.bin" | tr -d ' ')
    OVERHEAD=$((FEC_BYTES - ORIG_BYTES))
    PCT=$(python3 -c "print(f'{100*$OVERHEAD/$ORIG_BYTES:.1f}')")
    echo "Bytes after FEC (transmitted): $(human_size "$FEC_BYTES")  (+$(human_size "$OVERHEAD"), +${PCT}%)"
else
    MODULATE_INPUT="$WORKDIR/test.bin"
    echo "Bytes transmitted: $(human_size "$ORIG_BYTES")  (no --fec)"
fi
echo ""

# 3. Modulate: bits -> symbols (no sample rate involved yet)
python3 modulate.py -t "$MOD_TYPE" -i "$MODULATE_INPUT" -o "$WORKDIR/symbols.cfile"
echo ""

# 4. Pulse-shape: symbols -> oversampled RRC baseband samples
python3 pulseshape.py -i "$WORKDIR/symbols.cfile" -o "$WORKDIR/shaped.cfile"
echo ""


# 5. Plot BEFORE matched filtering - expect a smeared line, not clean
#    points (a single RRC filter alone is NOT zero-ISI)
python3 plot_constellation.py -i "$WORKDIR/shaped.cfile" -o "$WORKDIR/before_mf.html" --title "before matched filter"
echo ""


# 6. Simulate a noisy channel if an SNR was given on the command line -
#    between pulse shaping and the matched filter (noise is a sample-level
#    thing). No SNR given -> clean round trip, no noise.
if [ -n "$SNR_DB" ]; then
    echo "Injecting noise at ${SNR_DB}dB SNR"
    python3 channel.py -i "$WORKDIR/shaped.cfile" -o "$WORKDIR/noisy.cfile" --snr-db "$SNR_DB"
    MF_INPUT="$WORKDIR/noisy.cfile"
else
    echo "No SNR given - clean round trip, no noise"
    MF_INPUT="$WORKDIR/shaped.cfile"
fi
echo ""


# 7. Matched filter + decimate: back down to one sample/symbol.
python3 matchedfilter.py -i "$MF_INPUT" -o "$WORKDIR/recovered_symbols.cfile"
echo ""

# 8. Plot AFTER matched filtering - expect two clean dots at +/-1.0 with
#    no noise, or two fuzzy clusters around +/-1.0 with noise added
#    (TX RRC + RX RRC in cascade = a full raised-cosine response,
#    which IS zero-ISI at the correct symbol-spaced sampling instant)
python3 plot_constellation.py -i "$WORKDIR/recovered_symbols.cfile" -o "$WORKDIR/after_mf.html" --title "after matched filter"
echo ""

# 9. Demodulate: symbols -> bits/bytes
python3 demodulate.py -t "$MOD_TYPE" -i "$WORKDIR/recovered_symbols.cfile" -o "$WORKDIR/out.bin"
echo ""

# 9b. (Optional) FEC-decode: finds sync via the tolerant correlator and
#     Viterbi-decodes the header/payload blocks, correcting bit errors.
#     Writes a clean, standard (uncoded) frame that recv can read directly -
#     no --correlate needed afterward, fec_decode.py already handled sync.
RECV_INPUT="$WORKDIR/out.bin"
if [ -n "$FEC_PRESET" ]; then
    echo "--- fec_decode.py (preset $FEC_PRESET) ---"
    if python3 fec_decode.py -i "$WORKDIR/out.bin" -o "$WORKDIR/corrected.bin" --preset "$FEC_PRESET"; then
        RECV_INPUT="$WORKDIR/corrected.bin"
    else
        echo "FEC decode failed - frame not recovered"
        RECV_INPUT=""
    fi
    echo ""
fi

# 10. Verify the round trip reproduced the original frame exactly. Only
#     meaningful without FEC - fec_decode.py rebuilds a fresh canonical
#     frame (different lead/end padding than the original) on success, so
#     it's never byte-identical to test.bin even when FEC worked perfectly.
if [ -z "$FEC_PRESET" ]; then
    cmp "$WORKDIR/test.bin" "$WORKDIR/out.bin" && echo "IDENTICAL"
    echo ""
fi

# 11. Confirm it still decodes correctly through the link layer.
#     Without FEC and with noise, also show plain (exact-match) recv
#     failing so the benefit of --correlate (tolerant sync via
#     fmlink_sync.py) is visible each run, not just the noise-free case.
if [ -n "$RECV_INPUT" ]; then
    if [ -n "$SNR_DB" ] && [ -z "$FEC_PRESET" ]; then
        echo "--- plain recv (exact byte match) ---"
        python3 fmlink_cli.py recv -i "$RECV_INPUT"
        echo ""
        echo "--- recv --correlate (tolerant Hamming-distance sync) ---"
        python3 fmlink_cli.py recv -i "$RECV_INPUT" --correlate
    else
        python3 fmlink_cli.py recv -i "$RECV_INPUT"
    fi
fi

echo ""
echo "Working dir: $WORKDIR"
echo "Total time: $(human_duration "$SECONDS")"
