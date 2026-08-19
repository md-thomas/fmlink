#!/bin/bash

usage() {
    cat <<'EOF'
Usage: ./file_xfer_test.sh [FILE] [--message TEXT] [--mode MODE] [--snr SNR_DB] [--fec PRESET]

Sends a message and/or a file through openwave_cli.py's send/recv and
verifies the round trip. Exactly one test runs, shaped by what you pass:
  - FILE and --message both given: message + file attachment (send --attach)
  - --message only (no FILE):      plain text message (send)
  - FILE only (no --message):      file attachment with empty text (send --attach)
  - neither given:                 does nothing - nothing to test

  FILE            Path to a file to send as an attachment (positional).
  --message TEXT  Text message to send (combined with FILE if both given).
  --mode MODE     Waveform Mode ID, 0-4 (default: 0) - passed straight to
                  openwave_cli.py send's --mode.
  --snr SNR_DB    Push the transfer through the full modem pipeline
                  (modulate -> pulseshape -> channel -> matchedfilter ->
                  demodulate) with AWGN at this SNR, instead of a clean
                  link-layer-only send/recv round trip. Omit for the fast
                  link-layer-only test (no RF simulation at all).
  --fec PRESET    FEC configuration to use: r1_2_k3, r1_2_k7 (NASA/Voyager
                  standard), r1_2_k9, r1_3_k7, etc (see openwave_fec.py's
                  PRESETS, or run openwave_fec_generate_presets.py -h to
                  search for/add more) - only meaningful combined with
                  --snr. Omit to run the raw (uncoded) pipeline.
  -h, --help      Show this help and exit.

Examples:
  ./file_xfer_test.sh colorbar_test.png --message "check this out"
  ./file_xfer_test.sh colorbar_test.png
  ./file_xfer_test.sh --message "just a text message"
  ./file_xfer_test.sh colorbar_test.png --message "under noise" --snr -6 --fec r1_3_k7
EOF
}

INPUT_FILE=""
MESSAGE=""
MODE=0
SNR_DB=""
FEC_PRESET=""

while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --message) MESSAGE="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --snr) SNR_DB="$2"; shift 2 ;;
        --fec) FEC_PRESET="$2"; shift 2 ;;
        *) INPUT_FILE="$1"; shift ;;
    esac
done

if [ -z "$INPUT_FILE" ] && [ -z "$MESSAGE" ]; then
    echo "Nothing to send - pass a file, --message, or both. See --help."
    exit 0
fi

if [ -n "$INPUT_FILE" ] && [ ! -f "$INPUT_FILE" ]; then
    echo "Input file not found: $INPUT_FILE" >&2
    exit 1
fi

# All intermediate files (frames, symbols, recovered attachment) go to a
# fresh /tmp directory rather than cwd - keeps the repo clean and, as a
# bonus, gives every run its own directory so concurrent runs can never
# clobber each other's files (this is what the xfer_recovered stale-file
# bug below used to guard against with a plain `rm -f`; a unique WORKDIR
# per run makes that whole class of bug impossible instead of just
# papering over one instance of it). The trailing XXXXXX still guarantees
# uniqueness (so running the SAME config twice in parallel is still safe)
# - the mode/snr/fec label in front of it is purely so several concurrent
# runs are tellable apart by name in `ls /tmp` or a job list.
LABEL="mode${MODE}_snr${SNR_DB:-none}_fec${FEC_PRESET:-none}"
WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/openwave_file_xfer_test.${LABEL}.XXXXXX")

echo "mode: $MODE  snr: ${SNR_DB:-none (link-layer only)}  fec: ${FEC_PRESET:-none}"
echo "Working dir: $WORKDIR"
if [ -n "$INPUT_FILE" ] && [ -n "$MESSAGE" ]; then
    echo "Sending message + file attachment: \"$MESSAGE\" + $INPUT_FILE"
elif [ -n "$INPUT_FILE" ]; then
    echo "Sending file attachment only (no text): $INPUT_FILE"
else
    echo "Sending message only (no file): \"$MESSAGE\""
fi
echo ""

FAILED=0
RECV_FLAGS=()
[ -n "$SNR_DB" ] && [ -z "$FEC_PRESET" ] && RECV_FLAGS=(--correlate)

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

