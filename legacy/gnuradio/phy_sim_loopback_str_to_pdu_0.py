"""
Wraps an incoming string message (from a QT GUI Message Edit Box or the
beacon strobe) into a full FMlink link-layer frame - preamble, sync word,
header (with real Payload Length, Sequence Number, and Flags fields), payload,
and CRC32 - using the shared fmlink_link module so TX and RX can't drift out
of sync on the framing format. Ready for PDU to Tagged Stream.

Sequence Number increments once per transmitted frame (wraps at 16 bits) - lets
RX detect gaps/duplicates even with no ACK/retransmission protocol built yet.
ack_requested sets the Flags byte's ACK Requested bit on every frame this block
sends; nothing consumes it yet (no ACK responder exists), but RX parses and
displays it. The other flag bits (fragmented, more_fragments, ack_response,
priority) are supported by fmlink_link.build_frame() but not exposed here -
they need their own consuming feature to mean anything.
"""
from gnuradio import gr
import pmt
import sys, os
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())
import fmlink_link as link


class blk(gr.basic_block):
    """String to FMlink Frame PDU"""

    def __init__(self, lead_pad=24, end_pad=24, ack_requested=False):
        gr.basic_block.__init__(self, name='String to FMlink Frame PDU', in_sig=[], out_sig=[])
        self.lead_pad = lead_pad
        self.end_pad = end_pad
        self.flags = link.FLAG_ACK_REQUESTED if ack_requested else 0
        self.seq = 0
        self.message_port_register_in(pmt.intern('msg'))
        self.message_port_register_out(pmt.intern('pdus'))
        self.set_msg_handler(pmt.intern('msg'), self.handle_msg)

    def handle_msg(self, msg):
        if pmt.is_pair(msg):
            msg = pmt.cdr(msg)
        text = pmt.symbol_to_string(msg) if pmt.is_symbol(msg) else str(msg)
        payload = text.encode('utf-8')
        frame = link.build_frame(payload, lead_pad=self.lead_pad, end_pad=self.end_pad,
                                  seq=self.seq, flags=self.flags)
        self.seq = (self.seq + 1) % link.SEQ_MODULUS
        pdu = pmt.cons(pmt.make_dict(), pmt.init_u8vector(len(frame), list(frame)))
        self.message_port_pub(pmt.intern('pdus'), pdu)
