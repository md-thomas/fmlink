"""
Scans a rolling buffer of the received byte stream for the zero-padding
markers placed around each message on the TX side, and publishes the
framed text as a message whenever it changes. Avoids relying on stream
tags, which don't survive the adaptive symbol-sync/Costas loop chain
precisely enough for an isolated one-shot burst.
"""
import numpy as np
from gnuradio import gr
import pmt


class blk(gr.sync_block):
    """Rolling-buffer byte stream to message"""

    def __init__(self, pad=8, bufsize=512):
        gr.sync_block.__init__(self, name='Stream to Message', in_sig=[np.uint8], out_sig=[])
        self.pad = pad
        self.bufsize = bufsize
        self.buf = bytearray()
        self.last = None
        self.message_port_register_out(pmt.intern('msg'))

    def work(self, input_items, output_items):
        data = input_items[0].tobytes()
        self.buf.extend(data)
        if len(self.buf) > self.bufsize:
            del self.buf[:len(self.buf) - self.bufsize]

        zero_run = b'\x00' * self.pad
        buf = bytes(self.buf)

        # Find every complete zero-delimited segment currently in the
        # window and keep the last (most recent) one - older messages
        # can still be sitting in the buffer alongside a newer one.
        segments = []
        pos = 0
        while True:
            start = buf.find(zero_run, pos)
            if start == -1:
                break
            payload_start = start
            while payload_start < len(buf) and buf[payload_start] == 0:
                payload_start += 1
            end = buf.find(zero_run, payload_start)
            if end == -1 or end <= payload_start:
                break
            segments.append(buf[payload_start:end])
            pos = end

        if segments:
            text = segments[-1].decode('utf-8', errors='replace')
            if text and text != self.last:
                self.last = text
                self.message_port_pub(pmt.intern('msg'), pmt.string_to_symbol(text))

        return len(data)
