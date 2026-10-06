"""
KISS framing (the protocol between a host's packet software and a TNC) and
just enough AX.25 address parsing for openwave_tnc.py.

KISS wraps each frame between FEND (0xC0) bytes. Inside, the first byte is
a command: high nibble = TNC port, low nibble = command (0 = a data frame,
1-6 = TNC parameters, see below). A FEND or FESC (0xDB) in the frame body is
escaped as FESC TFEND (0xDB 0xDC) or FESC TFESC (0xDB 0xDD). A data frame is
one AX.25 frame without the HDLC flags or FCS - the TNC adds its own
integrity check (here, OpenWave's CRC32).

Reference: the original KISS specification (Chepponis & Karn, 1987).
"""

FEND = 0xC0
FESC = 0xDB
TFEND = 0xDC
TFESC = 0xDD

CMD_DATA = 0x0
CMD_TXDELAY = 0x1      # key-up delay, in 10 ms units
CMD_PERSISTENCE = 0x2  # p-persistence, (p * 256) - 1
CMD_SLOTTIME = 0x3     # in 10 ms units
CMD_TXTAIL = 0x4       # in 10 ms units (obsolete)
CMD_FULLDUPLEX = 0x5
CMD_SETHARDWARE = 0x6
CMD_RETURN = 0xFF      # leave KISS mode (whole byte, no port nibble)

COMMAND_NAMES = {
    CMD_DATA: 'data', CMD_TXDELAY: 'txdelay', CMD_PERSISTENCE: 'persistence',
    CMD_SLOTTIME: 'slottime', CMD_TXTAIL: 'txtail', CMD_FULLDUPLEX: 'fullduplex',
    CMD_SETHARDWARE: 'sethardware',
}


def encode(data, port=0, command=CMD_DATA):
    """One frame (bytes) -> its KISS encoding, FEND to FEND."""
    out = bytearray([FEND, ((port & 0xF) << 4) | (command & 0xF)])
    for byte in data:
        if byte == FEND:
            out += bytes([FESC, TFEND])
        elif byte == FESC:
            out += bytes([FESC, TFESC])
        else:
            out.append(byte)
    out.append(FEND)
    return bytes(out)


class Decoder:
    """Incremental KISS decoder: feed() it bytes as they arrive (any
    chunking); it returns the complete frames found so far as (port,
    command, data) tuples. Empty frames (back-to-back FENDs, used as
    padding) are skipped; a bad escape is passed through as the escaped
    byte, as most TNCs do."""

    def __init__(self):
        self._buf = bytearray()
        self._in_frame = False
        self._escape = False

    def feed(self, chunk):
        frames = []
        for byte in chunk:
            if byte == FEND:
                if self._in_frame and self._buf:
                    frames.append(self._finish())
                self._buf.clear()
                self._in_frame = True
                self._escape = False
            elif not self._in_frame:
                continue  # noise before the first FEND
            elif self._escape:
                self._buf.append(FEND if byte == TFEND else FESC if byte == TFESC else byte)
                self._escape = False
            elif byte == FESC:
                self._escape = True
            else:
                self._buf.append(byte)
        return frames

    def _finish(self):
        type_byte, data = self._buf[0], bytes(self._buf[1:])
        if type_byte == CMD_RETURN:
            return 0, CMD_RETURN, data
        return type_byte >> 4, type_byte & 0xF, data


def ax25_address(field):
    """A 7-byte AX.25 address field -> 'CALL' or 'CALL-SSID'."""
    call = bytes(b >> 1 for b in field[:6]).decode('ascii', errors='replace').rstrip()
    ssid = (field[6] >> 1) & 0x0F
    return f"{call}-{ssid}" if ssid else call


def ax25_endpoints(frame):
    """An AX.25 frame -> (source, destination) callsigns, or ('', '') if
    it's too short to have an address field. Digipeater addresses, if
    any, follow these two and aren't returned."""
    if len(frame) < 14:
        return '', ''
    return ax25_address(frame[7:14]), ax25_address(frame[0:7])


def ax25_address_field(call, last=False, command=False):
    """'CALL' / 'CALL-SSID' -> a 7-byte AX.25 address field (for building
    test frames). last marks the final address; command sets the C/H bit."""
    base, _, ssid = call.upper().partition('-')
    field = bytes(ord(c) << 1 for c in base.ljust(6)[:6])
    return field + bytes([0x60 | (0x80 if command else 0) | ((int(ssid or 0) & 0xF) << 1) | int(last)])


def ax25_ui_frame(src, dst, info, pid=0xF0):
    """A minimal AX.25 UI frame (unnumbered information, no digipeaters):
    the kind of frame a beacon or APRS packet is. For tests and tools."""
    return (ax25_address_field(dst, command=True) + ax25_address_field(src, last=True)
            + bytes([0x03, pid]) + info)
