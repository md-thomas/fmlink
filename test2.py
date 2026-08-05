#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: OpenWave pi/4-QPSK PHY Test
# Description: OpenWave phase 1 - pi/4-QPSK physical layer only, no framing yet
# GNU Radio version: 3.10.9.2

from PyQt5 import Qt
from gnuradio import qtgui
from gnuradio import blocks
from gnuradio import channels
from gnuradio.filter import firdes
from gnuradio import digital
from gnuradio import filter
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import gr, pdu
import sip
import test2_str_to_pdu_0 as str_to_pdu_0  # embedded python block
import test2_stream_display_sink_0 as stream_display_sink_0  # embedded python block



class test2(gr.top_block, Qt.QWidget):

    def __init__(self, msg="OpenWave pi/4-QPSK PHY test 123!", symbol_rate=4800):
        gr.top_block.__init__(self, "OpenWave pi/4-QPSK PHY Test", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("OpenWave pi/4-QPSK PHY Test")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("GNU Radio", "test2")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)

        ##################################################
        # Parameters
        ##################################################
        self.msg = msg
        self.symbol_rate = symbol_rate

        ##################################################
        # Variables
        ##################################################
        self.sps = sps = 4
        self.ring_points = ring_points = [1+0j, (2**0.5/2)+(2**0.5/2)*1j, 1j, -(2**0.5/2)+(2**0.5/2)*1j, -1+0j, -(2**0.5/2)-(2**0.5/2)*1j, -1j, (2**0.5/2)-(2**0.5/2)*1j]
        self.tx_const = tx_const = digital.constellation_calcdist(ring_points, [0, 1, 2, 3, 4, 5, 6, 7],
        8, 1, digital.constellation.NO_NORMALIZATION).base()
        self.tx_const.set_npwr(1.0)
        self.samp_rate = samp_rate = symbol_rate*sps
        self.decode_const = decode_const = digital.constellation_calcdist([ring_points[1], ring_points[3], ring_points[5], ring_points[7]], [0, 1, 2, 3],
        4, 1, digital.constellation.NO_NORMALIZATION).base()
        self.decode_const.set_npwr(1.0)

        ##################################################
        # Blocks
        ##################################################

        self.stream_display_sink_0 = stream_display_sink_0.blk(pad=8, bufsize=512)
        self.str_to_pdu_0 = str_to_pdu_0.blk(pad=48)
        self.qtgui_edit_box_msg_1 = qtgui.edit_box_msg(qtgui.STRING, "", 'Received', False, True, '', None)
        self._qtgui_edit_box_msg_1_win = sip.wrapinstance(self.qtgui_edit_box_msg_1.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_edit_box_msg_1_win, 1, 0, 1, 1)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_edit_box_msg_0 = qtgui.edit_box_msg(qtgui.STRING, msg, 'Send', False, True, '', None)
        self._qtgui_edit_box_msg_0_win = sip.wrapinstance(self.qtgui_edit_box_msg_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_edit_box_msg_0_win, 0, 0, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_const_sink_x_0 = qtgui.const_sink_c(
            256, #size
            "", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_const_sink_x_0.set_update_time(0.10)
        self.qtgui_const_sink_x_0.set_y_axis((-1.5), 1.5)
        self.qtgui_const_sink_x_0.set_x_axis((-1.5), 1.5)
        self.qtgui_const_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.qtgui_const_sink_x_0.enable_autoscale(False)
        self.qtgui_const_sink_x_0.enable_grid(False)
        self.qtgui_const_sink_x_0.enable_axis_labels(True)


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_const_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_const_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_const_sink_x_0.set_line_width(i, widths[i])
            self.qtgui_const_sink_x_0.set_line_color(i, colors[i])
            self.qtgui_const_sink_x_0.set_line_style(i, styles[i])
            self.qtgui_const_sink_x_0.set_line_marker(i, markers[i])
            self.qtgui_const_sink_x_0.set_line_alpha(i, alphas[i])

        self._qtgui_const_sink_x_0_win = sip.wrapinstance(self.qtgui_const_sink_x_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_const_sink_x_0_win, 2, 0, 1, 1)
        for r in range(2, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.pdu_pdu_to_tagged_stream_0 = pdu.pdu_to_tagged_stream(gr.types.byte_t, 'packet_len')
        self.filter_interp_fir_filter_xxx_0 = filter.interp_fir_filter_ccf(sps, firdes.root_raised_cosine(sps, sps, 1.0, 0.35, 11*sps))
        self.filter_interp_fir_filter_xxx_0.declare_sample_delay(0)
        self.digital_symbol_sync_xx_0 = digital.symbol_sync_cc(
            digital.TED_MOD_MUELLER_AND_MULLER,
            sps,
            0.045,
            1.0,
            1.0,
            1.5,
            1,
            tx_const,
            digital.IR_MMSE_8TAP,
            128,
            [])
        self.digital_map_bb_0 = digital.map_bb([1, 3, 5, 7])
        self.digital_diff_phasor_cc_0 = digital.diff_phasor_cc()
        self.digital_diff_encoder_bb_0 = digital.diff_encoder_bb(8, digital.DIFF_DIFFERENTIAL)
        self.digital_constellation_decoder_cb_0 = digital.constellation_decoder_cb(decode_const)
        self.digital_chunks_to_symbols_xx_0 = digital.chunks_to_symbols_bc(tx_const.points(), 1)
        self.channels_channel_model_0 = channels.channel_model(
            noise_voltage=0.001,
            frequency_offset=0.0,
            epsilon=1.0,
            taps=[1.0],
            noise_seed=0,
            block_tags=False)
        self.blocks_unpack_k_bits_bb_1 = blocks.unpack_k_bits_bb(2)
        self.blocks_unpack_k_bits_bb_0 = blocks.unpack_k_bits_bb(8)
        self.blocks_throttle2_0 = blocks.throttle( gr.sizeof_gr_complex*1, samp_rate, True, 0 if "auto" == "auto" else max( int(float(0.1) * samp_rate) if "auto" == "time" else int(0.1), 1) )
        self.blocks_pack_k_bits_bb_1 = blocks.pack_k_bits_bb(8)
        self.blocks_pack_k_bits_bb_0 = blocks.pack_k_bits_bb(2)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.qtgui_edit_box_msg_0, 'msg'), (self.str_to_pdu_0, 'msg'))
        self.msg_connect((self.str_to_pdu_0, 'pdus'), (self.pdu_pdu_to_tagged_stream_0, 'pdus'))
        self.msg_connect((self.stream_display_sink_0, 'msg'), (self.qtgui_edit_box_msg_1, 'val'))
        self.connect((self.blocks_pack_k_bits_bb_0, 0), (self.digital_map_bb_0, 0))
        self.connect((self.blocks_pack_k_bits_bb_1, 0), (self.stream_display_sink_0, 0))
        self.connect((self.blocks_throttle2_0, 0), (self.channels_channel_model_0, 0))
        self.connect((self.blocks_unpack_k_bits_bb_0, 0), (self.blocks_pack_k_bits_bb_0, 0))
        self.connect((self.blocks_unpack_k_bits_bb_1, 0), (self.blocks_pack_k_bits_bb_1, 0))
        self.connect((self.channels_channel_model_0, 0), (self.digital_symbol_sync_xx_0, 0))
        self.connect((self.digital_chunks_to_symbols_xx_0, 0), (self.filter_interp_fir_filter_xxx_0, 0))
        self.connect((self.digital_constellation_decoder_cb_0, 0), (self.blocks_unpack_k_bits_bb_1, 0))
        self.connect((self.digital_diff_encoder_bb_0, 0), (self.digital_chunks_to_symbols_xx_0, 0))
        self.connect((self.digital_diff_phasor_cc_0, 0), (self.digital_constellation_decoder_cb_0, 0))
        self.connect((self.digital_diff_phasor_cc_0, 0), (self.qtgui_const_sink_x_0, 0))
        self.connect((self.digital_map_bb_0, 0), (self.digital_diff_encoder_bb_0, 0))
        self.connect((self.digital_symbol_sync_xx_0, 0), (self.digital_diff_phasor_cc_0, 0))
        self.connect((self.filter_interp_fir_filter_xxx_0, 0), (self.blocks_throttle2_0, 0))
        self.connect((self.pdu_pdu_to_tagged_stream_0, 0), (self.blocks_unpack_k_bits_bb_0, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("GNU Radio", "test2")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_msg(self):
        return self.msg

    def set_msg(self, msg):
        self.msg = msg

    def get_symbol_rate(self):
        return self.symbol_rate

    def set_symbol_rate(self, symbol_rate):
        self.symbol_rate = symbol_rate
        self.set_samp_rate(self.symbol_rate*self.sps)

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps
        self.set_samp_rate(self.symbol_rate*self.sps)
        self.digital_symbol_sync_xx_0.set_sps(self.sps)
        self.filter_interp_fir_filter_xxx_0.set_taps(firdes.root_raised_cosine(self.sps, self.sps, 1.0, 0.35, 11*self.sps))

    def get_ring_points(self):
        return self.ring_points

    def set_ring_points(self, ring_points):
        self.ring_points = ring_points

    def get_tx_const(self):
        return self.tx_const

    def set_tx_const(self, tx_const):
        self.tx_const = tx_const

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.blocks_throttle2_0.set_sample_rate(self.samp_rate)

    def get_decode_const(self):
        return self.decode_const

    def set_decode_const(self, decode_const):
        self.decode_const = decode_const
        self.digital_constellation_decoder_cb_0.set_constellation(self.decode_const)



def argument_parser():
    description = 'OpenWave phase 1 - pi/4-QPSK physical layer only, no framing yet'
    parser = ArgumentParser(description=description)
    parser.add_argument(
        "-m", "--msg", dest="msg", type=str, default="OpenWave pi/4-QPSK PHY test 123!",
        help="Set msg [default=%(default)r]")
    parser.add_argument(
        "-r", "--symbol-rate", dest="symbol_rate", type=intx, default=4800,
        help="Set symbol_rate [default=%(default)r]")
    return parser


def main(top_block_cls=test2, options=None):
    if options is None:
        options = argument_parser().parse_args()

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(msg=options.msg, symbol_rate=options.symbol_rate)

    tb.start()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
