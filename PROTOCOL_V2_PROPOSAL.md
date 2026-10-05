# Proposal: OpenWave frame format v2 (compact header, shorter audio preamble)

Status: **accepted and implemented (2026-10-05)** as frame format v2:
compact 20-byte header with flag bit 5 for header extensions, the
callsign alphabet as drafted, a 16-byte audio preamble, 1-byte FEC preset
IDs, and compatibility option A (clean switch). The spec
(`OpenWave_Specification.txt`) is the authoritative description; this file
records the reasoning and measurements.

Re-measured after implementation, through the v2 production code (same 9
fading conditions × 40 frames as below): mode 0 357/360, mode 2 322/360.
A targeted follow-up (200 frames per point at the two slow-fading
conditions, 32- vs 16-byte preamble with the v2 header) found no
significant preamble effect: every difference was within about one
standard error, in both directions.

## Why

On short bursts most of the airtime is fixed overhead, not payload. At
mode 0 the FEC-coded 32-byte header alone (1.76 s) takes longer to send
than a 20-byte message (1.07 s). Shorter bursts mean less airtime, and on
a fading FM link fewer fades caught per burst.

Measured airtime (audio PHY, mic port, default 250 ms lead-in):

| Burst | v1 today | header 32→20 B | + audio preamble 32→16 B | saving |
|---|---|---|---|---|
| Mode 0 / r1_4_k9, 20-byte message | 3.86 s | 3.22 s | **3.01 s** | 22% |
| Mode 0 / r1_4_k9, ACK | 2.79 s | 2.15 s | **1.94 s** | 31% |
| Mode 2 / r1_2_k7, 20-byte message | 0.79 s | 0.71 s | **0.65 s** | 17% |
| Mode 2 / r1_2_k7, ACK | 0.65 s | 0.57 s | **0.52 s** | 20% |
| Mode 2 / r1_2_k7, 200-byte message | 1.99 s | 1.91 s | **1.85 s** | 7% |
| Negotiation MODE_SET (control channel) | 2.02 s | 1.70 s | **1.49 s** | 26% |

A message plus its ACK at mode 0 drops from 6.65 s to 4.95 s of airtime.

## What changes

1. **Header: 32 → 20 bytes** (layout below). Drops the 7 reserved bytes,
   packs callsigns into 6 bytes instead of 8, and shares one byte between
   version and packet type.
2. **Audio PHY preamble: 32 → 16 bytes** (the first 16 bytes of the same
   PN sequence, then the same sync word). The IQ PHY keeps 32: its
   bit-level marker search needs the length.
3. **(Optional) Control payloads carry a 1-byte FEC preset ID** instead of
   the preset's name: MODE_SET 1.49 → 1.33 s, MODE_GRANT 1.59 → 1.44 s.

Unchanged: convolutional FEC and presets, golden interleaver, CRC32, max
payload (1024), sync word, flags, fragmentation rules, Waveform Modes,
the IQ PHY's padding, ACK semantics, Control ops.

## v2 header layout (20 bytes)

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 1 | Version (high 4 bits) / Packet Type (low 4 bits) | version 2; types 1-4 as today (room for 16) |
| 1 | 1 | Waveform Mode ID | as v1 |
| 2 | 1 | Flags | as v1, plus bit 5 = Header Extension (below); bits 6-7 reserved |
| 3 | 2 | Sequence Number | as v1, big-endian |
| 5 | 6 | Source callsign | packed, see below |
| 11 | 6 | Destination callsign | packed; "ALL" = broadcast |
| 17 | 2 | Payload Length | as v1 (0-1024), big-endian |
| 19 | 1 | Fragment Index | as v1 |

No reserved bytes. Room to grow comes from elsewhere:
- **flag bit 5 (Header Extension)**, below;
- **flag bits 6-7**, still free;
- **packet types 5-15**, unused;
- **versions 3-15**, unused.

A spare byte was considered and dropped. It would cost every frame about
53 ms at mode 0, and the likely future fields (e.g. a relay path, at 6
bytes per hop) wouldn't fit in one byte anyway. CRC32 still covers
header + payload.

### Header extension (flag bit 5)

For fields a future version wants to carry only in some frames. When
flag bit 5 is set, the payload starts with an extension block, and the
application data follows it:

```
[block length N: 1 byte][N bytes of entries][application payload...]
entry = [type: 1 byte][length L: 1 byte][L bytes of value]
```

- Payload Length counts the whole payload, extension block included.
- The block belongs to the frame it's in. In a fragmented message, each
  fragment that sets the flag carries its own block, which is removed
  before the fragments are reassembled.
- A receiver skips any entry type it doesn't recognize, using its length.
  An older v2 station can therefore still read the application payload
  of a frame that uses newer extensions.
- No entry types are defined in v2. Frames without the flag carry no
  block and cost nothing extra.
- Examples of what this could carry later: a relay path or hop count, a
  network/group ID, a content type (text/file/compressed), or an
  authentication signature (proving the sender; content stays in the
  clear as amateur rules require).

### Callsign packing (6 bytes, up to 9 characters)

Alphabet of 40 symbols, each character's index:

| Index | 0 | 1-26 | 27-36 | 37 | 38 | 39 |
|---|---|---|---|---|---|---|
| Character | end / padding | A-Z | 0-9 | `/` | `-` | `.` |

Left-align the callsign, pad to 9 characters with index 0, and encode
value = c0·40^8 + c1·40^7 + ... + c8 as a 48-bit big-endian integer
(40^9 = 2.62e14 < 2^48 = 2.81e14, so it always fits). Lowercase is folded
to uppercase; any other character is rejected when building a frame. To
decode, take base-40 digits most-significant first and stop at the first 0.

