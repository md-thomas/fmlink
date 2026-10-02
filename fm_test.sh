#!/usr/bin/env bash
# Launches a full simulated FM radio link for interactive testing: the
# ota_sim simulator in FM mode plus two ota_station.py stations using the
# audio PHY (--phy audio), each in its own terminal window. Type messages
# or /commands in either station window; the simulator window logs every
# burst (airtime, clicks, squelch, fades).
#
# Usage: ./fm_test.sh [options]
#   --port mic|data       radio audio connection, all three processes (default: mic)
#   --cnr DB              carrier-to-noise ratio in the IF bandwidth (default: 15;
#                         ~8 is the fringe, below ~6 the squelch stays shut)
#   --fading none|rayleigh|rician   (default: none)
#   --speed KMH           mobile speed for fading (default: 50 when fading is on)
#   --freq-mhz MHZ        carrier frequency, sets the Doppler rate (default: 146)
#   --ppm PPM             sound-card clock offset between the radios (default: 0)
#   --sim-port PORT       simulator TCP port (default: 9500)
#   --no-auto             fixed --mode 0 on both stations instead of --auto
#   --no-ack              don't request ACKs
#   --sim-args "..."      extra ota_sim.simulator flags, e.g. "--fm-channel wide"
#   --station-args "..."  extra flags for both stations, e.g. "--verbose"
#   --terminal NAME       xfce4-terminal, gnome-terminal or xterm (default: first found)
#   --dry-run             print the three commands instead of opening windows
#
# Examples:
#   ./fm_test.sh
#   ./fm_test.sh --port data --cnr 10
#   ./fm_test.sh --fading rayleigh --speed 80 --freq-mhz 440
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PORT=mic
CNR=15
FADING=none
SPEED=
FREQ_MHZ=146
PPM=0
SIM_PORT=9500
AUTO=1
ACK=1
SIM_ARGS=
STATION_ARGS=
TERMINAL=
DRY_RUN=0

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT=$2; shift 2 ;;
        --cnr) CNR=$2; shift 2 ;;
        --fading) FADING=$2; shift 2 ;;
        --speed) SPEED=$2; shift 2 ;;
        --freq-mhz) FREQ_MHZ=$2; shift 2 ;;
        --ppm) PPM=$2; shift 2 ;;
        --sim-port) SIM_PORT=$2; shift 2 ;;
        --no-auto) AUTO=0; shift ;;
        --no-ack) ACK=0; shift ;;
        --sim-args) SIM_ARGS=$2; shift 2 ;;
        --station-args) STATION_ARGS=$2; shift 2 ;;
        --terminal) TERMINAL=$2; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

case "$PORT" in mic|data) ;; *) echo "--port must be mic or data" >&2; exit 2 ;; esac
case "$FADING" in none|rayleigh|rician) ;; *) echo "--fading must be none, rayleigh or rician" >&2; exit 2 ;; esac
if [ "$FADING" != none ] && [ -z "$SPEED" ]; then SPEED=50; fi

SIM_CMD="python3 -m ota_sim.simulator --listen-port $SIM_PORT --channel fm --port $PORT"
SIM_CMD+=" --base-snr-db $CNR --freq-mhz $FREQ_MHZ --fading $FADING --clock-offset-ppm $PPM"
[ -n "$SPEED" ] && SIM_CMD+=" --speed-kmh $SPEED"
[ -n "$SIM_ARGS" ] && SIM_CMD+=" $SIM_ARGS"

STATION_OPTS="--sim-host 127.0.0.1 --sim-port $SIM_PORT --phy audio --audio-port $PORT"
[ "$AUTO" = 1 ] && STATION_OPTS+=" --auto" || STATION_OPTS+=" --mode 0 --fec"
[ "$ACK" = 1 ] && STATION_OPTS+=" --ack"
[ -n "$STATION_ARGS" ] && STATION_OPTS+=" $STATION_ARGS"
A_CMD="python3 ota_station.py --station-id K0MDT --dst W1AW $STATION_OPTS"
B_CMD="python3 ota_station.py --station-id W1AW --dst K0MDT $STATION_OPTS"

if [ "$DRY_RUN" = 1 ]; then
    printf '%s\n%s\n%s\n' "$SIM_CMD" "$A_CMD" "$B_CMD"
    exit 0
fi

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$SIM_PORT") 2>/dev/null; }

if port_open; then
    echo "error: something is already listening on port $SIM_PORT - close the old" \
         "simulator, or pass --sim-port" >&2
    exit 1
fi

if [ -z "$TERMINAL" ]; then
    for candidate in xfce4-terminal gnome-terminal xterm; do
        if command -v "$candidate" >/dev/null; then TERMINAL=$candidate; break; fi
    done
fi
if [ -z "$TERMINAL" ] || ! command -v "$TERMINAL" >/dev/null; then
    echo "error: no supported terminal found (xfce4-terminal, gnome-terminal, xterm)." \
         "Run these in three terminals yourself:" >&2
    printf '  %s\n' "$SIM_CMD" "$A_CMD" "$B_CMD" >&2
    exit 1
fi

# Open one window running cmd; it stays open after cmd exits so its output
# (session summary, errors) can still be read.
open_window() {
    local title=$1 cmd=$2
    local wrapped="cd '$PWD' && $cmd; echo; read -rp '[$title exited - press Enter to close]'"
    case "$TERMINAL" in
        xfce4-terminal) xfce4-terminal --title="$title" --command="bash -c \"$wrapped\"" & ;;
        gnome-terminal) gnome-terminal --title="$title" -- bash -c "$wrapped" & ;;
        xterm) xterm -T "$title" -e bash -c "$wrapped" & ;;
        *) echo "error: unsupported --terminal $TERMINAL" >&2; exit 2 ;;
    esac
}

echo "FM test: $PORT port, CNR $CNR dB, fading $FADING${SPEED:+ at $SPEED km/h}, ${PPM} ppm, port $SIM_PORT"
open_window "ota_sim (FM, $PORT)" "$SIM_CMD"

for _ in $(seq 50); do
    port_open && break
    sleep 0.2
done
if ! port_open; then
    echo "error: simulator didn't start listening on port $SIM_PORT within 10 s -" \
         "check its window" >&2
    exit 1
fi

open_window "K0MDT ($PORT)" "$A_CMD"
open_window "W1AW ($PORT)" "$B_CMD"
echo "Opened 3 windows. Close each station with /quit, the simulator with Ctrl+C."
