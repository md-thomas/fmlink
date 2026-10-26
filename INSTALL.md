# Installing FMlink

How to set up an FMlink station on a new computer, from a bare Linux
install to a working web page. For leveling the audio, the results so far
and troubleshooting on the air, see [RADIO_SETUP.md](RADIO_SETUP.md); for
Winlink over FMlink, its [Pat section](RADIO_SETUP.md#winlink-peer-to-peer-pat-over-fmlink).

> **A station transmits.** Use a clear frequency and low power, and run it
> only where you are licensed to. Station identification and band-plan rules
> are your responsibility; FMlink frames are not encrypted.

## What you need

- **A computer running Linux.** Tested on Ubuntu 24.04 (Python 3.12) and
  Raspberry Pi OS / Debian 13 on a Raspberry Pi 5 (Python 3.13). Python
  3.11 or newer is required. Windows and macOS are not supported.
- **An FM radio** (an HT works well) and **a USB audio/PTT interface**:
  an AIOC, a Digirig or a SignaLink. A laptop's headphone jack and
  Bluetooth KISS TNCs do not work - see
  [RADIO_SETUP.md](RADIO_SETUP.md#what-you-need).
- A network connection for the install (the station itself needs none,
  except to reach its web page).

Without a radio you can still install everything and run two stations
against each other over the simulator (step 8).

## 1. System packages

```bash
sudo apt update
sudo apt install git python3-venv python3-numpy python3-scipy python3-serial alsa-utils
```

numpy and scipy come from the system so nothing has to be compiled (this
matters on a Pi). `alsa-utils` provides `arecord`/`aplay` and `alsamixer`.

## 2. Get the code

The three repos live side by side; the web interface finds the other two
as its neighbours.

```bash
mkdir -p ~/Projects && cd ~/Projects
git clone https://github.com/md-thomas/fmlink.git
git clone https://github.com/md-thomas/ota_sim.git
git clone https://github.com/md-thomas/fmlink_web.git
```

| Repo | What it is |
|---|---|
| `fmlink` | The modem, link layer, station (`ota_station.py`), KISS TNC and radio tools |
| `ota_sim` | The over-the-air simulator; the web interface needs it present even on a radio |
| `fmlink_web` | The browser interface for one station |

Elsewhere than `~/Projects/<repo>`? Set `FMLINK_SRC` and `OTA_SIM_SRC` to
the checkouts' paths.

## 3. The web interface's Python environment

```bash
cd ~/Projects/fmlink_web
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[test]'
```

This installs FastAPI and uvicorn 0.54 or newer into `.venv` (the older
uvicorn some distributions ship breaks the live updates). Nothing needs
Node: the built page is in the repo.

## 4. Permission to key the radio

Skip this for a SignaLink (VOX: it keys itself from the audio).

The AIOC and Digirig key the radio through a serial port that belongs to
the `dialout` group. Add yourself, then **log out and back in**:

```bash
sudo usermod -aG dialout $USER
```

Without it the station runs but never transmits, and logs
`[radio tx error] ... - burst NOT sent`.

## 5. Check the interface is found

Plug in the interface, then:

```bash
cd ~/Projects/fmlink
./radio_stations.py
# aioc       audio hw:CARD=AllInOneCable,DEV=0   PTT DTR usb-AIOC_All-In-One-Cable_...
```

It finds AIOC, Digirig and SignaLink interfaces by their USB IDs, so it
doesn't matter which USB port they are in.

## 6. Set the receive level

Do this on each station before its first run, and again after changing
the radio's volume or replugging anything. Open the radio's squelch so it
hisses, then run:

```bash
./level_meter.py
```

and turn the radio's volume until it says the level is good (hiss around
-15 dBFS). The details (Digirig AGC, transmit level, the mixer settings)
are in [RADIO_SETUP.md](RADIO_SETUP.md#audio-setup-and-leveling).

## 7. Run the station

```bash
cd ~/Projects/fmlink_web
./run.sh --station-id K0MDT-1 --dst K0MDT-2 --radio --tx-level 0.6
```

Use your own callsign for `--station-id` and the other station's for
`--dst`; the other station runs the same command with the two swapped.
Then open `http://<this computer's name or address>:8080` in a browser on
the same network. Ctrl-C stops the station.

**Anyone on your network can open the page and transmit** - there's no
password. Add `--host 127.0.0.1` to keep it to this computer.

To start it the same way every time without retyping the options, save
them once:

```bash
cp station.env.example station.env     # then edit RUN_ARGS in it
./run.sh
```

`station.env` is this station's own file (git ignores it). Options on the
command line replace it entirely. `./run.sh --help` lists every option.

## 8. Try it without a radio (optional)

Two stations on one computer, talking over the simulator. In three
terminals:

```bash
cd ~/Projects/ota_sim && python3 -m ota_sim.simulator --channel fm --port mic
cd ~/Projects/fmlink_web && ./run.sh --station-id TEST-1 --dst TEST-2 --sim-host 127.0.0.1
cd ~/Projects/fmlink_web && ./run.sh --station-id TEST-2 --dst TEST-1 --sim-host 127.0.0.1 --port 8081
```

Open `http://localhost:8080` and `http://localhost:8081`, and send a
message from one page to the other. (Start the simulator from inside the
`ota_sim` folder, as shown; elsewhere Python won't find it.)

## Other ways to run it

- **Without the web page:** `ota_station.py` in `fmlink` is the same
  station in a terminal, with typed commands. Only needs steps 1, 2
  (`fmlink` alone), 4, 5 and 6:

  ```bash
  cd ~/Projects/fmlink
  ./ota_station.py --station-id K0MDT-1 --dst K0MDT-2 --phy audio --audio-port mic --radio --ack --auto
  ```

- **As a KISS TNC for Winlink (Pat):** see
  [RADIO_SETUP.md](RADIO_SETUP.md#winlink-peer-to-peer-pat-over-fmlink)
  for the AX.25 setup.
- **Desktop app (simulator only):**
  [fmlink_ui](https://github.com/md-thomas/fmlink_ui).

## Updating

```bash
cd ~/Projects
for r in fmlink ota_sim fmlink_web; do git -C $r pull; done
cd fmlink_web && .venv/bin/pip install -e '.[test]'    # picks up any new requirements
```

Then restart the station. Both stations should run the same version.

## Checking the install

The test suites take a couple of minutes (about 2.5 on a Pi 5):

```bash
cd ~/Projects/fmlink && python3 -m unittest discover -s tests -q
cd ~/Projects/fmlink_web && .venv/bin/python -m unittest discover -s tests -q
```

## Troubleshooting the install

| Symptom | Cause and fix |
|---|---|
| `./run.sh`: `no venv at .../.venv` | Step 3 wasn't done in this folder. If you moved or renamed the folder, delete `.venv` and redo step 3: a venv doesn't survive a move. |
| `expected .../ota_sim to exist` | `ota_sim` isn't next to `fmlink_web` (step 2), or set `OTA_SIM_SRC`. |
| `No module named 'ota_sim'` when starting the simulator | Start it from inside `~/Projects/ota_sim` (step 8). |
| The page never updates live / websocket errors | An old uvicorn. Rerun `.venv/bin/pip install -e '.[test]'` in `fmlink_web`. |
| The station runs but never transmits | The `dialout` permission (step 4), and log out and back in after adding it. |
| `./radio_stations.py` finds nothing | Replug the interface; check `lsusb` lists it. |
| Messages don't get through | Almost always levels: redo step 6, then see [RADIO_SETUP.md](RADIO_SETUP.md#troubleshooting). |
| Can't open the page from another computer | Use the computer's address, not `localhost`; check a firewall allows port 8080; don't use `--host 127.0.0.1`. |
