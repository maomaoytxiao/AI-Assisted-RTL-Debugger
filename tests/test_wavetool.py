"""Tests for the agent-facing JSON CLI (scripts/wavetool.py).

Uses the golden milestone-2 database build/waves.db (make test && make db).
"""

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import wavetool                                          # noqa: E402

GOLDEN_DB = os.path.join(ROOT, "build", "waves.db")


class WavetoolTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(GOLDEN_DB):
            raise AssertionError("missing %s - run 'make db'" % GOLDEN_DB)

    def call(self, *argv):
        return wavetool.run(["--db", GOLDEN_DB] + list(argv))

    def test_find(self):
        out, code = self.call("find", "dut.*_ptr")
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual([r["hierarchical_name"] for r in out["result"]],
                         ["tb_sync_fifo.dut.rd_ptr", "tb_sync_fifo.dut.wr_ptr"])

    def test_value_with_units_and_ticks(self):
        a, _ = self.call("value", "dut.count", "100ns")
        b, _ = self.call("value", "dut.count", "100000")
        self.assertEqual(a["result"], b["result"])
        self.assertEqual(a["result"]["value"], "0100")

    def test_history(self):
        out, code = self.call("history", "dut.full", "100ns", "250ns")
        self.assertEqual(code, 0)
        self.assertEqual([(c["time"], c["value"]) for c in out["result"]["changes"]],
                         [(135000, "1"), (155000, "0"), (225000, "0")])

    def test_errors_are_structured(self):
        out, code = self.call("value", "rd_data", "0")
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["type"], "AmbiguousSignalError")
        self.assertEqual(len(out["error"]["candidates"]), 2)
        out, code = self.call("value", "wr_pt", "0")
        self.assertEqual(out["error"]["type"], "SignalNotFoundError")
        self.assertIn("tb_sync_fifo.dut.wr_ptr", out["error"]["suggestions"])
        out, code = wavetool.run(["--exp", "no_such_exp", "info"])
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["type"], "WaveformQueryError")
        out, code = wavetool.run(["--exp", "../etc", "info"])
        self.assertEqual(out["error"]["type"], "ValueError")

    def test_cli_prints_json(self):
        p = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "wavetool.py"),
                            "--db", GOLDEN_DB, "info"],
                           stdout=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertEqual(out["result"]["timescale"], "1ps")


if __name__ == "__main__":
    unittest.main(verbosity=2)
