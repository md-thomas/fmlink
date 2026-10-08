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


def mode_params(mode, fec_preset=None, port='mic'):
    m = link.WAVEFORM_MODES[mode]
    gens, k = fec.PRESETS[fec_preset or m['fec_preset']]
    return m['modulation'], m['symbol_rate'], gens, k, audio.carrier_for(port, m['symbol_rate'])


def build_samples(text, mode=0, fec_preset=None, port='mic', src='TEST', level=0.5, lead_in=0.5):
    """Message text -> audio samples (float, 48 kHz) for one burst."""
    mod, rate, gens, k, carrier = mode_params(mode, fec_preset, port)
    frame = audio.frame_for_audio(link.build_frame(text.encode(), mode_id=mode, src_id=src.encode(),
                                                   dst_id=b'ALL'))
    symbols = MODULATORS[mod](fec.encode_frame(frame, gens, k, marker=link.AUDIO_MARKER))
    return audio.modulate(symbols, rate, carrier, level=level, lead_in_s=lead_in)


def read_wav(path):
    """48 kHz WAV -> mono float samples (stereo is averaged: some sound
    cards only record stereo)."""
    with wave.open(path) as w:
        assert w.getframerate() == audio.AUDIO_SAMPLE_RATE, "needs a 48 kHz recording"
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    x = audio.pcm16_to_audio(raw)
    return x.reshape(-1, channels).mean(axis=1) if channels > 1 else x


def decode_samples(x, mode=0, fec_preset=None, port='mic'):
    """Recorded audio -> dict: quality, snr_db, status ('ok', a decode
    failure status, 'no_frame' or 'no_burst'), payload (bytes or None),
    header (parsed header of that frame, or None), timing (sample index of
    the burst's marker, None if no burst), plus the recording's rms and peak."""
    mod, rate, gens, k, carrier = mode_params(mode, fec_preset, port)
    out = {'rms': float(x.std()), 'peak': float(np.abs(x).max()), 'quality': 0.0,
           'snr_db': float('-inf'), 'status': 'no_burst', 'payload': None, 'header': None,
           'timing': None}
    symbols, info = audio.demodulate(x, rate, carrier, MODULATORS[mod](link.AUDIO_MARKER),
                                     constellation=modem._CONSTELLATIONS[mod])
    out['quality'], out['snr_db'] = float(info['quality']), float(info['snr_db'])
    out['timing'] = info['timing']
    if info['quality'] < 0.15:
        return out
    n = 8 // BITS[mod]
    symbols = symbols[:len(symbols) - len(symbols) % n]
    bits = DEMODULATORS[mod](symbols)
    frames = list(fec.decode_frames(bits, gens, k, first_marker_bit=info['marker_index'] * BITS[mod],
                                    marker=link.AUDIO_MARKER))
    out['status'] = 'no_frame'
    for status, header, payload in frames:
        out['status'] = status
        if status == 'ok':
            out['payload'], out['header'] = payload, header
            break
    return out


def gen(args):
    samples = build_samples(args.text, args.mode, args.fec, args.port, args.src, args.level, args.lead_in)
    mod, rate, _, _, carrier = mode_params(args.mode, args.fec, args.port)
    with wave.open(args.output, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(audio.AUDIO_SAMPLE_RATE)
        w.writeframes(audio.audio_to_pcm16(samples))
    print(f"wrote {args.output}: {len(samples) / audio.AUDIO_SAMPLE_RATE:.2f} s, {mod} {rate} baud, "
          f"carrier {carrier:g} Hz, level {args.level}")


def decode(args):
    x = read_wav(args.input)
    r = decode_samples(x, args.mode, args.fec, args.port)
    print(f"{len(x) / audio.AUDIO_SAMPLE_RATE:.1f} s, rms {r['rms']:.3f}, peak {r['peak']:.3f}")
    print(f"sync quality {r['quality']:.2f} (>~0.5 good, <~0.15 nothing), SNR {r['snr_db']:.1f} dB")
    if r['status'] == 'no_burst':
        print("no burst found")
    elif r['status'] == 'no_frame':
        print("sync found but no frame decoded")
    else:
        print(r['status'], r['payload'] if r['status'] == 'ok' else '')


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
