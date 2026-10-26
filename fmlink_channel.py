"""
FMlink channel simulation: inject noise onto real baseband sample files,
to test the modem/link-layer chain under realistic conditions instead of a
clean round trip.

Operates at the sample level (post pulse-shaping) - noise is a sample-level
phenomenon, not a symbol-level one, so this deliberately sits between
pulseshape.py and matchedfilter.py in the pipeline, not before modulate.py.
See fmlink_pulse.py's module docstring for the equivalent reasoning about
why symbol_rate has no meaning before pulse shaping.

AWGN only for now, matching the archived phy_sim_loopback.grc's
channels_channel_model_0 block, which also only simulated noise (frequency/
timing offset left at none - see legacy/gnuradio/README.md).
"""
import numpy as np


def add_awgn(samples, noise_voltage, seed=None):
    """Add complex AWGN to samples: independent real and imaginary noise
    components, each ~ N(0, noise_voltage^2). Matches
    channels_channel_model_0's noise_voltage parameter convention/name, so
    results are comparable to the existing .grc flowgraphs. noise_voltage=0
    returns samples unchanged (as complex128, not necessarily the same
    dtype/object). seed makes the noise reproducible across runs - None
    (default) uses fresh entropy each call."""
    samples = np.asarray(samples, dtype=complex)
    if noise_voltage == 0:
        return samples.copy()
    rng = np.random.default_rng(seed)
    noise = (rng.normal(0, noise_voltage, len(samples)) +
             1j * rng.normal(0, noise_voltage, len(samples)))
    return samples + noise


def snr_db_to_noise_voltage(samples, snr_db):
    """Compute the per-component (I and Q) noise standard deviation needed
    to hit the given SNR (dB), measured against samples' own average power
    (mean(|s|^2)) rather than an assumed constellation amplitude - this
    makes it meaningful regardless of which pipeline stage samples came
    from (raw symbols or oversampled pulse-shaped output).

    SNR = signal_power / noise_power, where noise_power = 2*noise_voltage^2
    (both I and Q components contribute independently)."""
    samples = np.asarray(samples, dtype=complex)
    signal_power = np.mean(np.abs(samples) ** 2)
    snr_linear = 10 ** (snr_db / 10)
    noise_power = signal_power / snr_linear
    return float(np.sqrt(noise_power / 2))
