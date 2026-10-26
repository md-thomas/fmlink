"""
Wraps an incoming string message (from a QT GUI Message Edit Box) into a
PDU framed with a timing preamble, the FMlink sync word,
and a temporary zero-run end marker (until a real Payload Length header
field exists), ready for PDU to Tagged Stream. The lead-in zeros give
Symbol Sync's acquisition loop time to settle before the preamble
arrives; the trailing zeros give the matched-filter/timing-recovery
pipeline's group delay somewhere to flush into once the real payload
ends.
"""
from gnuradio import gr
import pmt

PREAMBLE = b'\x55' * 8
SYNC_WORD = b'\x1A\xCF\xFC\x1D'


class blk(gr.basic_block):
    """String to Framed PDU"""

    def __init__(self, lead_pad=24, end_pad=24):
        gr.basic_block.__init__(self, name='String to Framed PDU', in_sig=[], out_sig=[])
        self.lead_pad = lead_pad
        self.end_pad = end_pad
        self.message_port_register_in(pmt.intern('msg'))
        self.message_port_register_out(pmt.intern('pdus'))
        self.set_msg_handler(pmt.intern('msg'), self.handle_msg)

    def handle_msg(self, msg):
        if pmt.is_pair(msg):
            msg = pmt.cdr(msg)
        text = pmt.symbol_to_string(msg) if pmt.is_symbol(msg) else str(msg)
        payload = (b'\x00' * self.lead_pad + PREAMBLE + SYNC_WORD +
                   text.encode('utf-8') + b'\x00' * self.end_pad)
        pdu = pmt.cons(pmt.make_dict(), pmt.init_u8vector(len(payload), list(payload)))
        self.message_port_pub(pmt.intern('pdus'), pdu)
