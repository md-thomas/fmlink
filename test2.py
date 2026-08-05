#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
OpenWave phase 1: pi/4-QPSK physical layer only.

No preamble/sync-word/header/CRC/FEC yet (that's link-layer, deferred to a
later phase) - this just proves the pi/4-QPSK modem itself works: modulate,
push through a simulated noisy channel, demodulate, recover the original
bytes.

Why pi/4-QPSK isn't "just QPSK with a rotated constellation":
GNU Radio's stock constellations (qpsk, dqpsk) are both single, fixed
4-point constellations - dqpsk is literally the same 4 points as qpsk, just
listed in a different order. Real pi/4-QPSK alternates between two QPSK
constellations 45 degrees apart, symbol to symbol: every phase transition is
an odd multiple of 45 degrees (+-45 or +-135), never a multiple of 90. That
alternation is implemented here as a trick rather than a dedicated block:

  - Treat the union of both 45-degree-apart constellations as a single
    8-point, 45-degree-spaced ring (points at 0,45,90,...,315 degrees).
  - Map each 2-bit symbol to one of the four ODD ring positions (1,3,5,7)
    via map_bb - the even positions (0,2,4,6) are never used directly as
    input.
  - Differentially encode those odd values modulo 8 (diff_encoder_bb).
    Accumulating an odd value mod 8 always flips the result's parity, so
    the transmitted point alternates between the "even" and "odd" halves
    of the ring every symbol - exactly the alternating-constellation
    behavior that defines pi/4-QPSK.

