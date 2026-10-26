"""
FMlink audio-subcarrier PHY: symbols <-> real audio, for driving a
radio's audio port (FM transceivers - see ota_sim's FMLink and
tasklist.txt's Phase 5) instead of exchanging complex baseband IQ.

An audio path has no DC response and only passes a few kHz, so the
complex baseband signal fmlink_pulse.py produces can't go through it
directly. TX pulse-shapes at the audio sample rate (sps = sample_rate /
symbol_rate, so each mode's symbol rate must divide it) and mixes the
result onto an audio carrier inside the radio's passband; RX mixes back
down and matched-filters, leaving the same symbol stream the IQ path
produces, for the same demodulators/sync/FEC to consume.

Unlike the IQ path, nothing about the received burst's timing, carrier
phase or level can be assumed: radio filters add group delay, key-up
and squelch eat the start of the burst, and the audio chain rotates the
carrier's phase arbitrarily. demodulate() recovers all three from one
cross-correlation against the frame's known preamble+sync-word symbols.
The correlation peak's position is the symbol timing; its complex value
is the channel gain and phase. That resolves QPSK/16-QAM's rotational
ambiguity outright - the reference IS the transmitted marker - and it
gives a data-aided SNR estimate (error vector against known symbols)
instead of fmlink_modem.estimate_snr_db()'s blind one.

After the marker, a decision-directed loop tracks drift. The two ends'
sound cards never run at exactly the same rate, and even 20 ppm rotates
the carrier past QPSK's 45 degree tolerance within a few seconds, so a
one-shot estimate alone fails on long bursts. In an audio modem the
carrier and the symbol clock both come from the same sample clocks, so
one clock-offset error eps shows up as BOTH a carrier frequency offset
(-carrier_hz * eps) and a symbol-timing drift (sps * eps samples per
symbol). A second-order phase-locked loop tracks the carrier phase, and
its frequency term is converted back to eps to steer symbol timing too:
one loop, no separate timing-recovery loop.
"""
import numpy as np

import fmlink_link as link
import fmlink_pulse as pulse

AUDIO_SAMPLE_RATE = 48000

# Where each radio audio connection puts the carrier, and the highest
# symbol rate it carries. An RRC signal is symbol_rate * (1 + alpha) wide.
#   mic:  nominally 300-3000 Hz (ota_sim's PORT_PRESETS). 1650 Hz centers
#         600/1200 baud (1200 baud at 840-2460 Hz). 2400 baud (380-3620 Hz)
#         overhangs that, but measured through two HTs' mic/speaker paths
#         (2026-10-07: flat to ~2.3 kHz, -5 to -13 dB at 3 kHz, nothing
#         above ~4 kHz) mode 3 on a 2000 Hz carrier delivered 5/5 each way
#         at 11.4 and 7.8 dB, so the mic port allows it, on 2000 Hz.
#         Also fine through a SignaLink (2026-10-24): mode 3 repeatedly
#         pinged 0/5 (no_reply) there at first, and a radio_response.py
#         sweep measured its upper edge (3620 Hz) at -36 dB vs -11/-17 dB
#         for mode 2's edges, looking like a real bandwidth limit - but
#         the receiving station's rx thread was silently dying on an ALSA
#         overrun mid-test (fixed: RadioTransport._rx_loop now reopens the
#         capture instead of going deaf for good) and running stale code
#         without that fix. With both stations healthy and current, mode
#         3 ran 5/5 at 19.5 dB, same as the other port. The -36 dB sweep
#         reading was very likely a fade caught during that unhealthy
#         period, not a SignaLink characteristic.
#   data: ~10 Hz-5 kHz flat, so 2000 Hz carries 2400 baud at 380-3620 Hz,
#         clear of the upper roll-off.
# carrier_hz is the port's carrier; carriers_by_rate overrides it for a
# symbol rate (see carrier_for()).
PORT_PROFILES = {
    'mic': {'carrier_hz': 1650.0, 'max_symbol_rate': 2400, 'carriers_by_rate': {2400: 2000.0}},
    'data': {'carrier_hz': 2000.0, 'max_symbol_rate': 2400},
}


