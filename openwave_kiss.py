"""
KISS framing (the protocol between a host's packet software and a TNC) and
just enough AX.25 address parsing for openwave_tnc.py.

KISS wraps each frame between FEND (0xC0) bytes. Inside, the first byte is
a command: high nibble = TNC port, low nibble = command (0 = a data frame,
1-6 = TNC parameters, see below). A FEND or FESC (0xDB) in the frame body is
escaped as FESC TFEND (0xDB 0xDC) or FESC TFESC (0xDB 0xDD). A data frame is
one AX.25 frame without the HDLC flags or FCS - the TNC adds its own
integrity check (here, OpenWave's CRC32).

CRC-protected variants: Linux's mkiss driver (kissattach) by default
starts in "auto" mode and sends SMACK (command byte | 0x80, CRC-16 appended)
or FlexNet (command byte | 0x20, FlexNet CRC appended) frames to probe the
TNC, before settling on plain KISS. Decoder accepts all three, recording
which one the host used so replies can match it (encode(..., crc=...)).
The flag bits overlap KISS port numbers 8+ and 2, so a frame only counts
as SMACK/FlexNet if its CRC checks out; otherwise it's a plain frame for
that port.

Reference: the original KISS specification (Chepponis & Karn, 1987); SMACK
and FlexNet CRCs as implemented by Linux's drivers/net/hamradio/mkiss.c.
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

CRC_NONE = 'none'
CRC_SMACK = 'smack'
CRC_FLEXNET = 'flexnet'
_SMACK_FLAG = 0x80
_FLEX_FLAG = 0x20


def _smack_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


def _flex_table():
    # mkiss.c's crc_flex_table: the reflected CRC-CCITT table (0x8408), XORed
    # with a constant 0x0F87, used with a non-reflected shift - see crc_flexnet().
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        table.append(crc ^ 0x0F87)
    return table


_SMACK_TABLE = _smack_table()
_FLEX_TABLE = _flex_table()


def crc_smack(data):
    """CRC-16/ARC (poly 0x8005 reflected, init 0) - SMACK's CRC, sent low
    byte first. Over a frame with its CRC appended, the result is 0."""
    crc = 0
    for byte in data:
        crc = (crc >> 8) ^ _SMACK_TABLE[(crc ^ byte) & 0xFF]
    return crc


def crc_flexnet(data):
    """FlexNet's CRC as mkiss computes it (init 0xFFFF), sent high byte
    first. Over a frame with its CRC appended, the result is 0x7070."""
    crc = 0xFFFF
    for byte in data:
        crc = ((crc << 8) ^ _FLEX_TABLE[((crc >> 8) ^ byte) & 0xFF]) & 0xFFFF
    return crc


COMMAND_NAMES = {
    CMD_DATA: 'data', CMD_TXDELAY: 'txdelay', CMD_PERSISTENCE: 'persistence',
    CMD_SLOTTIME: 'slottime', CMD_TXTAIL: 'txtail', CMD_FULLDUPLEX: 'fullduplex',
    CMD_SETHARDWARE: 'sethardware',
}


def encode(data, port=0, command=CMD_DATA, crc=CRC_NONE):
    """One frame (bytes) -> its KISS encoding, FEND to FEND. crc=CRC_SMACK
    or CRC_FLEXNET adds that variant's flag and CRC (data frames on ports
    0-7 / port 0 or 1 only, as the flag bits overlap the port number)."""
    body = bytearray([((port & 0xF) << 4) | (command & 0xF)])
    body += data
    if crc == CRC_SMACK:
        body[0] |= _SMACK_FLAG
        value = crc_smack(body)
        body += bytes([value & 0xFF, value >> 8])
    elif crc == CRC_FLEXNET:
        body[0] |= _FLEX_FLAG
        value = crc_flexnet(body)
        body += bytes([value >> 8, value & 0xFF])
    out = bytearray([FEND])
    for byte in body:
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
    byte, as most TNCs do. SMACK/FlexNet frames have their CRC checked and
    removed; crc_mode is the variant of the most recent such data frame
    (CRC_NONE until one arrives)."""

    def __init__(self):
        self._buf = bytearray()
        self._in_frame = False
        self._escape = False
        self.crc_mode = CRC_NONE

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
        frame = bytes(self._buf)
        type_byte = frame[0]
        if type_byte == CMD_RETURN:
            return 0, CMD_RETURN, frame[1:]
        if len(frame) >= 3:
            if type_byte & _SMACK_FLAG and crc_smack(frame) == 0:
                self.crc_mode = CRC_SMACK
                type_byte &= ~_SMACK_FLAG
                return type_byte >> 4, type_byte & 0xF, frame[1:-2]
            if type_byte & _FLEX_FLAG and crc_flexnet(frame) == 0x7070:
                self.crc_mode = CRC_FLEXNET
                type_byte &= ~_FLEX_FLAG
                return type_byte >> 4, type_byte & 0xF, frame[1:-2]
        if (type_byte & 0x0F) == CMD_DATA and not type_byte & (_SMACK_FLAG | _FLEX_FLAG):
            self.crc_mode = CRC_NONE
        return type_byte >> 4, type_byte & 0xF, frame[1:]


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
