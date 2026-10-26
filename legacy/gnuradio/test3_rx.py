#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: FMlink RX (RTL-SDR)
# Description: FMlink hardware bring-up - RX half, RTL-SDR.
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import blocks
from gnuradio import digital
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
import osmosdr
import time
import sip
import test3_rx_stream_display_sink_0 as stream_display_sink_0  # embedded python block
import threading



class test3_rx(gr.top_block, Qt.QWidget):

    def __init__(self, center_freq_default=433000000, rtl_gain=40.0, rtl_ppm=0.0, symbol_rate=600):
        gr.top_block.__init__(self, "FMlink RX (RTL-SDR)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("FMlink RX (RTL-SDR)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "test3_rx")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Parameters
        ##################################################
        self.center_freq_default = center_freq_default
        self.rtl_gain = rtl_gain
        self.rtl_ppm = rtl_ppm
        self.symbol_rate = symbol_rate

        ##################################################
        # Variables
        ##################################################
        self.sps = sps = 4
        self.ring_points = ring_points = [1+0j, (2**0.5/2)+(2**0.5/2)*1j, 1j, -(2**0.5/2)+(2**0.5/2)*1j, -1+0j, -(2**0.5/2)-(2**0.5/2)*1j, -1j, (2**0.5/2)-(2**0.5/2)*1j]
        self.radio_rate = radio_rate = 1920000
        self.center_freq_mhz = center_freq_mhz = center_freq_default/1e6
        self.baseband_rate = baseband_rate = symbol_rate*sps
        self.tx_const = tx_const = digital.constellation_calcdist(ring_points, [0, 1, 2, 3, 4, 5, 6, 7],
        8, 1, digital.constellation.NO_NORMALIZATION).base()
        self.tx_const.set_npwr(1.0)
        self.resamp_factor = resamp_factor = radio_rate // baseband_rate
        self.nfilters = nfilters = 128
        self.freq_offset = freq_offset = 100000
        self.decode_const = decode_const = digital.constellation_calcdist([ring_points[1], ring_points[3], ring_points[5], ring_points[7]], [0, 1, 2, 3],
        4, 1, digital.constellation.NO_NORMALIZATION).base()
        self.decode_const.set_npwr(1.0)
        self.center_freq = center_freq = int(center_freq_mhz*1e6)

        ##################################################
        # Blocks
        ##################################################

        self.stream_display_sink_0 = stream_display_sink_0.blk(term_len=8, max_payload_bytes=256, tagname='sync')
        self.qtgui_freq_sink_x_0 = qtgui.freq_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            (center_freq - freq_offset), #fc
            radio_rate, #bw
            "", #name
            1,
            None # parent
        )
        self.qtgui_freq_sink_x_0.set_update_time(0.10)
        self.qtgui_freq_sink_x_0.set_y_axis((-140), 10)
        self.qtgui_freq_sink_x_0.set_y_label('Relative Gain', 'dB')
        self.qtgui_freq_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.qtgui_freq_sink_x_0.enable_autoscale(False)
        self.qtgui_freq_sink_x_0.enable_grid(False)
        self.qtgui_freq_sink_x_0.set_fft_average(1.0)
        self.qtgui_freq_sink_x_0.enable_axis_labels(True)
        self.qtgui_freq_sink_x_0.enable_control_panel(False)
        self.qtgui_freq_sink_x_0.set_fft_window_normalized(False)



        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_freq_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_freq_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_freq_sink_x_0.set_line_width(i, widths[i])
            self.qtgui_freq_sink_x_0.set_line_color(i, colors[i])
            self.qtgui_freq_sink_x_0.set_line_alpha(i, alphas[i])

        self._qtgui_freq_sink_x_0_win = sip.wrapinstance(self.qtgui_freq_sink_x_0.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_freq_sink_x_0_win, 2, 0, 1, 1)
        for r in range(2, 3):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.qtgui_edit_box_msg_1 = qtgui.edit_box_msg(qtgui.STRING, "", 'Received', False, True, '', None)
        self._qtgui_edit_box_msg_1_win = sip.wrapinstance(self.qtgui_edit_box_msg_1.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._qtgui_edit_box_msg_1_win, 1, 0, 1, 1)
        for r in range(1, 2):
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
        self.top_grid_layout.addWidget(self._qtgui_const_sink_x_0_win, 3, 0, 1, 1)
        for r in range(3, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.osmosdr_source_0 = osmosdr.source(
            args="numchan=" + str(1) + " " + 'rtl=0'
        )
        self.osmosdr_source_0.set_time_unknown_pps(osmosdr.time_spec_t())
        self.osmosdr_source_0.set_sample_rate(radio_rate)
        self.osmosdr_source_0.set_center_freq((center_freq - freq_offset), 0)
        self.osmosdr_source_0.set_freq_corr(rtl_ppm, 0)
        self.osmosdr_source_0.set_dc_offset_mode(0, 0)
        self.osmosdr_source_0.set_iq_balance_mode(0, 0)
        self.osmosdr_source_0.set_gain_mode(False, 0)
        self.osmosdr_source_0.set_gain(rtl_gain, 0)
        self.osmosdr_source_0.set_if_gain(30, 0)
        self.osmosdr_source_0.set_bb_gain(20, 0)
        self.osmosdr_source_0.set_antenna('', 0)
        self.osmosdr_source_0.set_bandwidth(0, 0)
        self.filter_freq_xlating_fir_filter_xxx_0 = filter.freq_xlating_fir_filter_ccf(resamp_factor, firdes.low_pass(1.0, radio_rate, baseband_rate*0.45, baseband_rate*0.35), freq_offset, radio_rate)
        self.digital_symbol_sync_xx_0 = digital.symbol_sync_cc(
            digital.TED_MOD_MUELLER_AND_MULLER,
            sps,
            0.045,
            1.0,
            1.0,
            1.5,
            1,
            tx_const,
            digital.IR_PFB_MF,
            nfilters,
            firdes.root_raised_cosine(nfilters, sps*nfilters, 1.0, 0.35, 11*sps*nfilters))
        self.digital_diff_phasor_cc_0 = digital.diff_phasor_cc()
        self.digital_correlate_access_code_tag_xx_0 = digital.correlate_access_code_tag_bb(''.join(format(b, '08b') for b in (b'\x55'*4 + b'\x1A\xCF\xFC\x1D')), 4, "sync")
        self.digital_constellation_decoder_cb_0 = digital.constellation_decoder_cb(decode_const)
        self._center_freq_mhz_range = qtgui.Range(24.0, 1766.0, 0.001, center_freq_default/1e6, 200)
        self._center_freq_mhz_win = qtgui.RangeWidget(self._center_freq_mhz_range, self.set_center_freq_mhz, "Center Freq (MHz)", "eng_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._center_freq_mhz_win, 0, 0, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.blocks_unpack_k_bits_bb_1 = blocks.unpack_k_bits_bb(2)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.stream_display_sink_0, 'msg'), (self.qtgui_edit_box_msg_1, 'val'))
        self.connect((self.blocks_unpack_k_bits_bb_1, 0), (self.digital_correlate_access_code_tag_xx_0, 0))
        self.connect((self.digital_constellation_decoder_cb_0, 0), (self.blocks_unpack_k_bits_bb_1, 0))
        self.connect((self.digital_correlate_access_code_tag_xx_0, 0), (self.stream_display_sink_0, 0))
        self.connect((self.digital_diff_phasor_cc_0, 0), (self.digital_constellation_decoder_cb_0, 0))
        self.connect((self.digital_diff_phasor_cc_0, 0), (self.qtgui_const_sink_x_0, 0))
        self.connect((self.digital_symbol_sync_xx_0, 0), (self.digital_diff_phasor_cc_0, 0))
        self.connect((self.filter_freq_xlating_fir_filter_xxx_0, 0), (self.digital_symbol_sync_xx_0, 0))
        self.connect((self.osmosdr_source_0, 0), (self.filter_freq_xlating_fir_filter_xxx_0, 0))
        self.connect((self.osmosdr_source_0, 0), (self.qtgui_freq_sink_x_0, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "test3_rx")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_center_freq_default(self):
        return self.center_freq_default

    def set_center_freq_default(self, center_freq_default):
        self.center_freq_default = center_freq_default
        self.set_center_freq_mhz(self.center_freq_default/1e6)

    def get_rtl_gain(self):
        return self.rtl_gain

    def set_rtl_gain(self, rtl_gain):
        self.rtl_gain = rtl_gain
        self.osmosdr_source_0.set_gain(self.rtl_gain, 0)

    def get_rtl_ppm(self):
        return self.rtl_ppm

    def set_rtl_ppm(self, rtl_ppm):
        self.rtl_ppm = rtl_ppm
        self.osmosdr_source_0.set_freq_corr(self.rtl_ppm, 0)

    def get_symbol_rate(self):
        return self.symbol_rate

    def set_symbol_rate(self, symbol_rate):
        self.symbol_rate = symbol_rate
        self.set_baseband_rate(self.symbol_rate*self.sps)

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps
        self.set_baseband_rate(self.symbol_rate*self.sps)
        self.digital_symbol_sync_xx_0.set_sps(self.sps)

    def get_ring_points(self):
        return self.ring_points

    def set_ring_points(self, ring_points):
        self.ring_points = ring_points

    def get_radio_rate(self):
        return self.radio_rate

    def set_radio_rate(self, radio_rate):
        self.radio_rate = radio_rate
        self.set_resamp_factor(self.radio_rate // self.baseband_rate)
        self.filter_freq_xlating_fir_filter_xxx_0.set_taps(firdes.low_pass(1.0, self.radio_rate, self.baseband_rate*0.45, self.baseband_rate*0.35))
        self.osmosdr_source_0.set_sample_rate(self.radio_rate)
        self.qtgui_freq_sink_x_0.set_frequency_range((self.center_freq - self.freq_offset), self.radio_rate)

    def get_center_freq_mhz(self):
        return self.center_freq_mhz

    def set_center_freq_mhz(self, center_freq_mhz):
        self.center_freq_mhz = center_freq_mhz
        self.set_center_freq(int(self.center_freq_mhz*1e6))

    def get_baseband_rate(self):
        return self.baseband_rate

    def set_baseband_rate(self, baseband_rate):
        self.baseband_rate = baseband_rate
        self.set_resamp_factor(self.radio_rate // self.baseband_rate)
        self.filter_freq_xlating_fir_filter_xxx_0.set_taps(firdes.low_pass(1.0, self.radio_rate, self.baseband_rate*0.45, self.baseband_rate*0.35))

    def get_tx_const(self):
        return self.tx_const

    def set_tx_const(self, tx_const):
        self.tx_const = tx_const

    def get_resamp_factor(self):
        return self.resamp_factor

    def set_resamp_factor(self, resamp_factor):
        self.resamp_factor = resamp_factor

    def get_nfilters(self):
        return self.nfilters

    def set_nfilters(self, nfilters):
        self.nfilters = nfilters

    def get_freq_offset(self):
        return self.freq_offset

    def set_freq_offset(self, freq_offset):
        self.freq_offset = freq_offset
        self.filter_freq_xlating_fir_filter_xxx_0.set_center_freq(self.freq_offset)
        self.osmosdr_source_0.set_center_freq((self.center_freq - self.freq_offset), 0)
        self.qtgui_freq_sink_x_0.set_frequency_range((self.center_freq - self.freq_offset), self.radio_rate)

    def get_decode_const(self):
        return self.decode_const

    def set_decode_const(self, decode_const):
        self.decode_const = decode_const
        self.digital_constellation_decoder_cb_0.set_constellation(self.decode_const)

    def get_center_freq(self):
        return self.center_freq

    def set_center_freq(self, center_freq):
        self.center_freq = center_freq
        self.osmosdr_source_0.set_center_freq((self.center_freq - self.freq_offset), 0)
        self.qtgui_freq_sink_x_0.set_frequency_range((self.center_freq - self.freq_offset), self.radio_rate)



def argument_parser():
    description = 'FMlink hardware bring-up - RX half, RTL-SDR.'
    parser = ArgumentParser(description=description)
    parser.add_argument(
        "-f", "--center-freq-default", dest="center_freq_default", type=intx, default=433000000,
        help="Set center_freq_default [default=%(default)r]")
    parser.add_argument(
        "-g", "--rtl-gain", dest="rtl_gain", type=eng_float, default=eng_notation.num_to_str(float(40.0)),
        help="Set rtl_gain [default=%(default)r]")
    parser.add_argument(
        "-c", "--rtl-ppm", dest="rtl_ppm", type=eng_float, default=eng_notation.num_to_str(float(0.0)),
        help="Set rtl_ppm [default=%(default)r]")
    parser.add_argument(
        "-r", "--symbol-rate", dest="symbol_rate", type=intx, default=600,
        help="Set symbol_rate [default=%(default)r]")
    return parser


def main(top_block_cls=test3_rx, options=None):
    if options is None:
        options = argument_parser().parse_args()

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls(center_freq_default=options.center_freq_default, rtl_gain=options.rtl_gain, rtl_ppm=options.rtl_ppm, symbol_rate=options.symbol_rate)

    tb.start()
    tb.flowgraph_started.set()

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