def carrier_for(port, symbol_rate):
    """The carrier (Hz) a port uses at symbol_rate."""
    prof = PORT_PROFILES[port]
    return prof.get('carriers_by_rate', {}).get(symbol_rate, prof['carrier_hz'])

# Silence before the burst's first symbol: the receiver loses ~100-300 ms
# to transmitter key-up (TXDELAY) plus squelch opening, and a frame's own
# 24-byte lead pad is far shorter than that at audio symbol rates.
DEFAULT_LEAD_IN_S = 0.25
# Silence after the last symbol so the TX filters' ringing finishes
# before the transmitter unkeys.
DEFAULT_TAIL_S = 0.02
# Peak audio level, sound-card full scale = 1.0. FM converts level to
# deviation; this leaves headroom below the radio's limiter even after
# the mic path's pre-emphasis lifts the upper half of the signal.
DEFAULT_LEVEL = 0.7

ALPHA = 0.35
SPAN_SYMBOLS = 11

# Tracking loop: normalized noise bandwidth (per symbol) and damping of
# the second-order phase-locked loop. Narrow enough that noise and
# decision errors barely move it; wide enough to follow a few hundred ppm
# of clock offset.
LOOP_BANDWIDTH = 0.005
LOOP_DAMPING = 0.707

# Receive equalizer: an FIR on the matched-filter output with taps every
# half symbol, EQ_SPAN_SYMBOLS symbols each side of the centre, fitted by
# least squares to the burst's known marker symbols (EQ_RIDGE keeps the
# fit from chasing noise). It undoes the radios' audio response - an HT's
# mic/speaker path tilts several dB across the band (see RADIO_SETUP.md
# "Frequency response"), which otherwise smears symbols into each other.
EQ_SPAN_SYMBOLS = 2
EQ_RIDGE = 0.001


def frame_for_audio(frame_bytes):
    """A build_frame()-style frame as the audio PHY sends it: the 16-byte-
    preamble marker (link.AUDIO_MARKER), header, payload and CRC - no lead
    or end padding. The IQ PHY's padding exists for its fixed-timing
    matched filter; here the lead-in silence and RRC tails cover the ends,
    and the receiver finds the marker by correlating against the samples,
    for which 16 bytes of preamble is enough. Encode the result with
    fec.encode_frame(..., marker=link.AUDIO_MARKER). A frame already in
    audio form is returned unchanged."""
    start = frame_bytes.find(link.IQ_MARKER)
    if start < 0:
        return frame_bytes
    header_at = start + len(link.IQ_MARKER)
    header = link.parse_header(frame_bytes[header_at:header_at + link.HEADER_LEN])
    end = header_at + link.HEADER_LEN + header['payload_len'] + link.CRC_LEN
    return link.AUDIO_MARKER + frame_bytes[header_at:end]


def samples_per_symbol(symbol_rate, sample_rate=AUDIO_SAMPLE_RATE):
    sps = sample_rate / symbol_rate
    if sps != int(sps) or sps < 2:
        raise ValueError(f"{symbol_rate} sym/s doesn't divide {sample_rate} Hz into a whole "
                         f"number (>= 2) of samples per symbol")
    return int(sps)


def modulate(symbols, symbol_rate, carrier_hz, sample_rate=AUDIO_SAMPLE_RATE,
             lead_in_s=DEFAULT_LEAD_IN_S, tail_s=DEFAULT_TAIL_S, level=DEFAULT_LEVEL):
    """Complex symbols -> real audio samples in [-level, level]: lead-in
    silence, RRC-shaped symbols on carrier_hz, tail silence."""
    sps = samples_per_symbol(symbol_rate, sample_rate)
    taps = pulse.rrc_taps(ALPHA, sps, SPAN_SYMBOLS)
    upsampled = np.zeros(len(symbols) * sps, dtype=complex)
    upsampled[::sps] = symbols
    baseband = _fft_convolve(upsampled, taps)  # == pulse.pulse_shape(), just faster
    t = np.arange(len(baseband)) / sample_rate
    passband = np.real(baseband * np.exp(2j * np.pi * carrier_hz * t))
    peak = np.max(np.abs(passband)) if len(passband) else 0.0
    if peak > 0:
        passband *= level / peak
    lead = np.zeros(int(round(lead_in_s * sample_rate)))
    tail = np.zeros(int(round(tail_s * sample_rate)))
    return np.concatenate([lead, passband, tail])


