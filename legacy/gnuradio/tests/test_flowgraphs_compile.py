"""
Compile-check every archived .grc flowgraph (see legacy/gnuradio/README.md)
with grcc. Catches YAML typos, bad block parameter names/expressions,
missing connections, and wrong GNU Radio API usage - the class of bug that
otherwise only shows up the next time someone opens GNU Radio Companion.
Doesn't require any SDR hardware, just the gnuradio + gr-iio + gr-osmosdr
install.

IMPORTANT: must be run with the actual repository root as the current
working directory - NOT legacy/gnuradio/. The generated epy_blocks resolve
`import openwave_link` via os.getcwd(), and openwave_link.py lives at the
repo root, not alongside these archived flowgraphs.

    cd <repo-root>
    python3 -m unittest discover -s legacy/gnuradio/tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

# Directory the archived .grc files actually live in.
FLOWGRAPH_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Real repo root, where openwave_link.py lives - this test process's own
# import AND the compiled epy_block's os.getcwd()-based import both need it.
REPO_ROOT = os.path.dirname(os.path.dirname(FLOWGRAPH_DIR))
sys.path.insert(0, REPO_ROOT)
import openwave_link as link

# Explicit list, not a glob over *.grc - legacy/gnuradio/ also has older,
# unrelated prototype flowgraphs (test.grc, test1.grc, predating the current
# PHY/link-layer design) that aren't part of what we're maintaining here.
GRC_FILES = ['phy_sim_loopback.grc', 'tx_pluto.grc', 'rx_rtlsdr.grc']


@unittest.skipIf(shutil.which('grcc') is None, 'grcc not installed')
class FlowgraphCompileTests(unittest.TestCase):
    def _compile(self, grc_filename):
        with tempfile.TemporaryDirectory() as out_dir:
            src_path = os.path.join(FLOWGRAPH_DIR, grc_filename)
            result = subprocess.run(
                ['grcc', src_path, '-o', out_dir],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
            )
            return result


def _make_test(grc_filename):
    def test(self):
        result = self._compile(grc_filename)
        self.assertEqual(
            result.returncode, 0,
            f"grcc failed for {grc_filename}:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
    return test


# One test method per .grc file found in the project root, so a failure names
# the specific flowgraph rather than just "some flowgraph broke".
for _grc_filename in GRC_FILES:
    _test_name = 'test_compiles_' + _grc_filename.replace('.grc', '').replace('-', '_')
    setattr(FlowgraphCompileTests, _test_name, _make_test(_grc_filename))


class AccessCodeConsistencyTests(unittest.TestCase):
    """digital_correlate_access_code_tag_xx's access_code has to be a hardcoded
    literal in the .grc files - GRC's Import block doesn't actually share its
    namespace with other blocks' parameter expressions in this GNU Radio
    version (confirmed empirically: even a minimal `import math` / `math.pi`
    reproduction fails the same way), so it can't be a live reference to
    openwave_link.PREAMBLE/SYNC_WORD. This is the guard against that literal
    silently drifting out of sync if PREAMBLE or SYNC_WORD ever change."""

    EXPECTED = ''.join(format(b, '08b') for b in (link.PREAMBLE[-4:] + link.SYNC_WORD))

    def _access_code_in(self, grc_filename):
        path = os.path.join(FLOWGRAPH_DIR, grc_filename)
        with open(path) as f:
            fg = yaml.safe_load(f)
        for b in fg['blocks']:
            if b['id'] == 'digital_correlate_access_code_tag_xx':
                # the stored value is a valid python string literal (leading/
                # trailing quote chars included), so it embeds verbatim as
                # code rather than getting re-stringified - strip those to
                # compare against the bare bit string.
                raw = b['parameters']['access_code']
                self.assertTrue(raw.startswith("'") and raw.endswith("'"),
                                 f"access_code in {grc_filename} isn't a quoted literal: {raw!r}")
                return raw[1:-1]
        self.fail(f"no digital_correlate_access_code_tag_xx block found in {grc_filename}")

    def test_phy_sim_loopback_access_code_matches_module(self):
        self.assertEqual(self._access_code_in('phy_sim_loopback.grc'), self.EXPECTED)

    def test_rx_rtlsdr_access_code_matches_module(self):
        self.assertEqual(self._access_code_in('rx_rtlsdr.grc'), self.EXPECTED)


if __name__ == '__main__':
    unittest.main()
