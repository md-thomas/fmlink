"""
Uses the 'sync' tag from the access-code correlator to know exactly where the
header starts, then uses openwave_link.FrameParser to collect exactly
Header+Payload+CRC32 bytes (per the header's real Payload Length field) and
verify the CRC before publishing. No zero-run-terminator guessing - the header
tells us exactly how many bytes to expect, and the CRC tells us whether we got
them right.

Also tracks Sequence Number (gap/duplicate detection) and parses Flags from
each CRC-verified frame - never from a CRC-failed one, since a corrupted
header can still unpack without a struct error while holding garbage values.
Counts and the last frame's seq/flags are published on the 'stats' port -
useful for testing loss/duplication without an ACK protocol or real
over-the-air conditions (crank up noise_voltage in phy_sim_loopback.grc's
channel model).
"""
import numpy as np
from gnuradio import gr
import pmt
import sys, os
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())
import openwave_link as link


class blk(gr.sync_block):
    """Sync-word-aligned, header-framed bit stream to message"""

    def __init__(self, tagname='sync'):
        gr.sync_block.__init__(self, name='Framed Stream to Message', in_sig=[np.uint8], out_sig=[])
        self.tagname = tagname
        self.parser = link.FrameParser()
        self.armed = False
        self.bitpos = 0
        self.curbyte = 0
        self.counts = {'ok': 0, 'crc_fail': 0, 'bad_length': 0, 'dup': 0, 'lost': 0}
        self.last_seq = None
        self.last_flags = 0
        self.message_port_register_out(pmt.intern('msg'))
        self.message_port_register_out(pmt.intern('stats'))

    def _publish_stats(self):
        c = self.counts
        seq_str = str(self.last_seq) if self.last_seq is not None else '-'
        text = (f"OK:{c['ok']}  CRC fail:{c['crc_fail']}  Bad length:{c['bad_length']}  "
                f"Dup:{c['dup']}  Lost:{c['lost']}  |  Seq:{seq_str}  "
                f"Flags:{link.format_flags(self.last_flags)}")
        self.message_port_pub(pmt.intern('stats'), pmt.string_to_symbol(text))

    def _update_seq(self, header):
        seq = header['seq']
        if self.last_seq is not None:
            if seq == self.last_seq:
                self.counts['dup'] += 1
            else:
                gap = (seq - self.last_seq - 1) % link.SEQ_MODULUS
                # Bound the gap we'll believe as real loss vs. e.g. TX having
                # restarted (seq reset to 0) - avoids one bogus huge count.
                if 0 < gap < 1000:
                    self.counts['lost'] += gap
        self.last_seq = seq
        self.last_flags = header['flags']

    def work(self, input_items, output_items):
        in0 = input_items[0]
        tags = self.get_tags_in_window(0, 0, len(in0), pmt.intern(self.tagname))
        tag_offsets = {t.offset - self.nitems_read(0) for t in tags}

        for i, bit in enumerate(in0):
            if i in tag_offsets:
                self.parser.reset()
                self.armed = True
                self.bitpos = 0
                self.curbyte = 0

            if not self.armed:
                continue

            self.curbyte = (self.curbyte << 1) | int(bit)
            self.bitpos += 1
            if self.bitpos == 8:
                status, payload = self.parser.feed(self.curbyte)
                self.bitpos = 0
                self.curbyte = 0
                if status == 'ok':
                    self._update_seq(self.parser.last_header)
                    self.counts['ok'] += 1
                    self._publish_stats()
                    text = payload.decode('utf-8', errors='replace')
                    if text:
                        self.message_port_pub(pmt.intern('msg'), pmt.string_to_symbol(text))
                    self.armed = False
                elif status in ('crc_fail', 'bad_length'):
                    self.counts[status] += 1
                    self._publish_stats()
                    self.armed = False

        return len(in0)
