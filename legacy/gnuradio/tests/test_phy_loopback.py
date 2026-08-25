"""
Headless, hardware-free regression tests for the OpenWave link layer, run over
phy_sim_loopback.grc's simulated channel (TX -> channel model -> RX, all in one
process). These are real GNU Radio flowgraph runs, not just calls into
openwave_link.py directly (see test_openwave_link.py for the fast pure-Python
tests) - they're slower and use wall-clock sleeps to let data flow through the
scheduler, so some timing slop is inherent. If a test is ever flaky, prefer
increasing its sleep margins over just re-running.

IMPORTANT: must be run with the actual repository root as the current
working directory - NOT legacy/gnuradio/. The generated epy_blocks resolve
`import openwave_link` via os.getcwd() (GRC's block-introspection step
doesn't define __file__, see openwave_link import lines in the .grc
epy_block sources), and openwave_link.py lives at the repo root, not
alongside this archived flowgraph.

    cd <repo-root>
    python3 -m unittest discover -s legacy/gnuradio/tests -v
"""
import json
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import importlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# Directory the archived .grc file actually lives in.
FLOWGRAPH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Real repo root, where openwave_link.py lives - this test process's own
# import AND the compiled epy_block's os.getcwd()-based import both need it.
REPO_ROOT = os.path.dirname(os.path.dirname(FLOWGRAPH_DIR))
sys.path.insert(0, REPO_ROOT)
import openwave_link as link
GRC_FILE = 'phy_sim_loopback.grc'
MODULE_NAME = 'phy_sim_loopback'

try:
    import yaml
    from PyQt5 import Qt
    from gnuradio import blocks
    import pmt
    DEPS_AVAILABLE = True
except ImportError:
    DEPS_AVAILABLE = False

_qapp = None


def _get_qapp():
    global _qapp
    if _qapp is None:
        _qapp = Qt.QApplication(sys.argv[:1])
    return _qapp


def _compile_flowgraph(overrides, out_dir):
    """Compile GRC_FILE to out_dir. overrides is {block_name: {param: value}},
    applied to a patched copy of the .grc before compiling - lets tests dial in
    e.g. heavier noise or a different symbol_rate without touching the real file.
    A params dict of exactly {'_disabled': True} disables that block entirely
    (GRC drops it and its connections from the compiled flowgraph) instead of
    patching its parameters - useful for genuinely removing a signal path
    rather than just changing a value."""
    src_path = os.path.join(FLOWGRAPH_DIR, GRC_FILE)
    if overrides:
        with open(src_path) as f:
            fg = yaml.safe_load(f)
        for block_name, params in overrides.items():
            for b in fg['blocks']:
                if b['name'] == block_name:
                    if params == {'_disabled': True}:
                        b['states']['state'] = 'disabled'
                    else:
                        b['parameters'].update(params)
                    break
            else:
                raise ValueError(f"override target block {block_name!r} not found in {GRC_FILE}")
        src_path = os.path.join(out_dir, GRC_FILE)
        with open(src_path, 'w') as f:
            yaml.dump(fg, f, default_flow_style=False, sort_keys=False)

    result = subprocess.run(
        ['grcc', src_path, '-o', out_dir],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"grcc failed:\n{result.stdout}\n{result.stderr}")


def _load_flowgraph_module(out_dir):
    if out_dir not in sys.path:
        sys.path.insert(0, out_dir)
    if MODULE_NAME in sys.modules:
        del sys.modules[MODULE_NAME]  # force a fresh load from this out_dir
    return importlib.import_module(MODULE_NAME)


def run_loopback(messages, overrides=None, settle_s=0.5, per_msg_s=0.4, drain_s=0.6):
    """Compile+run phy_sim_loopback.grc with optional block-parameter overrides,
    send each of `messages` as a separate burst, and return (decoded, stats):
    lists of the text/stats strings RX published, in arrival order.

    Pins the flowgraph's own 'msg' parameter default to messages[0], since the
    Send box publishes its default value once automatically at startup - without
    this, every test would see one extra, untracked decode.
    """
    overrides = dict(overrides or {})
    if messages:
        overrides.setdefault('msg', {'value': json.dumps(messages[0])})

    with tempfile.TemporaryDirectory() as out_dir:
        _compile_flowgraph(overrides, out_dir)
        fgmod = _load_flowgraph_module(out_dir)

        _get_qapp()
        tb = fgmod.phy_sim_loopback()
        msg_dbg = blocks.message_debug()
        stats_dbg = blocks.message_debug()
        tb.msg_connect((tb.stream_display_sink_0, 'msg'), (msg_dbg, 'store'))
        tb.msg_connect((tb.stream_display_sink_0, 'stats'), (stats_dbg, 'store'))

        tb.start()
        time.sleep(settle_s)
        for m in messages:
            tb.str_to_pdu_0.to_basic_block()._post(pmt.intern('msg'), pmt.string_to_symbol(m))
            time.sleep(per_msg_s)
        time.sleep(drain_s)
        tb.stop()
        tb.wait()

        decoded = [pmt.symbol_to_string(msg_dbg.get_message(i)) for i in range(msg_dbg.num_messages())]
        stats = [pmt.symbol_to_string(stats_dbg.get_message(i)) for i in range(stats_dbg.num_messages())]
        return decoded, stats