def demodulate(audio, symbol_rate, carrier_hz, reference_symbols, pre_symbols=0,
               constellation=None, sample_rate=AUDIO_SAMPLE_RATE, equalize=True):
    """Real audio -> (symbols, info), symbols normalized so the known
    reference lands on its ideal points (unit-energy constellation, zero
    phase). reference_symbols is the frame's marker as transmitted (e.g.
    the preamble+sync word modulated with this burst's modulation);
    symbols start pre_symbols before it (so a frame's lead pad is
    included, as the IQ path's would be) and run to the end of the audio.
    constellation (the modulation's ideal points) enables drift tracking
    past the marker - see the module docstring; without it, every symbol
    uses the marker's one-shot timing and phase.

    info: 'quality' (normalized correlation peak, 0..1 - near 1 for a
    clean burst, ~0.1 or less for noise or the wrong mode/rate), 'snr_db'
    (data-aided, over the reference symbols), 'timing' (sample index of
    the reference's first symbol in the matched-filter output),
    'marker_index' (index of the reference's first symbol in the returned
    symbols - pre_symbols, unless the audio began too late for all of
    them).
    Returns (empty array, info with quality 0) if the audio is shorter
    than the reference."""
    sps = samples_per_symbol(symbol_rate, sample_rate)
    ref = np.asarray(reference_symbols, dtype=complex)
    empty = (np.zeros(0, dtype=complex), {'quality': 0.0, 'snr_db': float('-inf'), 'timing': None,
                                          'marker_index': 0})

    audio = np.asarray(audio, dtype=float)
    t = np.arange(len(audio)) / sample_rate
    # sqrt(2) undoes the real-part's halving of the passband amplitude.
    mixed = np.sqrt(2) * audio * np.exp(-2j * np.pi * carrier_hz * t)
    y = _fft_convolve(mixed, pulse.rrc_taps(ALPHA, sps, SPAN_SYMBOLS))  # == pulse.matched_filter()

    template = np.zeros(len(ref) * sps - (sps - 1), dtype=complex)
    template[::sps] = ref
    n_valid = len(y) - len(template) + 1
    if len(ref) == 0 or n_valid <= 0:
        return empty
    n_fft = 1 << int(np.ceil(np.log2(len(y) + len(template))))
    corr = np.fft.ifft(np.fft.fft(y, n_fft) * np.conj(np.fft.fft(template, n_fft)))[:n_valid]

    # Normalized correlation: |sum(y*conj(ref))| / sqrt(sum|ref|^2 * sum|y|^2)
    # over the same samples, so a loud noise burst can't out-score a
    # quieter real signal.
    # Energy at the template's nonzero taps only (every sps-th sample).
    mask = (template != 0).astype(float)
    window_energy = np.fft.irfft(np.fft.rfft(np.abs(y) ** 2, n_fft) *
                                 np.conj(np.fft.rfft(mask, n_fft)), n_fft)[:n_valid]
    ref_energy = np.sum(np.abs(ref) ** 2)
    floor = 1e-9 * max(np.max(window_energy), 1e-30)
    quality = np.abs(corr) / np.sqrt(ref_energy * np.maximum(window_energy, floor))
    timing = int(np.argmax(quality))

    gain = corr[timing] / ref_energy  # complex channel gain (level + carrier phase)
    if abs(gain) < 1e-12:
        return empty
    y = y / gain
    eq_taps = 0
    if equalize:
        y, eq_taps = _equalize(y, timing, sps, ref)
    # Symbols before the marker (the frame's lead pad) at the marker's
    # one-shot timing/phase - only a few dozen, so drift is negligible.
    n_pre = min(pre_symbols, timing // sps)
    before = y[timing - n_pre * sps:timing:sps]

    # Residual carrier frequency across the marker (radians/symbol): the
    # phase change between the marker's first and second halves, once the
    # known data is removed. A sound-card clock offset leaves exactly this.
    # The one-shot gain above only fixes the marker's AVERAGE phase, so
    # without de-rotating, a long marker under drift reads its phase error
    # as noise (measured: ~5 dB low at 100 ppm, 600 baud). Halves rather
    # than neighbouring symbols: the estimate is ~N/2 times less noisy.
    marker = y[timing:timing + len(ref) * sps:sps]
    residual = marker * np.conj(ref)
    half = len(ref) // 2
    freq = (float(np.angle(np.sum(residual[half:2 * half]) * np.conj(np.sum(residual[:half])))) / half
            if half else 0.0)
    center = (len(ref) - 1) / 2
    derotated = marker * np.exp(-1j * freq * (np.arange(len(ref)) - center))
    noise = np.mean(np.abs(derotated - ref) ** 2)
    if eq_taps:
        # The equalizer was fitted to these very symbols, which flatters
        # them: a least-squares fit of p taps to N points leaves about
        # (N - p) / N of the true error. Undo that, so the SNR (and the
        # mode --auto picks from it) reflects the rest of the burst.
        noise *= len(ref) / max(len(ref) - eq_taps, 1)
    signal = np.mean(np.abs(ref) ** 2)
    snr_db = float(10 * np.log10(signal / max(noise, 1e-12)))

    if constellation is None:
        after = y[timing::sps]
    else:
        # The tracker starts from zero, not from freq: over a marker this
        # short, freq is good enough to de-rotate an SNR measurement but far
        # too noisy to seed the loop (it also steers symbol timing, and a
        # bad seed walks the timing off - measured: mode 2 delivery under
        # fading fell from 333/360 to 217/360 when seeded).
        after = _track(y, timing, sps, ref, np.asarray(list(constellation), dtype=complex),
                       -symbol_rate / (2 * np.pi * carrier_hz))
    symbols = np.concatenate([before, after])
    return symbols, {'quality': float(quality[timing]), 'snr_db': snr_db, 'timing': timing,
                     'marker_index': n_pre}


def _equalize(y, timing, sps, ref):
    """y (matched-filter output, already scaled by the marker's gain) put
    through an FIR fitted to the marker: taps every sps/2 samples,
    EQ_SPAN_SYMBOLS symbols each side, least squares (ridge EQ_RIDGE) from
    the samples around each marker symbol to the symbol transmitted.
    Returns (equalized y, number of taps), or (y, 0) unchanged when the
    marker is too close to either end of the audio."""
    step = max(1, sps // 2)
    offsets = np.arange(-2 * EQ_SPAN_SYMBOLS, 2 * EQ_SPAN_SYMBOLS + 1) * step
    centers = timing + np.arange(len(ref)) * sps
    if centers[0] + offsets[0] < 0 or centers[-1] + offsets[-1] >= len(y):
        return y, 0
    a = y[centers[:, None] + offsets[None, :]]
    gram = a.conj().T @ a
    ridge = EQ_RIDGE * np.real(np.trace(gram)) / len(offsets)
    w = np.linalg.solve(gram + ridge * np.eye(len(offsets)), a.conj().T @ ref)
    out = np.zeros_like(y)
    for tap, d in zip(w, offsets):
        if d >= 0:
            out[:len(y) - d] += tap * y[d:]
        else:
            out[-d:] += tap * y[:len(y) + d]
    return out, len(offsets)


def _fft_convolve(x, taps):
    """Full linear convolution, as np.convolve(x, taps, 'full'), via FFT:
    at audio sample rates the RRC filter is hundreds of taps long (880 at
    600 baud), where direct convolution dominates a burst's processing time."""
    n = len(x) + len(taps) - 1
    if len(x) == 0:
        return np.zeros(len(taps) - 1, dtype=complex)
    n_fft = 1 << int(np.ceil(np.log2(n)))
    return np.fft.ifft(np.fft.fft(x, n_fft) * np.fft.fft(taps, n_fft))[:n]


def _track(y, timing, sps, ref, points, eps_per_rad):
    """Second-order decision-directed PLL over the symbol stream from the
    marker onward (data-aided on the marker itself, where the transmitted
    symbols are known). The loop keeps running past the frame's end into
    whatever follows (squelch-tail noise), so its final state says nothing
    reliable - only the symbols are returned. eps_per_rad converts the
    loop's frequency term (radians/symbol) to the clock-offset fraction eps
    that also drives the sampling position."""
    theta_n = LOOP_BANDWIDTH / (LOOP_DAMPING + 1 / (4 * LOOP_DAMPING))
    denom = 1 + 2 * LOOP_DAMPING * theta_n + theta_n ** 2
    k_p = 4 * LOOP_DAMPING * theta_n / denom
    k_i = 4 * theta_n ** 2 / denom

    phase, freq = 0.0, 0.0
    pos = float(timing)
    out = []
    while True:
        idx = int(round(pos))
        if idx >= len(y):
            break
        z = y[idx] * np.exp(-1j * phase)
        n = len(out)
        decision = ref[n] if n < len(ref) else points[np.argmin(np.abs(points - z))]
        out.append(z)
        error = np.angle(z * np.conj(decision)) if decision != 0 else 0.0
        freq += k_i * error
        phase += freq + k_p * error
        pos += sps * (1 + freq * eps_per_rad)
    return np.asarray(out, dtype=complex)


def audio_to_pcm16(audio):
    """Float audio in [-1, 1] -> mono 16-bit little-endian PCM bytes (the
    ota_sim FM channel's wire format)."""
    return np.clip(np.round(np.asarray(audio) * 32767.0), -32768, 32767).astype('<i2').tobytes()


def pcm16_to_audio(data):
    """Inverse of audio_to_pcm16()."""
    data = data[:len(data) - len(data) % 2]
    return np.frombuffer(data, dtype='<i2').astype(float) / 32768.0



# -- per-burst link quality, for fading-aware mode selection ----------------
# A burst is "fading" when more than FADE_FRACTION_THRESHOLD of its
# FADE_WINDOW_S-long stretches sit FADE_DEPTH_DB or more below the burst's
# median SNR. Measured per probe through ota_sim's FM model: without fading
# that fraction stayed <= 0.015 at every CNR from 8 dB up (it rises to
# ~0.05 at 6 dB, the squelch edge, where being conservative is right
# anyway); with 30-150 km/h Rayleigh fading it was >= 0.064 at every CNR
# tested. 5 ms is about one fade's length at those speeds.
FADE_WINDOW_S = 0.005
FADE_DEPTH_DB = 6.0
FADE_FRACTION_THRESHOLD = 0.03


def burst_quality(received, reference, symbol_rate):
    """Link quality over a whole decoded burst, from exact per-symbol
    errors against the symbols actually transmitted (reference - e.g. a
    CRC-verified frame rebuilt and re-encoded). Returns {'snr_db': average
    SNR over the burst, 'fade_fraction': see above, 'fading': bool}."""
    errors = np.abs(np.asarray(received) - np.asarray(reference)) ** 2
    snr_db = float(10 * np.log10(1 / max(np.mean(errors), 1e-12)))
    window = max(1, int(round(FADE_WINDOW_S * symbol_rate)))
    if len(errors) < 2 * window:
        return {'snr_db': snr_db, 'fade_fraction': 0.0, 'fading': False}
    windowed = 10 * np.log10(1 / np.maximum(
        np.convolve(errors, np.ones(window) / window, mode='valid'), 1e-12))
    fade_fraction = float(np.mean(windowed < np.median(windowed) - FADE_DEPTH_DB))
    return {'snr_db': snr_db, 'fade_fraction': fade_fraction,
            'fading': fade_fraction > FADE_FRACTION_THRESHOLD}
