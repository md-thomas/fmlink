#!/usr/bin/env python3
"""Find the radio interfaces (AIOC, Digirig, ...) attached to this computer
and to the other computers listed in stations.toml, and describe each as a
station: where it is, its ALSA audio device and its PTT serial port.

  ./radio_stations.py          # list what's detected

Stations are named by interface ('aioc', 'digirig'); if the same interface
exists on two computers, use 'aioc@macbookair2' to say which.
"""
import os
import re
import socket
import subprocess
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, 'stations.toml')
LOCAL = 'localhost'

# How to recognise each interface: its USB sound card (ALSA card id or
# name) and its serial PTT port (a /dev/serial/by-id name pattern) - or, for
# an interface with no PTT line at all (e.g. the SignaLink: no serial port,
# no CM108-style GPIO on its HID endpoint either - see RADIO_SETUP.md), just
# 'ptt_line': 'vox' and no 'serial' entry. The radio keys itself off the
# transmitted audio, so there's nothing for this software to assert/release.
PROFILES = {
    'aioc': {'card_id': 'AllInOneCable', 'serial': re.compile(r'AIOC', re.I), 'ptt_line': 'dtr'},
    'digirig': {'card_name': 'USB PnP Sound Device', 'serial': re.compile(r'CP210', re.I),
                'ptt_line': 'rts'},
    'signalink': {'card_name': 'USB Audio CODEC', 'ptt_line': 'vox'},
}
SCAN_CMD = "arecord -l 2>/dev/null; echo ---; ls /dev/serial/by-id 2>/dev/null"


def is_local(host):
    return host in (LOCAL, socket.gethostname())


def load_config(path=DEFAULT_CONFIG):
    if not os.path.exists(path):
        return {}
    with open(path, 'rb') as f:
        return tomllib.load(f)


def _run(host, command):
    argv = ['sh', '-c', command] if is_local(host) else \
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', host, command]
    return subprocess.run(argv, capture_output=True, text=True, timeout=20,
                          stdin=subprocess.DEVNULL).stdout


def detect(host, overrides=None):
    """Stations on one computer (host LOCAL or an ssh name)."""
    overrides = overrides or {}
    cards_text, _, serial_text = _run(host, SCAN_CMD).partition('---')
    cards = re.findall(r'^card \d+: (\S+) \[(.*?)\]', cards_text, re.M)
    ports = [p for p in serial_text.split() if p]
    found = []
    for name, prof in PROFILES.items():
        card = next((cid for cid, cname in cards
                     if cid == prof.get('card_id') or cname.strip() == prof.get('card_name')), None)
        if card is None:
            continue
        port = next((f'/dev/serial/by-id/{p}' for p in ports
                     if prof.get('serial') and prof['serial'].search(p)), None)
        st = {'name': name, 'host': host,
              # plughw: converts mono to the stereo some sound cards (the CM108) insist on
              'tx_device': f'plughw:CARD={card},DEV=0',
              'rx_device': f'hw:CARD={card},DEV=0', 'ptt_port': port,
              'ptt_line': prof['ptt_line'], 'rx_channels': 1}
        st.update(overrides.get(name, {}))
        found.append(st)
    return found


def find_stations(config_path=DEFAULT_CONFIG, local_only=False):
    """Local stations first, then those on the [scan] hosts. Unreachable
    hosts are skipped with a warning."""
    config = load_config(config_path)
    overrides = config.get('interfaces', {})
    stations = detect(LOCAL, overrides)
    if not local_only:
        for host in config.get('scan', {}).get('hosts', []):
            if is_local(host):
                continue  # the same config file works from every machine
            try:
                stations += detect(host, overrides)
            except (subprocess.SubprocessError, OSError) as exc:
                print(f"warning: could not scan {host}: {exc}")
    return stations


def label(st):
    """'aioc' for this computer's, 'aioc@host' for another's."""
    return st['name'] if is_local(st['host']) else f"{st['name']}@{st['host']}"


def resolve(name, stations):
    """A station by 'aioc' or 'aioc@host'. Raises ValueError if unknown/ambiguous."""
    matches = [s for s in stations if name in (label(s), f"{s['name']}@{s['host']}")]
    if not matches:
        matches = [s for s in stations if s['name'] == name]
    have = ', '.join(label(s) for s in stations) or 'nothing detected'
    if not matches:
        raise ValueError(f"no station {name!r} (detected: {have})")
    if len(matches) > 1:
        raise ValueError(f"{name!r} is ambiguous - use one of: {', '.join(label(s) for s in matches)}")
    return matches[0]


def pick_local(name=None, config_path=DEFAULT_CONFIG):
    """This computer's station: the one named, or the only one attached.
    Raises ValueError if there's none or it's ambiguous."""
    local = detect(LOCAL, load_config(config_path).get('interfaces', {}))
    if name:
        matches = [s for s in local if s['name'] == name.split('@')[0]]
        if not matches:
            raise ValueError(f"no {name!r} on this computer (here: "
                             f"{', '.join(s['name'] for s in local) or 'no radio interface found'})")
        return matches[0]
    if len(local) != 1:
        raise ValueError("name the station to use (here: "
                         + (', '.join(s['name'] for s in local) or 'no radio interface found') + ")")
    return local[0]


def describe(st):
    if st['ptt_line'] == 'vox':
        port = 'audio-triggered, no PTT line'
    else:
        port = st['ptt_port'] or 'NO PTT PORT FOUND (receive only)'
    return (f"{label(st):<22} audio {st['rx_device']:<26} PTT {st['ptt_line'].upper()} "
            f"{port.replace('/dev/serial/by-id/', '')[:48]}")


def main():
    stations = find_stations()
    if not stations:
        print("no radio interfaces detected")
        return 1
    for st in stations:
        print(describe(st))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
