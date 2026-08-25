"""
Uses the 'sync' tag emitted by the access-code correlator to know exactly
which bit is the start of the payload - no more guessing a bit-to-byte
phase. Packs bits into bytes from that point and publishes the payload
once a run of zero bytes (temporary end-of-message marker, until a real
Payload Length header field exists) is seen.

term_len is deliberately smaller than how much zero padding TX actually
sends (see str_to_pdu_0's end_pad), since the filter/timing-recovery
pipeline's group delay means the last several trailing bytes never
arrive once the finite burst source goes idle - there's nothing left to
flush them through.
"""
import numpy as np
from gnuradio import gr
import pmt


class blk(gr.sync_block):
    """Sync-word-aligned bit stream to message"""

    def __init__(self, term_len=8, max_payload_bytes=256, tagname='sync'):
        gr.sync_block.__init__(self, name='Framed Stream to Message', in_sig=[np.uint8], out_sig=[])
        self.term_len = term_len
        self.max_payload_bytes = max_payload_bytes
        self.tagname = tagname
        self.bits = []
        self.armed = False
        self.message_port_register_out(pmt.intern('msg'))

    def work(self, input_items, output_items):
        in0 = input_items[0]
        tags = self.get_tags_in_window(0, 0, len(in0), pmt.intern(self.tagname))
        tag_offsets = {t.offset - self.nitems_read(0) for t in tags}

        for i, bit in enumerate(in0):
            if i in tag_offsets:
                self.bits = []
                self.armed = True
            if self.armed:
                self.bits.append(int(bit))
                if len(self.bits) >= self.max_payload_bytes * 8:
                    self.armed = False
                    self.bits = []
                    continue
                if len(self.bits) % 8 == 0 and len(self.bits) >= self.term_len * 8:
                    byts = np.packbits(np.array(self.bits, dtype=np.uint8)).tobytes()
                    zero_run = b'\x00' * self.term_len
                    idx = byts.find(zero_run)
                    if idx != -1:
                        text = byts[:idx].decode('utf-8', errors='replace')
                        if text:
                            self.message_port_pub(pmt.intern('msg'), pmt.string_to_symbol(text))
                        self.armed = False
                        self.bits = []

        return len(in0)