On the receive side, this pays off: differentially decoding by phase
difference (diff_phasor_cc) between consecutive received symbols recovers
the original odd-position increment directly, and is inherently immune to
any constant carrier phase offset - no Costas loop is needed the way plain
QPSK needs one. It is NOT immune to a moving frequency offset, though: a
non-zero, accumulating frequency error would bias every phase-difference
measurement. The simulated channel below uses frequency_offset=0.0, so this
doesn't show up here - if frequency offset modeling is added later, this
design would need revisiting (either an AFC stage, or accept the resulting
bias).
"""

import argparse

import numpy as np
from gnuradio import analog, blocks, channels, digital, filter as gr_filter, gr
from gnuradio.filter import firdes


def build_pi4_constellations():
    """Returns (tx_constellation, decode_constellation).

    tx_constellation: all 8 ring points, 0/45/../315 degrees, in sequential
    index order - used as the modulator's symbol table and as Symbol Sync's
    decision-directed slicer.

    decode_constellation: just the 4 odd (45/135/225/315) positions, mapped
    back to their original 2-bit symbol values - used to slice the
    diff_phasor_cc output straight back to data, with no separate
    differential-decode step needed.
    """
    angles = np.arange(8) * (np.pi / 4)
    ring_points = [complex(np.cos(a), np.sin(a)) for a in angles]

    tx_constellation = digital.constellation_calcdist(
        ring_points, list(range(8)), 8, 1,
        digital.constellation.NO_NORMALIZATION).base()

    odd_points = [ring_points[1], ring_points[3], ring_points[5], ring_points[7]]
    decode_constellation = digital.constellation_calcdist(
        odd_points, [0, 1, 2, 3], 4, 1,
        digital.constellation.NO_NORMALIZATION).base()

    return tx_constellation, decode_constellation


class Pi4QpskModem(gr.top_block):
    def __init__(self, test_text="OpenWave pi/4-QPSK PHY test", symbol_rate=4800):
        gr.top_block.__init__(self, "OpenWave pi/4-QPSK PHY Test")

        if symbol_rate not in (600, 1200, 2400, 4800, 9600):
            raise ValueError(
                f"symbol_rate must be one of the OpenWave modes "
                f"(600, 1200, 2400, 4800, 9600), got {symbol_rate}"
            )

        # --- Parameters ---
        self.sps = 4
        self.symbol_rate = symbol_rate
        self.samp_rate = symbol_rate * self.sps

        self.tx_const, self.decode_const = build_pi4_constellations()

        # ---------------------------------------------------------------
        # TRANSMITTER (TX)
        # ---------------------------------------------------------------
        # Padded with dummy bytes on each end to absorb sync-loop
        # acquisition transients and filter group delay, same reasoning as
        # the QPSK modem test.
        pad = [0x00] * 48
        data_bytes = pad + list(test_text.encode('utf-8')) + pad
        self.src = blocks.vector_source_b(data_bytes, False)

        self.unpack_tx = blocks.unpack_k_bits_bb(8)          # byte -> bits
        self.pack_tx_symbols = blocks.pack_k_bits_bb(2)       # bits -> 2-bit symbols (0..3)
        self.to_odd = digital.map_bb([1, 3, 5, 7])            # symbol -> odd ring position
        self.diff_enc = digital.diff_encoder_bb(8, digital.DIFF_DIFFERENTIAL)  # mod-8 phase accumulator

        self.modulator = digital.chunks_to_symbols_bc(self.tx_const.points(), 1)
        self.tx_rrc = gr_filter.interp_fir_filter_ccf(
            self.sps,
            firdes.root_raised_cosine(1.0, self.sps, 1.0, 0.35, 11 * self.sps)
        )
        self.throttle = blocks.throttle(gr.sizeof_gr_complex, self.samp_rate, True)

        # ---------------------------------------------------------------
        # CHANNEL SIMULATION
        # ---------------------------------------------------------------
        self.channel = channels.channel_model(
            noise_voltage=0.001,
            frequency_offset=0.0,
            epsilon=1.0,
            taps=[1.0]
        )

        # ---------------------------------------------------------------
        # RECEIVER (RX)
        # ---------------------------------------------------------------
        # 1. Symbol timing recovery. The slicer is the full 8-point ring so
        #    the timing-error detector's decisions match the actual
        #    transmitted point set.
        self.symbol_sync = digital.symbol_sync_cc(
            digital.TED_MOD_MUELLER_AND_MULLER,
            sps=self.sps,
            loop_bw=0.045,
            damping_factor=1.0,
            ted_gain=1.0,
            max_deviation=1.5,
            osps=1,
            slicer=self.tx_const,
            interp_type=digital.IR_MMSE_8TAP,
            n_filters=128,
            taps=[],
        )

        # 2. Differential phase detection - recovers the original odd-ring
        #    increment directly from the phase difference between
        #    consecutive symbols. No Costas loop, no separate differential
        #    decoder: this one block does the job of both for pi/4-QPSK.
        self.diff_dec = digital.diff_phasor_cc()

        # 3. Slice straight back to the 2-bit symbol value.
        self.slicer = digital.constellation_decoder_cb(self.decode_const)

        # 4. Bits back into bytes.
        self.unpack_rx_bits = blocks.unpack_k_bits_bb(2)
        self.pack_rx = blocks.pack_k_bits_bb(8)

        # 5. Output sink.
        self.sink = blocks.vector_sink_b()

        # ---------------------------------------------------------------
        # CONNECTIONS
        # ---------------------------------------------------------------
        self.connect(self.src, self.unpack_tx, self.pack_tx_symbols, self.to_odd,
                     self.diff_enc, self.modulator, self.tx_rrc, self.throttle)
        self.connect(self.throttle, self.channel)
        self.connect(self.channel, self.symbol_sync, self.diff_dec, self.slicer,
                     self.unpack_rx_bits, self.pack_rx, self.sink)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="OpenWave pi/4-QPSK PHY loopback test")
    parser.add_argument('-m', '--msg', default="OpenWave pi/4-QPSK PHY test 123!",
                         help="message text to transmit")
    parser.add_argument('-r', '--rate', type=int, default=4800,
                         help="symbol rate in baud - one of 600/1200/2400/4800/9600 (default: 4800)")
    args = parser.parse_args()

    msg = args.msg
    print(f"[TX] Original Message: '{msg}' @ {args.rate} baud (pi/4-QPSK)")

    tb = Pi4QpskModem(test_text=msg, symbol_rate=args.rate)
    tb.start()
    tb.wait()

    rx_bytes = bytes(tb.sink.data())

    payload = msg.encode('utf-8')
    idx = rx_bytes.find(payload)

    if idx != -1:
        print(f"[RX] Recovered Message: '{rx_bytes[idx:idx+len(payload)].decode('utf-8')}'")
    else:
        decoded_text = rx_bytes.decode('utf-8', errors='ignore')
        print(f"[RX] Exact match not found. Raw decode: '{decoded_text}'")
