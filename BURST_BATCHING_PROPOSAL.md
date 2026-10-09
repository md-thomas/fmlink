# Proposal: burst batching

Status: **accepted (2026-10-08). Part 1 implemented (2026-10-08), not yet
tried on the air; part 2 next.** Decisions at the end.

Part 1 as built: the batching lives in `ota_station.Station`
(`_queue_burst` / `_flush_tx_batch`), so it covers both the radio and the
simulator. Bursts queued within 50 ms (`BATCH_COLLECT_S`) share one
transmission, later ones 50 ms apart (`BATCH_GAP_S`) without a lead-in, up
to `--max-tx-s` (default 30) on `ota_station.py` and `openwave_tnc.py`.
Receiving: the radio's burst detector now trims only to a decoded burst's
end (it used to drop 0.3 s more, which would cut into the next burst), and
a station handed a whole transmission at once (the simulator) decodes the
bursts in it earliest first, one frame per burst. Tests: a 4-burst
batched transmission through the burst detector; four AX.25 frames from a
KISS host crossing the simulated FM link in one transmission, in order.

## Problem

A message longer than 1024 bytes is sent as segments, and today each
segment is its own round trip (stop-and-wait):

```
A: [PTT][lead-in][segment 1]
B:                           [turnaround][PTT][lead-in][ACK]
A:                                                          [turnaround][PTT][lead-in][segment 2] ...
```

Measured on the air at mode 3 (RADIO_SETUP.md, "Message length test"): a
1024-byte segment takes **3.8 s to send** (3.5 s of symbols plus the 250 ms
lead-in) but **~6.3 s per segment overall**. The other ~2.5 s goes to the
turnaround each way, a second PTT key-up and lead-in for the ACK, the ACK
itself (0.4 s), and the receiver's burst detection. An 8000-character
message took 50 s, about 160 chars/s, against mode 3's 2400 bit/s
(~300 chars/s) after FEC.

The KISS TNC has the same problem one layer down. AX.25 sends up to its
window (MAXFRAME, 1-7) of I-frames back to back, but the TNC keys the radio
separately for each one, paying a key-up, lead-in and receiver turnaround
per frame.

## Idea

Send several frames in **one transmission** (one PTT key-up, one lead-in),
and acknowledge them with **one selective ACK** that says which arrived;
resend only the missing ones.

```
A: [PTT][lead-in][seg 1][seg 2][seg 3] ... [seg 8]
B:                                                 [turnaround][PTT][lead-in][ACK: 1-8 except 5]
A:                                                                                              [PTT][lead-in][seg 5]
B:                                                                                                                    [ACK]
```

Every segment stays a complete, independent frame (its own marker, header
and CRC). The receiver already decodes bursts that arrive back to back, in
order (the BurstDetector fix of 2026-10-07).

## Design

Two parts. Part 1 needs no protocol change and helps the KISS TNC on its
own; part 2 is the link-layer change for OpenWave messages.

### Part 1 - transport: one key-up for everything queued

`openwave_radio.RadioTransport` transmits one queued burst per PTT key-up.
Change its transmit loop to take **every burst already queued** (plus any
that arrive within a short collection window, ~50 ms) and play them in one
key-up:

- the first burst keeps its lead-in; later ones get a short gap
  (`BATCH_GAP_S`, ~50 ms) instead of the 250 ms lead-in, enough for the
  receiver to separate them;
- stop adding bursts when the key-up would exceed `--max-tx-s` (default
  30 s; radios have a transmit time-out, and a shared channel needs gaps).
  At least one burst always goes, however long;
- the receiver needs no change.

The simulator transport (`ota_sim` `SimClient`) gets the same: concatenate
the queued bursts into one message so the simulator's airtime and collision
model see one transmission.

**Effect on the KISS TNC:** the kernel hands the TNC a window of I-frames
within milliseconds, so they would go out in one key-up automatically.
With batching it becomes worth raising the AX.25 window (`axports`'
window column, 2 today) toward 7. The kernel's T1 retry timer must then
cover the longer transmission (`/proc/sys/net/ax25/ax0/t1_timeout`).

### Part 2 - link layer: windowed segments and a selective ACK

**Sender** (`Station.send_message`, `--ack`): instead of one segment then
wait, send a **batch** of up to `--batch` segments (default: as many as fit
in `--max-tx-s`), all queued at once so part 1 puts them in one key-up.

