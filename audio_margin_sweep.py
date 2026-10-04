#!/usr/bin/env python3
"""Where does a mode stop decoding? Adds white noise to real audio bursts and
reports, per noise level, the data-aided SNR the station would log and how
many bursts were delivered. Software only: nothing is transmitted.

  ./audio_margin_sweep.py -m 1 2 --fec r1_2_k3
  ./audio_margin_sweep.py -m 0 --fec r1_4_k9 --sigmas 1.3 1.6 2.0 --trials 30

Measured 2026-10-04 (mic port, 30-character message, 24 bursts per point):
mode 0 r1_4_k9 breaks near -1 dB, mode 1 r1_2_k3 near 4.5 dB, mode 2 r1_2_k3
near 7 dB. At the same noise, mode 1 reads ~3.1 dB below mode 0 and mode 2
~2.5 dB below - the doubled noise bandwidth; see
openwave_link.select_mode_for_snr(probe_symbol_rate=...).
"""
import argparse

import numpy as np

import audio_wav_test as awt
import openwave_fec as fec
import openwave_link as link


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('-m', '--modes', type=int, nargs='+', default=[0, 1, 2], choices=sorted(link.WAVEFORM_MODES))
    p.add_argument('--fec', choices=sorted(fec.PRESETS), default='r1_2_k3')
    p.add_argument('--port', choices=('mic', 'data'), default='mic')
    p.add_argument('--text', default='K0MDT OpenWave test mode X #1', help='message to send')
    p.add_argument('--sigmas', type=float, nargs='+',
                   default=[0.10, 0.19, 0.27, 0.38, 0.55, 0.65, 0.78, 0.92, 1.1, 1.3, 1.6],
                   help='noise rms added to the audio (burst peak is 0.5)')
    p.add_argument('--trials', type=int, default=24)
    args = p.parse_args()

    rng = np.random.default_rng(7)
    for mode in args.modes:
        print(f"--- mode {mode} {args.fec}")
        for sigma in args.sigmas:
            ok, snrs = 0, []
            for _ in range(args.trials):
                s = awt.build_samples(args.text, mode, args.fec, args.port, src='K0MDT', level=0.5)
                x = np.concatenate([np.zeros(48000), s, np.zeros(48000)])
                r = awt.decode_samples(x + rng.normal(0, sigma, len(x)), mode, args.fec, args.port)
                ok += r['payload'] == args.text.encode()
                if r['quality'] >= 0.15 and np.isfinite(r['snr_db']):
                    snrs.append(r['snr_db'])
            print(f"  sigma {sigma:.2f}: SNR {np.mean(snrs) if snrs else float('nan'):5.1f} dB  "
                  f"delivered {ok}/{args.trials}", flush=True)


if __name__ == '__main__':
    main()
