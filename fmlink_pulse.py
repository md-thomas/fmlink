"""
FMlink RRC pulse shaping: symbols <-> real timed samples.

This is where symbol_rate/sps actually start to matter (see
fmlink_modem.py's docstring for why they deliberately don't appear at the
bits<->symbols stage). Needs numpy for the convolution - fmlink_link.py and
fmlink_modem.py stay dependency-free, but real DSP is the point where that
stops being practical to avoid.

Roll-off alpha=0.35 and the 11-symbol filter span match the convention
already used in tx_pluto.grc/phy_sim_loopback.grc's
firdes.root_raised_cosine(...) calls, so results here are comparable to the
existing GNU Radio chain even though this module doesn't depend on GNU Radio
and isn't trying to bit-for-bit reproduce firdes's internal normalization.

Key fact this module leans on: a single RRC filter is NOT zero-ISI by
itself - only the *cascade* of a TX RRC filter and a matched (identical) RX
RRC filter is (that cascade is a full raised-cosine response). So a clean,
correct constellation only appears after pulse_shape() -> matched_filter()
-> decimate() at group_delay(), not from pulse_shape() alone. rrc_taps() is
normalized to unit energy (sum(taps**2) == 1) specifically so that this
round trip reproduces the original symbol values almost exactly (see
tests/test_fmlink_pulse.py) - that's what makes the matched-filter cascade
recover clean points instead of some arbitrarily-scaled version of them.
"""
import numpy as np


def rrc_taps(alpha=0.35, sps=4, span_symbols=11):
    """Generate root-raised-cosine filter taps, normalized to unit energy
    (sum(taps**2) == 1). Length is span_symbols*sps, forced to the next odd
    number (a symmetric/linear-phase FIR needs an odd tap count so it has a
    single, exact center sample - its group delay)."""
    ntaps = span_symbols * sps
    if ntaps % 2 == 0:
        ntaps += 1
    t = (np.arange(ntaps) - (ntaps - 1) / 2) / sps  # time axis in symbol periods (Ts=1)

    taps = np.zeros(ntaps)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-8:
            taps[i] = (1 - alpha) + 4 * alpha / np.pi
        elif alpha != 0 and abs(abs(ti) - 1 / (4 * alpha)) < 1e-8:
            taps[i] = (alpha / np.sqrt(2)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha)) +
                (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha))
            )
        else:
            num = (np.sin(np.pi * ti * (1 - alpha)) +
                   4 * alpha * ti * np.cos(np.pi * ti * (1 + alpha)))
            den = np.pi * ti * (1 - (4 * alpha * ti) ** 2)
            taps[i] = num / den

    taps /= np.sqrt(np.sum(taps ** 2))
    return taps


def group_delay(alpha=0.35, sps=4, span_symbols=11):
    """Sample offset of the first true symbol's peak in a pulse_shape() ->
    matched_filter() cascade's output - i.e. what to pass as decimate()'s
    offset. Each RRC filter (length ntaps, linear phase) delays by
    (ntaps-1)/2 samples; two in cascade delay by ntaps-1 total."""
    ntaps = len(rrc_taps(alpha, sps, span_symbols))
    return ntaps - 1


def pulse_shape(symbols, alpha=0.35, sps=4, span_symbols=11):
    """Upsample symbols by sps (zero-stuffing) and convolve with the RRC
    taps (full convolution - no truncation, so edge symbols aren't lost).
    Returns a numpy complex128 array of oversampled baseband samples,
    length len(symbols)*sps + ntaps - 1."""
    taps = rrc_taps(alpha, sps, span_symbols)
    if len(symbols) == 0:
        return np.zeros(len(taps) - 1, dtype=complex)
    upsampled = np.zeros(len(symbols) * sps, dtype=complex)
    upsampled[::sps] = symbols
    return np.convolve(upsampled, taps, mode='full')


def matched_filter(samples, alpha=0.35, sps=4, span_symbols=11):
    """Convolve samples with the same RRC taps again (the matched filter) -
    still oversampled, doesn't decimate. See module docstring for why this
    cascade (not pulse_shape() alone) is what's zero-ISI at symbol-spaced
    sampling."""
    taps = rrc_taps(alpha, sps, span_symbols)
    return np.convolve(samples, taps, mode='full')


def decimate(samples, sps, offset):
    """Pick every sps-th sample starting at offset - collapses an
    oversampled signal back down to one sample per symbol."""
    return samples[offset::sps]


def expected_symbol_count(num_shaped_samples, alpha=0.35, sps=4, span_symbols=11):
    """Given the length of a pulse_shape() output, return how many original
    symbols it represents - the inverse of pulse_shape()'s length formula
    (len(symbols)*sps + ntaps - 1). matched_filter()+decimate() alone can't
    tell real symbols apart from the filter's trailing ringing past the end
    of the real signal; this is how a caller (matchedfilter.py) knows where
    to stop without needing separate metadata alongside the sample file."""
    ntaps = len(rrc_taps(alpha, sps, span_symbols))
    return (num_shaped_samples - (ntaps - 1)) // sps