- Only the **last frame of the batch** has `FLAG_ACK_REQUESTED`; the others
  are sent with the flag clear, so the receiver doesn't ACK each one.
- After the batch, wait for one ACK (timeout sized for the batch's total
  airtime). Resend only the segments it reports missing, as the next batch,
  until all are acknowledged or the retry limit is reached.

**Selective ACK** (packet type ACK, so no new type):

- An ACK with an **empty payload** means what it does today: the header's
  sequence number is acknowledged.
- An ACK with a **payload** is selective. Header sequence number = the
  message's first segment's sequence number; payload =

  ```
  byte 0      count n of segments the bitmap covers (1-64)
  bytes 1..   bitmap, ceil(n / 8) bytes: bit i set = segment with
              fragment index i received
  ```

  64 segments (64 KiB, `MAX_FRAGMENTS`) fit in 9 bytes.

**Receiver:**

- Send a selective ACK when a frame with `FLAG_ACK_REQUESTED` arrives,
  covering every segment of that message received so far.
- If the batch's last frame is lost, the receiver never sees the flag. So
  also send one after the batch ends without it: a few seconds after the
  last segment of an incomplete message, with no further bursts heard.
  Without this the sender would time out and resend the whole batch.
- **Out-of-order reassembly:** `FragmentReassembler` today requires
  segments strictly in order and aborts on a gap. With selective resends,
  segment 5 can arrive after segment 8. Change it to collect segments by
  fragment index (per source and first sequence number), deliver when
  indices 0..last are all present (last = the one without
  `FLAG_MORE_FRAGMENTS`), and keep a timeout to discard abandoned messages.
  Duplicates (a resend of something that already arrived) are dropped.

**Compatibility:** a station that doesn't know about selective ACKs must
not be sent batches. During `--auto` negotiation each side appends a
capabilities byte to its MODE_GRANT / MODE_SET payload (bit 0 = batching).
Older stations already ignore trailing bytes there
(`parse_control_payload`), and a station that sends none is treated as
"no batching", so mixed versions fall back to stop-and-wait. Without
`--auto` there is no negotiation: batching then needs `--batch` on both
stations.

## Expected gain

Mode 3, an 8000-character message (8 segments), the 2026-10-08 link:

| | Today | Batched |
|---|---|---|
| Transmissions | 8 data + 8 ACK | 1 data + 1 ACK |
| Time | 50 s (measured) | ~32 s (estimate: 28.9 s of segments + key-up + one turnaround and ACK) |
| Throughput | ~160 chars/s | ~250 chars/s |

Single-segment messages are unchanged. Slower modes gain less, because
airtime dominates and fewer segments fit under the cap. A full 1024-byte
segment is 7.1 s at mode 2 (4 per 30 s batch), 14.1 s at mode 1 (2 per
batch) and 28-56 s at mode 0 depending on the FEC, so mode 0 sends one
segment per transmission, as today.

## Costs and risks

- **Longer transmissions.** 8 segments at mode 3 is ~29 s keyed. Capped by
  `--max-tx-s`. HTs often have a transmit time-out of 60-180 s; check yours.
- **A fade costs more per transmission**, but only the missed segments are
  resent, so it degrades gracefully. On a link losing many segments the
  sender could shrink the batch (a later refinement).
- **The receiver is deaf for longer** while the other station transmits a
  batch. That's fine for two stations; with more on a frequency it raises
  the stakes on channel access (carrier sense, already in TODO).
- **Spec change**: the selective ACK payload and the capabilities byte go
  into OpenWave_Specification.txt. The header is unchanged.

## Plan

1. Part 1: transport batching in `RadioTransport` and the simulator client,
   `--max-tx-s`. Test: the KISS TNC over the simulator with the kernel's
   window at 7, then Pat on the air.
2. Part 2: selective ACK, out-of-order reassembly, batched sending,
   capability bit. Test over the simulator (including lost segments), then
   repeat the on-air message length test and compare with the 2026-10-08
   numbers.
3. Spec, RADIO_SETUP and MODEM_ARCHITECTURE updates.

## Decisions (2026-10-08)

1. `--max-tx-s` default **30 s**.
2. Batching **on automatically** once both stations have negotiated it;
   stations without it fall back to stop-and-wait.
3. **Part 1 first** (transport batching, no protocol change), then part 2.