| Callsign | Packed |
|---|---|
| K0MDT | 45 A3 24 2B 40 00 |
| W1AW | 8D 44 D5 05 80 00 |
| ALL | 07 CB 16 30 00 00 |
| K0MDT-7 | 45 A3 24 51 30 80 |
| W1AW/P | 8D 44 DA BA 70 00 |
| VE3XYZ/MM | 83 FD 44 BE DD 55 |

Compared with v1's 8-byte field: one more character (9 vs 8), and SSIDs
(`-7`) and portable suffixes (`/P`, `/MM`) fit, but only the alphabet
above is allowed. v1 accepted any UTF-8 up to 8 bytes.

### Worked example (same frame as the spec's v1 example)

Mode 2 Data frame, ACK requested, seq 1, K0MDT → ALL, payload "Hello":

```
21                    <- Version 2 / Packet Type 1 (Data)
02                    <- Waveform Mode ID (Mode 2)
01                    <- Flags (ACK requested)
00 01                 <- Sequence Number
45 A3 24 2B 40 00     <- Source "K0MDT"
07 CB 16 30 00 00     <- Destination "ALL"
00 05                 <- Payload Length
00                    <- Fragment Index
48 65 6C 6C 6F        <- Payload "Hello"
F4 97 61 37           <- CRC32 (header + payload)
```

## Shorter audio preamble: evidence

Measured through ota_sim's FM model (mic port; 9 conditions × 40 frames
each: no fading at 8, 10 and 15 dB with 100 ppm clock offset, plus
Rayleigh fading at 5, 30 and 80 km/h at 12 and 15 dB), frames delivered:

| Audio preamble | Mode 0 / r1_4_k9 | Mode 2 / r1_2_k7 |
|---|---|---|
| 32 bytes (today) | 360/360 | 333/360 |
| **16 bytes** | **359/360** | **330/360** |
| 8 bytes | 359/360 | 319/360 |
| 4 bytes | 352/360 | 323/360 |

16 bytes delivers the same as 32 (within run-to-run variation); 8 and
below start missing frames in slow fading, where a short marker lets a
noise peak out-correlate a faded one. The marker still gives 160 symbols
(~22 dB) of correlation at mode 0.

## Optional: 1-byte FEC preset IDs in Control payloads

MODE_GRANT and MODE_SET currently end with the preset name in ASCII
("r1_2_k7", 7 bytes). Replace it with a fixed ID from a table in the
spec:

| ID | Preset | ID | Preset | ID | Preset |
|---|---|---|---|---|---|
| 1 | r1_2_k3 | 6 | r1_3_k9 | 11 | r1_5_k9 |
| 2 | r1_2_k7 | 7 | r1_4_k3 | 12 | r1_6_k7 |
| 3 | r1_2_k9 | 8 | r1_4_k7 | 13 | r1_6_k9 |
| 4 | r1_3_k3 | 9 | r1_4_k9 | | |
| 5 | r1_3_k7 | 10 | r1_5_k7 | | |

Saves ~0.15 s per negotiation message. The cost is a numbering that can
never be reordered: new presets only get new IDs.

## Compatibility: how v1 and v2 stations coexist

The version field sits inside the FEC-coded header, and the header length
(32 vs 20) must be known before it can be decoded, so a receiver can't
just read the version and adapt. Options:

- **A. Clean switch (recommended).** v2 only; both stations and every
  tool move together. There are no deployed radios, and all ends run this
  codebase. Simplest code, smallest test surface. Old capture files
  (`openwave_cli.py`, `fec_*.py` outputs) stop decoding unless a
  `--v1`-style option is kept for them.
- **B. The sync word selects the format.** v2 frames use a different
  4-byte sync word, so the receiver knows the format from the marker it
  matched, before decoding anything. A receiver can then accept both. It
  costs a second correlation per burst, and a second sync word that has to
  be chosen for low cross-correlation with the first. Worth it only if v1
  and v2 stations need to share a channel.

## Decisions needed

1. Adopt the compact header (20 bytes, layout above), with flag bit 5
   reserved for header extensions? Any field to keep or change?
2. Is the callsign alphabet right (A-Z, 0-9, `/`, `-`, `.`; 9 characters)?
3. Audio preamble 16 bytes?
4. Include the optional 1-byte preset IDs?
5. Compatibility: A (clean switch) or B (sync word selects format)?

## Implementation outline (after approval)

- `openwave_link.py`: v2 `build_header`/`parse_header`, callsign
  pack/unpack, `HEADER_LEN = 20`, `VERSION = 2`, `FLAG_HEADER_EXTENSION`.
  Receivers skip an extension block, so its application payload is
  delivered intact; senders don't set the flag yet. Frame builders take
  the PHY's preamble length.
- `openwave_sync.py` / `openwave_fec.py`: marker length as a parameter
  (32 bytes IQ, 16 bytes audio) instead of the fixed 288 bits.
- `openwave_audio.py` / `ota_station.py`: 16-byte marker on the audio
  path; preset IDs in Control payloads if adopted.
- `OpenWave_Specification.txt`: new header layout, callsign encoding,
  preamble per PHY, preset ID table, a new worked example.
- Tests: header and callsign round trips, the new worked example
  byte-for-byte, a frame with an extension block (incl. unknown entry
  types) delivering its application payload intact, all existing tests updated. Then re-run the fading and
  preamble studies to confirm the delivery figures hold with the new
  format.
