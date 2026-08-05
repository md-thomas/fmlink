"""
Wraps an incoming string message (from a QT GUI Message Edit Box) into a
zero-padded byte-vector PDU, ready for PDU to Tagged Stream.

The padding absorbs sync-loop acquisition transients and filter group delay so
the real payload isn't the first/last thing in each burst.
"""
from gnuradio import gr
import pmt


class blk(gr.basic_block):
    """String to Padded PDU"""

    def __init__(self, pad=48):
        gr.basic_block.__init__(self, name='String to Padded PDU', in_sig=[], out_sig=[])
        self.pad = pad
        self.message_port_register_in(pmt.intern('msg'))
        self.message_port_register_out(pmt.intern('pdus'))
        self.set_msg_handler(pmt.intern('msg'), self.handle_msg)

    def handle_msg(self, msg):
        if pmt.is_pair(msg):
            msg = pmt.cdr(msg)
        text = pmt.symbol_to_string(msg) if pmt.is_symbol(msg) else str(msg)
        payload = b'\x00' * self.pad + text.encode('utf-8') + b'\x00' * self.pad
        pdu = pmt.cons(pmt.make_dict(), pmt.init_u8vector(len(payload), list(payload)))
        self.message_port_pub(pmt.intern('pdus'), pdu)