_SKIP_REASON = None
if not DEPS_AVAILABLE:
    _SKIP_REASON = 'gnuradio/PyQt5/pyyaml not available'
elif shutil.which('grcc') is None:
    _SKIP_REASON = 'grcc not installed'


@unittest.skipIf(_SKIP_REASON, _SKIP_REASON)
class CleanChannelTests(unittest.TestCase):
    def test_message_decodes_correctly(self):
        msg = "OpenWave regression test message"
        decoded, _ = run_loopback([msg])
        self.assertIn(msg, decoded)

    def test_multiple_messages_all_decode(self):
        messages = ["First", "Second message", "Three!"]
        decoded, _ = run_loopback(messages)
        for m in messages:
            self.assertIn(m, decoded, f"{m!r} never decoded")

    def test_empty_channel_stats_show_zero_failures(self):
        msg = "clean channel stats check"
        _, stats = run_loopback([msg])
        self.assertTrue(stats, "expected at least one stats update")
        self.assertIn('CRC fail:0', stats[-1])
        self.assertIn('Bad length:0', stats[-1])


@unittest.skipIf(_SKIP_REASON, _SKIP_REASON)
class NoisyChannelTests(unittest.TestCase):
    """Noise level/mode chosen to reliably produce some corruption - see
    the equivalent manual test earlier in this project's history, which
    empirically confirmed 0.5 noise_voltage at 9600 baud reliably drops some
    fraction of frames."""

    NOISY_OVERRIDES = {
        'channels_channel_model_0': {'noise_voltage': '0.5'},
        'symbol_rate': {'value': '9600'},
    }

    def test_no_garbled_messages_under_heavy_noise(self):
        """The core 'solid link layer' property: RX should never publish
        anything except the exact original text - corrupted frames get
        silently rejected by CRC, not passed through as garbage."""
        msg = "OpenWave pi/4-QPSK PHY test 123!"
        decoded, stats = run_loopback([msg] * 15, self.NOISY_OVERRIDES, per_msg_s=0.3)
        garbled = [d for d in decoded if d != msg]
        self.assertEqual(garbled, [], f"CRC gate let corrupted text through: {garbled!r}")
        # confirm the noise was actually heavy enough to matter, so this test
        # would catch a regression that broke corruption instead of trivially
        # passing because nothing bad ever happened
        self.assertTrue(stats, "expected stats updates")
        self.assertNotIn('CRC fail:0', stats[-1],
                          "expected at least one CRC failure at this noise level")


