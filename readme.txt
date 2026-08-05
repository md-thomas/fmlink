test1.grc - Interactive QPSK Differential Modem
================================================

This flow graph is a loopback QPSK modem test bench: type a message into the
"Send" box, it gets modulated, pushed through a simulated noisy channel, then
demodulated and shown in the "Received" box. It's meant for exercising/
demonstrating a differentially-encoded QPSK modem chain, not as a real
transmit/receive system (there's no radio hardware involved - the "channel"
is a software noise/impairment model).

Variables & Parameters
-----------------------
msg (Parameter, -m/--msg)
    Startup default text for the Send box. Only used once, when the flow
    graph first constructs the GUI - after that, whatever you type in the
    Send box is what gets transmitted.

symbol_rate (Parameter, -r/--symbol-rate)
    Baud rate in symbols/sec, 1200-19200. Command-line configurable.

sps (Variable = 4)
    Samples per symbol used throughout the modulator/demodulator.

samp_rate (Variable = symbol_rate * sps)
    The complex sample rate the TX/channel/RX chain actually runs at.
    Only feeds the Throttle block below - it's what makes the simulation
    run at a realistic pace instead of as fast as the CPU allows.

nfilts / rrc_taps (Variables)
    Left over from an earlier design that used a polyphase-filterbank
    resampler in Symbol Sync. The current Symbol Sync block uses the
    simpler MMSE 8-tap interpolator instead, so these two are effectively
    unused - harmless, just not doing anything anymore.

qpsk_const (Variable, Constellation Object)
    The QPSK constellation (4 points, Gray-coded symbol map) shared by the
    modulator, demodulator, and Symbol Sync's slicer.

Transmit chain
--------------
QT GUI Message Edit Box ("Send", qtgui_edit_box_msg_0)
    Text box. Posts a message with the typed text every time you press
    Enter (or the box loses focus).

str_to_pdu (embedded Python block)
    Bridges the plain string message from the Send box into a proper 
    Protocol Data Unit (PDU):
    pads the text with 48 zero bytes on each side and wraps it as a
    byte-vector message. The padding exists so the real payload isn't the
    very first/last thing in the burst - it gives the receiver's Costas
    loop and Symbol Sync time to lock on before the real data arrives, and
    absorbs the TX/RX filters' group delay at the tail end.

PDU to Tagged Stream (pdu_pdu_to_tagged_stream_0)
    Converts the incoming PDU into an actual byte stream burst (with a
    "packet_len" tag marking its start) so it can flow into the ordinary
    streaming blocks below. This is what actually kicks off transmission -
    nothing flows through the rest of the TX chain until a message arrives
    here.

Unpack K Bits, k=8 (blocks_unpack_k_bits_bb_0)
    Splits each byte into 8 individual bits (1 bit per output item).

Pack K Bits, k=2 (blocks_pack_k_bits_bb_0)
    Regroups those individual bits into 2-bit symbols (values 0-3) - one
    output item per QPSK symbol.

Differential Encoder, modulus 4 (digital_diff_encoder_bb_0)
    Encodes each symbol relative to the previous one instead of in
    absolute terms. This is the fix for the Costas loop's 4-fold phase
    ambiguity below: a QPSK Costas loop can lock onto the correct phase or
    any 90/180/270-degree rotation of it, and has no way to tell which.
    Differential encoding/decoding makes the recovered bits correct
    regardless of which of the 4 lock points the receiver settles on.

Chunks to Symbols (digital_chunks_to_symbols_xx_0)
    Maps each 2-bit symbol value to its complex QPSK constellation point.

Interpolating FIR Filter / RRC pulse shaping (filter_interp_fir_filter_xxx_0)
    Upsamples by sps (4x) and applies root-raised-cosine pulse shaping
    (excess bandwidth 0.35), producing the actual complex baseband signal.

Throttle (blocks_throttle2_0)
    Paces the flow graph to samp_rate instead of free-running as fast as
    the CPU allows. Not needed for correctness with these message-driven
    bursts, but keeps CPU usage sane and makes the timing behave like a
    real modem running at the chosen baud rate.

Channel simulation
-------------------
Channel Model (channels_channel_model_0)
    Adds a small amount of AWGN noise (noise_voltage=0.001) to the
    transmitted signal. frequency_offset and epsilon (timing offset) are
    both left at 0/1.0 (no impairment) - noise is the only thing actually
    being simulated right now.

Receive chain
-------------
Symbol Sync (digital_symbol_sync_xx_0)
    Recovers symbol timing using a Modified Mueller & Muller timing-error
    detector and an MMSE 8-tap interpolator, decimating back down to one
    sample per symbol.

Costas Loop, order 4 (digital_costas_loop_cc_0)
    Recovers carrier phase for QPSK. As noted above, it has a 4-fold phase
    ambiguity that's resolved downstream by differential decoding rather
    than here.

Constellation Decoder (digital_constellation_decoder_cb_0)
    Slices each received complex sample to the nearest QPSK constellation
    point and outputs the corresponding 2-bit symbol value.

Differential Decoder, modulus 4 (digital_diff_decoder_bb_0)
    Undoes the differential encoding from the TX side, making the output
    correct regardless of the Costas loop's phase-lock ambiguity.

Unpack K Bits, k=2 (blocks_unpack_k_bits_bb_1)
    Splits each 2-bit symbol back into individual bits.

Pack K Bits, k=8 (blocks_pack_k_bits_bb_1)
    Repacks those bits into full 8-bit bytes - the final recovered byte
    stream.

stream_display_sink (embedded Python block)
    Keeps a rolling 512-byte buffer of the recovered bytes and continuously
    scans it for the zero-padding markers str_to_pdu added on the way out.
    Whenever it finds a complete, new (changed) message framed between two
    such markers, it publishes it as a string message.

    This is deliberately NOT tag-based (i.e. it doesn't use the
    "packet_len" tag from PDU to Tagged Stream): an earlier version tried
    that with Tagged Stream to PDU and it crashed - the Costas loop/Symbol
    Sync only preserve tag positions approximately, which is fine for a
    continuous repeating stream but not exact enough for a strict,
    isolated one-shot burst. Scanning for the zero-padding markers is more
    forgiving and is what actually works reliably here.

QT GUI Message Edit Box ("Received", qtgui_edit_box_msg_1)
    Displays whatever text stream_display_sink publishes.

QT GUI Constellation Sink (qtgui_const_sink_x_0)
    Plots the complex samples coming out of the Costas loop (i.e. after
    carrier recovery, before slicing/decoding) - this is the "is the modem
    actually locked" visual. A clean run shows four tight clusters; a
    still-acquiring or badly-tuned loop shows a smeared ring or a rotating
    blob instead.

GUI layout note
----------------
Both Message Edit Boxes have an explicit "GUI Hint" (0,0,1,1 for Send,
1,0,1,1 for Received) so Send always renders above Received. Without a GUI
Hint, GNU Radio Companion falls back to its own internal widget ordering,
which doesn't necessarily match canvas position or the order blocks appear
in the file.