# Pushes a built frame file ($1) through either a direct copy (link-layer
# only, no --snr given) or the full modem pipeline (modulate -> pulseshape
# -> channel -> matchedfilter -> demodulate, with FEC encode/decode around
# it if --fec was given), writing the final received frame to $2. Reports
# what FEC does to the transmitted size (--fec's whole point is trading
# bandwidth for error resilience, so that tradeoff should be visible).
run_transfer() {
    local sent="$1"
    local received="$2"

    local orig_bytes
    orig_bytes=$(wc -c < "$sent" | tr -d ' ')
    echo "Bytes before FEC: $(human_size "$orig_bytes")"

    if [ -z "$SNR_DB" ]; then
        echo "Bytes transmitted: $(human_size "$orig_bytes")  (no --snr - link-layer only, no modem/FEC involved)"
        cp "$sent" "$received"
        return
    fi

    local mod_input="$sent"
    if [ -n "$FEC_PRESET" ]; then
        echo "FEC-encoding with preset $FEC_PRESET"
        python3 fec_encode.py -i "$sent" -o "$WORKDIR/xfer_fec.bin" --preset "$FEC_PRESET"
        mod_input="$WORKDIR/xfer_fec.bin"
        local fec_bytes
        fec_bytes=$(wc -c < "$WORKDIR/xfer_fec.bin" | tr -d ' ')
        local overhead=$((fec_bytes - orig_bytes))
        local pct
        pct=$(python3 -c "print(f'{100*$overhead/$orig_bytes:.1f}')")
        echo "Bytes after FEC (transmitted): $(human_size "$fec_bytes")  (+$(human_size "$overhead"), +${pct}%)"
    else
        echo "Bytes transmitted: $(human_size "$orig_bytes")  (no --fec)"
    fi

    python3 modulate.py -t bpsk -i "$mod_input" -o "$WORKDIR/xfer_symbols.cfile"
    python3 pulseshape.py -i "$WORKDIR/xfer_symbols.cfile" -o "$WORKDIR/xfer_shaped.cfile"
    echo "Injecting noise at ${SNR_DB}dB SNR"
    python3 channel.py -i "$WORKDIR/xfer_shaped.cfile" -o "$WORKDIR/xfer_noisy.cfile" --snr-db "$SNR_DB"
    python3 matchedfilter.py -i "$WORKDIR/xfer_noisy.cfile" -o "$WORKDIR/xfer_recsym.cfile"
    python3 demodulate.py -t bpsk -i "$WORKDIR/xfer_recsym.cfile" -o "$WORKDIR/xfer_demod.bin"

    if [ -n "$FEC_PRESET" ]; then
        echo "--- fec_decode.py (preset $FEC_PRESET) ---"
        if python3 fec_decode.py -i "$WORKDIR/xfer_demod.bin" -o "$received" --preset "$FEC_PRESET"; then
            :
        else
            echo "FEC decode failed - frame not recovered"
            : > "$received"  # empty - downstream checks will correctly report FAIL
        fi
    else
        cp "$WORKDIR/xfer_demod.bin" "$received"
    fi
}

# Build and send (message+file and file-only both go through --attach, just
# with an empty text field in the file-only case; message-only uses plain send)
if [ -n "$INPUT_FILE" ]; then
    python3 openwave_cli.py send "$MESSAGE" --attach "$INPUT_FILE" -o "$WORKDIR/xfer_sent.bin" --src K0MDT --dst W1AW --mode "$MODE"
else
    python3 openwave_cli.py send "$MESSAGE" -o "$WORKDIR/xfer_sent.bin" --src K0MDT --dst W1AW --mode "$MODE"
fi

run_transfer "$WORKDIR/xfer_sent.bin" "$WORKDIR/xfer_received.bin"

if [ -n "$INPUT_FILE" ]; then
    RECV_OUTPUT=$(python3 openwave_cli.py recv -i "$WORKDIR/xfer_received.bin" "${RECV_FLAGS[@]}" --save-attachment "$WORKDIR/xfer_recovered")
else
    RECV_OUTPUT=$(python3 openwave_cli.py recv -i "$WORKDIR/xfer_received.bin" "${RECV_FLAGS[@]}")
fi
echo "$RECV_OUTPUT"
echo ""

if [ -n "$MESSAGE" ]; then
    if echo "$RECV_OUTPUT" | grep -q "Message: $MESSAGE"; then
        echo "PASS: message text recovered correctly"
    else
        echo "FAIL: message text missing/incorrect"
        FAILED=1
    fi
fi

if [ -n "$INPUT_FILE" ]; then
    if cmp -s "$INPUT_FILE" "$WORKDIR/xfer_recovered"; then
        echo "PASS: attachment is byte-identical"
    else
        echo "FAIL: attachment does NOT match original"
        FAILED=1
    fi
fi
echo ""

if [ "$FAILED" -eq 0 ]; then
    echo "ALL TESTS PASSED"
else
    echo "SOME TESTS FAILED"
fi

echo ""
echo "Working dir: $WORKDIR"
echo "Total time: $(human_duration "$SECONDS")"

exit "$FAILED"