@unittest.skipIf(_SKIP_REASON, _SKIP_REASON)
class SequenceAndFlagsTests(unittest.TestCase):
    def test_ack_requested_flag_visible_in_rx_stats(self):
        msg = "seq test"
        overrides = {'str_to_pdu_0': {'ack_requested': 'True'}}
        decoded, stats = run_loopback([msg] * 3, overrides)
        self.assertIn(msg, decoded)
        self.assertTrue(any('ack_requested' in s for s in stats),
                         "ack_requested flag never showed up in RX stats")

    def test_lost_frame_count_tracks_sequence_gaps(self):
        msg = "seq gap test"
        overrides = {
            'channels_channel_model_0': {'noise_voltage': '0.5'},
            'symbol_rate': {'value': '9600'},
        }
        _, stats = run_loopback([msg] * 15, overrides, per_msg_s=0.3)
        self.assertTrue(stats, "expected stats updates")
        self.assertNotIn('Lost:0', stats[-1],
                          "expected some lost-frame gaps to be inferred at this noise level")

    def test_ack_requested_message_gets_acknowledged(self):
        """With ack_responder_0 wired in (RX's ack_needed -> responder -> TX's
        pdu input -> channel -> RX again -> ack_received -> original TX), a
        clean-channel ack_requested message should get acknowledged: no
        delivery_failed, and RX's own stats should show it received the
        looped-back ACK frame too."""
        msg = "ack loop test"
        overrides = {'str_to_pdu_0': {'ack_requested': 'True'}}
        with tempfile.TemporaryDirectory() as out_dir:
            _compile_flowgraph(overrides, out_dir)
            fgmod = _load_flowgraph_module(out_dir)

            _get_qapp()
            tb = fgmod.phy_sim_loopback()
            msg_dbg = blocks.message_debug()
            stats_dbg = blocks.message_debug()
            failed_dbg = blocks.message_debug()
            tb.msg_connect((tb.stream_display_sink_0, 'msg'), (msg_dbg, 'store'))
            tb.msg_connect((tb.stream_display_sink_0, 'stats'), (stats_dbg, 'store'))
            tb.msg_connect((tb.str_to_pdu_0, 'delivery_failed'), (failed_dbg, 'store'))

            tb.start()
            time.sleep(0.5)
            tb.str_to_pdu_0.to_basic_block()._post(pmt.intern('msg'), pmt.string_to_symbol(msg))
            time.sleep(2.0)
            tb.stop()
            tb.wait()

            decoded = [pmt.symbol_to_string(msg_dbg.get_message(i)) for i in range(msg_dbg.num_messages())]
            stats = [pmt.symbol_to_string(stats_dbg.get_message(i)) for i in range(stats_dbg.num_messages())]

            self.assertIn(msg, decoded)
            self.assertEqual(failed_dbg.num_messages(), 0,
                              "message should have been acknowledged, not given up on")
            self.assertTrue(stats)
            self.assertNotIn('ACKs rx:0', stats[-1],
                              "RX should have received the looped-back ACK frame")

    def test_ack_requested_message_exhausts_retries_and_gives_up_with_no_responder(self):
        """With ack_responder_0 explicitly disabled (so there's genuinely no
        return path - as is permanently the case for tx_pluto.grc talking to
        the real, transmit-incapable rx_rtlsdr.grc), an ack_requested message
        should reliably exhaust ack_max_retries and report delivery_failed,
        not hang waiting forever or silently succeed."""
        overrides = {
            'msg': {'value': json.dumps("never acked")},
            'ack_responder_0': {'_disabled': True},
            'str_to_pdu_0': {
                'ack_requested': 'True',
                'ack_max_retries': '1',
                'ack_timeout_s': '0.3',
            },
        }
        with tempfile.TemporaryDirectory() as out_dir:
            _compile_flowgraph(overrides, out_dir)
            fgmod = _load_flowgraph_module(out_dir)

            _get_qapp()
            tb = fgmod.phy_sim_loopback()
            failed_dbg = blocks.message_debug()
            tb.msg_connect((tb.str_to_pdu_0, 'delivery_failed'), (failed_dbg, 'store'))

            tb.start()
            # settle + 1 original send (auto-published) + up to 1 retry, each
            # bounded by the 0.3s timeout above, plus margin for the 0.5s tick
            # strobe granularity and normal burst propagation time.
            time.sleep(2.5)
            tb.stop()
            tb.wait()

            self.assertGreaterEqual(failed_dbg.num_messages(), 1,
                                     "expected delivery_failed to fire with no return path wired")


@unittest.skipIf(_SKIP_REASON, _SKIP_REASON)
class ModeTests(unittest.TestCase):
    def test_each_mode_still_decodes_cleanly(self):
        """All five OpenWave symbol-rate modes, at the default (near-noiseless)
        channel, should decode correctly. Catches PHY regressions that only
        show up at a particular mode's sample-rate/timing-recovery math.

        Timing margins are sized for the slowest mode (600 baud): a ~110-byte
        framed burst at 600 baud (150 bytes/sec effective) takes roughly 0.7s
        to transmit alone, before matched-filter/timing-recovery settling -
        the module-default per_msg_s/drain_s (sized for the file's much faster
        9600-baud default) would cut that off mid-burst.
        """
        for symbol_rate in ('600', '1200', '2400', '4800', '9600'):
            with self.subTest(symbol_rate=symbol_rate):
                msg = f"mode test {symbol_rate}"
                decoded, _ = run_loopback(
                    [msg], {'symbol_rate': {'value': symbol_rate}},
                    per_msg_s=1.0, drain_s=1.5,
                )
                self.assertIn(msg, decoded, f"mode {symbol_rate} baud failed to decode")


@unittest.skipIf(_SKIP_REASON, _SKIP_REASON)
class FragmentationTests(unittest.TestCase):
    def test_long_message_fragments_and_reassembles_over_the_air(self):
        """A message over MAX_PAYLOAD_LEN should transparently split into
        multiple over-the-air bursts and reassemble correctly at RX - this
        exercises the real burst-to-burst timing (throttle pacing, separate
        preamble/sync acquisition per fragment), not just the pure-Python
        build_fragmented_frames/FragmentReassembler logic already covered in
        test_openwave_link.py."""
        # comfortably over MAX_PAYLOAD_LEN (1024) to force exactly 2 fragments
        msg = "F" * (link.MAX_PAYLOAD_LEN + 400)
        decoded, stats = run_loopback(
            [msg], {'symbol_rate': {'value': '9600'}},
            per_msg_s=2.0, drain_s=2.0,
        )
        self.assertIn(msg, decoded)
        self.assertTrue(stats)
        self.assertIn('Frag abort:0', stats[-1])
        self.assertIn('Frag overflow:0', stats[-1])


if __name__ == '__main__':
    unittest.main()
