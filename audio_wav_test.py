#!/usr/bin/env python3
"""Offline over-the-air audio test: no real-time I/O needed.

  audio_wav_test.py gen    -m 0 -o tx.wav "hello"   # burst for the TX radio
  audio_wav_test.py decode -m 0 rx.wav              # decode a recording

Play tx.wav into a transmitting radio, record the receiving radio's audio
(e.g. arecord -D hw:AllInOneCable,0 -f S16_LE -r 48000 -c 1 -d 15 rx.wav),
then decode. The mode/FEC/port must match on both sides.
"""
import argparse
import wave

import numpy as np

import openwave_audio as audio
import openwave_fec as fec
import openwave_link as link
import openwave_modem as modem
from demodulate import DEMODULATORS
from modulate import MODULATORS

BITS = {'bpsk': 1, 'qpsk': 2, '16qam': 4}


def strip_padding(frame):
    marker = link.PREAMBLE + link.SYNC_WORD
    at = frame.find(marker) + len(marker)
    hdr = link.parse_header(frame[at:at + link.HEADER_LEN])
    return frame[frame.find(marker):at + link.HEADER_LEN + hdr['payload_len'] + link.CRC_LEN]


def mode_params(args):
    m = link.WAVEFORM_MODES[args.mode]
    gens, k = fec.PRESETS[args.fec or m['fec_preset']]
    prof = audio.PORT_PROFILES[args.port]
    return m['modulation'], m['symbol_rate'], gens, k, prof['carrier_hz']


def gen(args):
    mod, rate, gens, k, carrier = mode_params(args)
    frame = strip_padding(link.build_frame(args.text.encode(), mode_id=args.mode, src_id=args.src.encode(),
                                           dst_id=b'ALL'))
    symbols = MODULATORS[mod](fec.encode_frame(frame, gens, k))
    samples = audio.modulate(symbols, rate, carrier, level=args.level, lead_in_s=args.lead_in)
    pcm = audio.audio_to_pcm16(samples)
    with wave.open(args.output, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(audio.AUDIO_SAMPLE_RATE)
        w.writeframes(pcm)
    print(f"wrote {args.output}: {len(samples) / audio.AUDIO_SAMPLE_RATE:.2f} s, {mod} {rate} baud, "
          f"carrier {carrier:g} Hz, level {args.level}")


def decode(args):
    mod, rate, gens, k, carrier = mode_params(args)
    with wave.open(args.input) as w:
        assert w.getframerate() == audio.AUDIO_SAMPLE_RATE, "needs a 48 kHz recording"
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    x = audio.pcm16_to_audio(raw)
    if channels > 1:  # some sound cards only record stereo: average the channels
        x = x.reshape(-1, channels).mean(axis=1)
    print(f"{len(x) / audio.AUDIO_SAMPLE_RATE:.1f} s, rms {x.std():.3f}, peak {np.abs(x).max():.3f}")
    symbols, info = audio.demodulate(x, rate, carrier, MODULATORS[mod](link.PREAMBLE + link.SYNC_WORD),
                                     constellation=modem._CONSTELLATIONS[mod])
    print(f"sync quality {info['quality']:.2f} (>~0.5 good, <~0.15 nothing), SNR {info['snr_db']:.1f} dB")
    if info['quality'] < 0.15:
        print("no burst found")
        return
    n = 8 // BITS[mod]
    symbols = symbols[:len(symbols) - len(symbols) % n]
    bits = DEMODULATORS[mod](symbols)
    frames = list(fec.decode_frames(bits, gens, k, first_marker_bit=info['marker_index'] * BITS[mod]))
    for status, header, payload in frames:
        print(status, payload if status == 'ok' else '')
    if not frames:
        print("sync found but no frame decoded")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name, fn in (('gen', gen), ('decode', decode)):
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)
        s.add_argument('-m', '--mode', type=int, default=0, choices=sorted(link.WAVEFORM_MODES))
        s.add_argument('--fec', choices=sorted(fec.PRESETS), help="default: the mode's own preset")
        s.add_argument('--port', choices=sorted(audio.PORT_PROFILES), default='mic')
        if name == 'gen':
            s.add_argument('text')
            s.add_argument('--src', default='TEST', help='source ID in the frame header (e.g. a callsign)')
            s.add_argument('-o', '--output', default='tx.wav')
            s.add_argument('--level', type=float, default=0.5)
            s.add_argument('--lead-in', type=float, default=0.5, help='seconds of silence first')
        else:
            s.add_argument('input')
    args = p.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
