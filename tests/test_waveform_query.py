"""Tests for the VCD -> SQLite pipeline and the waveform query tools.

Run:  python3 -m unittest discover -s tests -v     (or: make db-test)

Two waveforms are used:
  * build/sync_fifo.vcd + build/waves.db  - the real FIFO simulation
    (produced by 'make test' and 'make db').
  * tests/fixtures/mini.vcd               - a small handwritten VCD covering
    Z values, real signals, $dumpoff and same-timestamp changes that the
    FIFO waveform does not contain. Converted into build/test_mini.db here.

Every value check is cross-checked against ReferenceVCD, a deliberately
separate, regex-based VCD reader in this file that shares no code with
scripts/vcd_to_sqlite.py or scripts/waveform_query.py.
"""

import bisect
import os
import re
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import vcd_to_sqlite                                    # noqa: E402
from waveform_query import (WaveformDB, AmbiguousSignalError,  # noqa: E402
                            SignalNotFoundError, WaveformQueryError,
                            find_signal, get_value_at_time,
                            get_signal_history)

FIFO_VCD = os.path.join(ROOT, "build", "sync_fifo.vcd")
FIFO_DB = os.path.join(ROOT, "build", "waves.db")
MINI_VCD = os.path.join(ROOT, "tests", "fixtures", "mini.vcd")
MINI_DB = os.path.join(ROOT, "build", "test_mini.db")
MINI_DB_B1 = os.path.join(ROOT, "build", "test_mini_batch1.db")


# --------------------------------------------------------------------------
# Independent reference reader (whole-file, regex based; fine for tests)
# --------------------------------------------------------------------------
class ReferenceVCD:
    DECL = re.compile(
        r"\$scope\s+(?P<stype>\S+)\s+(?P<sname>\S+)\s+\$end"
        r"|\$upscope\s+\$end"
        r"|\$var\s+(?P<vtype>\S+)\s+(?P<width>\d+)\s+(?P<ident>\S+)\s+"
        r"(?P<vname>\S+)(?:\s+(?P<range>\[[^\]]*\]))?\s+\$end"
        r"|\$timescale\s+(?P<ts>.*?)\s*\$end", re.S)

    def __init__(self, path):
        with open(path) as f:
            text = f.read()
        header, body = text.split("$enddefinitions", 1)
        body = body.split("$end", 1)[1]
        self.signals = {}            # hier name -> dict(width,type,ident)
        self.timescale = None
        scopes = []
        for m in self.DECL.finditer(header):
            if m.group("sname"):
                scopes.append(m.group("sname"))
            elif m.group("vname"):
                hier = ".".join(scopes + [m.group("vname")])
                self.signals[hier] = {"width": int(m.group("width")),
                                      "type": m.group("vtype"),
                                      "ident": m.group("ident")}
            elif m.group("ts") is not None:
                self.timescale = re.sub(r"\s+", "", m.group("ts"))
            else:
                scopes.pop()
        width_of = {s["ident"]: s["width"] for s in self.signals.values()}
        real_ids = {s["ident"] for s in self.signals.values()
                    if s["type"] == "real"}
        self.changes = {i: [] for i in width_of}   # ident -> [(t, value)]
        self.num_value_changes = 0
        self.end_time = 0
        toks = body.split()
        i, t = 0, None
        while i < len(toks):
            tok = toks[i]
            if tok.startswith("#"):
                t = int(tok[1:])
                self.end_time = t
            elif tok.startswith("$"):
                pass
            elif tok[0] in "bBrR":
                ident, raw = toks[i + 1], tok[1:]
                i += 1
                if ident in real_ids:
                    val = raw
                else:
                    w = width_of[ident]
                    raw = raw.lower()
                    pad = raw[0] if raw[0] in "xz" else "0"
                    val = raw.rjust(w, pad)
                self.changes[ident].append((t, val))
                self.num_value_changes += 1
            else:
                ident = tok[1:]
                self.changes[ident].append((t, tok[0].lower()))
                self.num_value_changes += 1
            i += 1

    def history(self, hier):
        return self.changes[self.signals[hier]["ident"]]

    def value_at(self, hier, t):
        """Latest value at or before t; last event wins on equal times."""
        h = self.history(hier)
        times = [c[0] for c in h]
        k = bisect.bisect_right(times, t)
        return h[k - 1][1] if k else None


def _require_fifo_db():
    if not os.path.isfile(FIFO_VCD):
        raise unittest.SkipTest("missing %s - run 'make test'" % FIFO_VCD)
    if not os.path.isfile(FIFO_DB):
        raise AssertionError("missing %s - run 'make db'" % FIFO_DB)
    if os.path.getmtime(FIFO_DB) < os.path.getmtime(FIFO_VCD):
        raise AssertionError("%s is older than %s - run 'make db'"
                             % (FIFO_DB, FIFO_VCD))


# ==========================================================================
# Real FIFO waveform
# ==========================================================================
class FifoWaveformTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _require_fifo_db()
        cls.ref = ReferenceVCD(FIFO_VCD)
        cls.db = WaveformDB(FIFO_DB)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    # ---- database content vs. VCD --------------------------------------
    def test_metadata_matches_vcd(self):
        rows = self.db.conn.execute(
            "SELECT hierarchical_name, width, signal_type, vcd_identifier"
            " FROM signal_metadata").fetchall()
        got = {r[0]: {"width": r[1], "type": r[2], "ident": r[3]} for r in rows}
        self.assertEqual(got, self.ref.signals)
        self.assertEqual(self.db.timescale, self.ref.timescale)
        self.assertEqual(self.db.timescale, "1ps")

    def test_event_count_matches_vcd(self):
        n = self.db.conn.execute("SELECT COUNT(*) FROM signal_changes").fetchone()[0]
        self.assertEqual(n, self.ref.num_value_changes)
        self.assertEqual(int(self.db.info["num_value_changes"]), n)
        self.assertEqual(int(self.db.info["end_time"]), self.ref.end_time)

    def test_full_history_of_every_signal_matches_vcd(self):
        end = self.ref.end_time
        for hier in self.ref.signals:
            with self.subTest(signal=hier):
                h = self.db.get_signal_history(hier, 0, end, max_events=10**6)
                got = [(c["time"], c["value"]) for c in h["changes"]]
                self.assertEqual(got, self.ref.history(hier))
                self.assertFalse(h["truncated"])

    def test_value_at_time_matches_vcd_around_every_change(self):
        # Probe each change time, 1 tick before/after it, and the midpoint
        # to the next change, for every signal.
        for hier in self.ref.signals:
            hist = self.ref.history(hier)
            probes = set()
            for k, (t, _) in enumerate(hist):
                probes.update({t, t + 1, max(t - 1, 0)})
                if k + 1 < len(hist):
                    probes.add((t + hist[k + 1][0]) // 2)
            probes.add(self.ref.end_time + 1000)
            for t in sorted(probes):
                got = self.db.get_value_at_time(hier, t)["value"]
                self.assertEqual(got, self.ref.value_at(hier, t),
                                 "%s @ %d" % (hier, t))

    # ---- required behaviours -------------------------------------------
    def test_lookup_exact_name(self):
        res = self.db.find_signal("tb_sync_fifo.dut.wr_ptr")
        self.assertEqual(res[0]["hierarchical_name"], "tb_sync_fifo.dut.wr_ptr")
        self.assertEqual(res[0]["width"], 4)
        self.assertEqual(res[0]["bit_range"], "[3:0]")
        self.assertEqual(res[0]["signal_type"], "reg")

    def test_lookup_partial_name(self):
        names = [r["hierarchical_name"] for r in self.db.find_signal("_ptr")]
        self.assertEqual(sorted(names), ["tb_sync_fifo.dut.rd_ptr",
                                         "tb_sync_fifo.dut.wr_ptr"])
        names = [r["hierarchical_name"] for r in self.db.find_signal("dut.*_fire")]
        self.assertEqual(sorted(names), ["tb_sync_fifo.dut.rd_fire",
                                         "tb_sync_fifo.dut.wr_fire"])
        # exact matches are ranked first
        res = self.db.find_signal("count")
        self.assertIn(res[0]["name"], ("count",))

    def test_lookup_brackets_are_literal(self):
        names = [r["hierarchical_name"] for r in self.db.find_signal("g_mem[3]")]
        self.assertEqual(names, ["tb_sync_fifo.g_mem[3].word"])
        self.assertEqual(self.db.find_signal("g_mem[9]"), [])

    def test_lookup_is_injection_safe(self):
        self.assertEqual(self.db.find_signal("x' OR '1'='1"), [])
        self.assertEqual(self.db.find_signal("%"), [])

    def test_fifo_internal_signals_present(self):
        for leaf in ("wr_ptr", "rd_ptr", "count", "full", "empty", "wr_en",
                     "rd_en", "wr_fire", "rd_fire", "rd_data", "wr_data"):
            with self.subTest(leaf=leaf):
                self.db.resolve("tb_sync_fifo.dut." + leaf)

    def test_timestamp_zero_initial_values(self):
        # Values from the $dumpvars block at #0
        self.assertEqual(self.db.get_value_at_time("tb_sync_fifo.dut.count", 0)["value"], "xxxx")
        self.assertEqual(self.db.get_value_at_time("tb_sync_fifo.rst_n", 0)["value"], "0")
        self.assertEqual(self.db.get_value_at_time("tb_sync_fifo.wr_data", 0)["value"], "00000000")
        v = self.db.get_value_at_time("tb_sync_fifo.g_mem[0].word", 0)
        self.assertEqual(v["value"], "xxxxxxxx")
        self.assertFalse(v["known"])
        self.assertEqual(v["last_change_time"], 0)

    def test_x_values_reported_as_unknown(self):
        v = self.db.get_value_at_time("dut.full", 0)
        self.assertEqual(v["value"], "x")
        self.assertTrue(v["has_x"])
        self.assertFalse(v["known"])
        self.assertIsNone(v["int"])
        self.assertIn("unknown", v["note"])

    def test_multibit_values(self):
        # The vector 'b0' in the VCD is stored at full declared width.
        meta = self.db.find_signal("tb_sync_fifo.cur_test")[0]
        self.assertEqual(meta["width"], 256)
        v = self.db.get_value_at_time("tb_sync_fifo.cur_test", 0)
        self.assertEqual(len(v["value"]), 256)
        # The TB sets cur_test to "init" and then "reset" within time 0;
        # $dumpvars records the final value of the 256-bit string reg.
        text = bytes.fromhex(v["hex"]).lstrip(b"\0").decode()
        self.assertEqual(text, "reset")
        # count history: every value is 4 bits; int/hex decode agree
        h = self.db.get_signal_history("dut.count", 0, self.ref.end_time)
        for c in h["changes"]:
            self.assertEqual(len(c["value"]), 4)
            if c["known"]:
                self.assertEqual(c["int"], int(c["value"], 2))
                self.assertEqual(int(c["hex"], 16), c["int"])
        self.assertEqual(max(c["int"] for c in h["changes"] if c["known"]), 8)

    def test_value_between_two_change_events(self):
        hist = self.ref.history("tb_sync_fifo.dut.wr_ptr")
        (t1, v1), (t2, v2) = hist[3], hist[4]
        self.assertLess(t1 + 1, t2)
        for t in (t1, t1 + 1, (t1 + t2) // 2, t2 - 1):
            self.assertEqual(self.db.get_value_at_time("dut.wr_ptr", t)["value"], v1)
        self.assertEqual(self.db.get_value_at_time("dut.wr_ptr", t2)["value"], v2)

    def test_time_units_respect_timescale(self):
        t = self.ref.history("tb_sync_fifo.dut.wr_ptr")[5][0]     # ticks (ps)
        self.assertEqual(t % 1000, 0)
        a = self.db.get_value_at_time("dut.wr_ptr", t)
        b = self.db.get_value_at_time("dut.wr_ptr", "%dns" % (t // 1000))
        c = self.db.get_value_at_time("dut.wr_ptr", "%dps" % t)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(self.db.to_ticks("1.5ns"), 1500)
        with self.assertRaises(ValueError):
            self.db.to_ticks("0.5ps")          # finer than the timescale

    def test_history_order_and_window(self):
        h = self.db.get_signal_history("dut.count", "100ns", "400ns")
        times = [c["time"] for c in h["changes"]]
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(100000 <= t <= 400000 for t in times))
        expected = [(t, v) for t, v in self.ref.history("tb_sync_fifo.dut.count")
                    if 100000 <= t <= 400000]
        self.assertEqual([(c["time"], c["value"]) for c in h["changes"]], expected)
        self.assertEqual(h["value_before_start"]["value"],
                         self.ref.value_at("tb_sync_fifo.dut.count", 99999))

    def test_history_inclusive_bounds(self):
        t = self.ref.history("tb_sync_fifo.dut.wr_ptr")[2][0]
        h = self.db.get_signal_history("dut.wr_ptr", t, t)
        self.assertEqual([c["time"] for c in h["changes"]], [t])

    def test_empty_time_range(self):
        hist = self.ref.history("tb_sync_fifo.dut.wr_ptr")
        (t1, v1), (t2, _) = hist[3], hist[4]
        h = self.db.get_signal_history("dut.wr_ptr", t1 + 1, t2 - 1)
        self.assertEqual(h["changes"], [])
        self.assertEqual(h["num_changes"], 0)
        self.assertEqual(h["value_before_start"]["value"], v1)
        # window entirely after the end of the waveform
        end = self.ref.end_time
        self.assertEqual(self.db.get_signal_history("dut.wr_ptr", end + 1, end + 100)["changes"], [])
        with self.assertRaises(ValueError):
            self.db.get_signal_history("dut.wr_ptr", 500, 100)

    def test_history_truncation(self):
        h = self.db.get_signal_history("tb_sync_fifo.clk", 0, self.ref.end_time,
                                       max_events=10)
        self.assertEqual(h["num_changes"], 10)
        self.assertTrue(h["truncated"])

    def test_ambiguous_name_is_rejected(self):
        with self.assertRaises(AmbiguousSignalError) as cm:
            self.db.get_value_at_time("rd_data", 0)        # tb.rd_data vs dut.rd_data
        self.assertEqual(sorted(cm.exception.candidates),
                         ["tb_sync_fifo.dut.rd_data", "tb_sync_fifo.rd_data"])
        with self.assertRaises(AmbiguousSignalError) as cm:
            self.db.get_signal_history("word", 0, 100)      # 8 memory words
        self.assertEqual(len(cm.exception.candidates), 8)

    def test_alias_names_resolve_to_same_net(self):
        # tb.clk and tb.dut.clk share VCD identifier "'": a bare 'clk' is
        # safe to resolve, and both names return identical histories.
        meta = self.db.find_signal("tb_sync_fifo.dut.clk")[0]
        self.assertEqual(meta["alias_of"], "tb_sync_fifo.clk")
        self.assertEqual(meta["vcd_identifier"],
                         self.db.find_signal("tb_sync_fifo.clk")[0]["vcd_identifier"])
        self.assertEqual(self.db.resolve("clk")["hierarchical_name"], "tb_sync_fifo.clk")
        a = self.db.get_signal_history("tb_sync_fifo.clk", 0, 100000)["changes"]
        b = self.db.get_signal_history("tb_sync_fifo.dut.clk", 0, 100000)["changes"]
        self.assertEqual(a, b)
        self.assertGreater(len(a), 0)
        # aliases are stored once
        n = self.db.conn.execute(
            "SELECT COUNT(*) FROM signal_changes c JOIN signal_metadata m"
            " ON c.signal_id = m.signal_id WHERE m.hierarchical_name ="
            " 'tb_sync_fifo.dut.clk'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_hierarchical_resolution(self):
        r = self.db.resolve
        self.assertEqual(r("dut.count")["hierarchical_name"], "tb_sync_fifo.dut.count")
        self.assertEqual(r("tb_sync_fifo.count")["hierarchical_name"], "tb_sync_fifo.count")
        self.assertEqual(r("dut.rd_data")["hierarchical_name"], "tb_sync_fifo.dut.rd_data")
        self.assertEqual(r("g_mem[3].word")["hierarchical_name"], "tb_sync_fifo.g_mem[3].word")
        self.assertEqual(r("wr_ptr")["hierarchical_name"], "tb_sync_fifo.dut.wr_ptr")
        # a suffix must match whole path segments: 'ptr' is not 'wr_ptr'
        with self.assertRaises(SignalNotFoundError):
            r("ptr")

    def test_invalid_or_missing_signals(self):
        with self.assertRaises(SignalNotFoundError) as cm:
            self.db.get_value_at_time("tb_sync_fifo.dut.no_such_signal", 0)
        with self.assertRaises(SignalNotFoundError) as cm:
            self.db.get_value_at_time("wr_pt", 0)
        self.assertIn("tb_sync_fifo.dut.wr_ptr", cm.exception.suggestions)
        self.assertEqual(self.db.find_signal("does_not_exist"), [])
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                self.db.resolve(bad)
        for bad_t in (-1, "abc", "5 parsecs", 1.5, True):
            with self.assertRaises(ValueError):
                self.db.get_value_at_time("dut.count", bad_t)
        with self.assertRaises(WaveformQueryError):
            WaveformDB(os.path.join(ROOT, "build", "missing.db"))

    def test_fifo_flag_invariants_from_database(self):
        # Design-level sanity check through the query API: after reset,
        # full == (count == 8), empty == (count == 0),
        # count == (wr_ptr - rd_ptr) mod 16 at every relevant change time.
        names = ["dut.count", "dut.full", "dut.empty", "dut.wr_ptr", "dut.rd_ptr"]
        times = sorted({c["time"] for n in names for c in
                        self.db.get_signal_history(n, 0, self.ref.end_time)["changes"]})
        checked = 0
        for t in times:
            v = {n: self.db.get_value_at_time(n, t) for n in names}
            if not all(x["known"] for x in v.values()):
                continue
            cnt = v["dut.count"]["int"]
            self.assertEqual(v["dut.full"]["int"], int(cnt == 8), t)
            self.assertEqual(v["dut.empty"]["int"], int(cnt == 0), t)
            self.assertEqual((v["dut.wr_ptr"]["int"] - v["dut.rd_ptr"]["int"]) % 16, cnt, t)
            checked += 1
        self.assertGreater(checked, 100)

    def test_database_is_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.db.conn.execute("DELETE FROM signal_changes")

    def test_indexes_used(self):
        plan = " ".join(r[3] for r in self.db.conn.execute(
            "EXPLAIN QUERY PLAN SELECT timestamp, value FROM signal_changes"
            " WHERE signal_id = ? AND timestamp <= ?"
            " ORDER BY timestamp DESC, change_id DESC LIMIT 1", (1, 100)))
        self.assertIn("idx_changes_signal_time", plan)

    def test_module_level_functions(self):
        self.assertTrue(find_signal("wr_ptr", db=FIFO_DB))
        v = get_value_at_time("dut.count", "100ns", db=FIFO_DB)
        self.assertEqual(v["value"], self.ref.value_at("tb_sync_fifo.dut.count", 100000))
        h = get_signal_history("dut.full", "0ns", "2036ns", db=FIFO_DB)
        self.assertEqual([(c["time"], c["value"]) for c in h["changes"]],
                         self.ref.history("tb_sync_fifo.dut.full"))


# ==========================================================================
# Handwritten fixture: X/Z, real values, same-timestamp changes, dumpoff
# ==========================================================================
class MiniFixtureTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.makedirs(os.path.dirname(MINI_DB), exist_ok=True)
        vcd_to_sqlite.convert(MINI_VCD, MINI_DB, quiet=True)
        vcd_to_sqlite.convert(MINI_VCD, MINI_DB_B1, batch_size=1, quiet=True)
        cls.ref = ReferenceVCD(MINI_VCD)
        cls.db = WaveformDB(MINI_DB)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_matches_reference(self):
        for hier in self.ref.signals:
            got = [(c["time"], c["value"]) for c in
                   self.db.get_signal_history(hier, 0, 1000)["changes"]]
            self.assertEqual(got, self.ref.history(hier), hier)
            for t in range(0, 35):
                self.assertEqual(self.db.get_value_at_time(hier, t)["value"],
                                 self.ref.value_at(hier, t), "%s @ %d" % (hier, t))

    def test_batch_size_does_not_change_result(self):
        q = ("SELECT m.hierarchical_name, c.timestamp, c.value, c.change_id"
             " FROM signal_changes c JOIN signal_metadata m USING(signal_id)"
             " ORDER BY c.change_id")
        a = sqlite3.connect(MINI_DB).execute(q).fetchall()
        b = sqlite3.connect(MINI_DB_B1).execute(q).fetchall()
        self.assertEqual(a, b)

    def test_timescale_with_space(self):
        self.assertEqual(self.db.timescale, "1ns")
        self.assertEqual(self.db.to_ticks("5ns"), 5)
        self.assertEqual(self.db.to_ticks("5000ps"), 5)
        with self.assertRaises(ValueError):
            self.db.to_ticks("5500ps")

    def test_z_preserved(self):
        v = self.db.get_value_at_time("top.bus", 0)
        self.assertEqual(v["value"], "zzzzzzzz")        # 'bz' extended with z
        self.assertTrue(v["has_z"])
        self.assertIn("high impedance", v["note"])
        self.assertEqual(self.db.get_value_at_time("u1.en", 0)["value"], "z")   # 'Z'
        self.assertEqual(self.db.get_value_at_time("u0.en", 15)["value"], "z")

    def test_mixed_xz_vectors(self):
        v = self.db.get_value_at_time("top.bus", 5)     # 'b10z1' -> 8 bits
        self.assertEqual(v["value"], "000010z1")
        self.assertFalse(v["known"])
        self.assertIn("partially", v["note"])
        self.assertEqual(self.db.get_value_at_time("top.state", 7)["value"], "0x10")
        self.assertEqual(self.db.get_value_at_time("top.state", 0)["value"], "xxxx")
        self.assertEqual(self.db.get_value_at_time("u0.bus", 0)["value"], "00000001")

    def test_same_timestamp_last_change_wins(self):
        v = self.db.get_value_at_time("top.state", 10)
        self.assertEqual(v["value"], "0011")
        self.assertEqual(v["int"], 3)
        h = self.db.get_signal_history("top.state", 10, 10)
        self.assertEqual([c["value"] for c in h["changes"]], ["0000", "0011"])

    def test_dumpoff_values(self):
        self.assertEqual(self.db.get_value_at_time("top.bus", 22)["value"], "xxxxxxxx")
        self.assertEqual(self.db.get_value_at_time("top.bus", 25)["value"], "11110000")

    def test_real_values(self):
        v = self.db.get_value_at_time("top.temp", 15)
        self.assertEqual(v["value"], "-1.25")
        self.assertEqual(v["real"], -1.25)

    def test_ambiguity_and_aliases(self):
        with self.assertRaises(AmbiguousSignalError):
            self.db.resolve("en")          # top.u0.en and top.u1.en
        with self.assertRaises(AmbiguousSignalError):
            self.db.resolve("bus")         # top.bus and top.u0.bus
        self.assertEqual(self.db.resolve("clk")["hierarchical_name"], "top.clk")
        self.assertEqual(self.db.resolve("u0.bus")["hierarchical_name"], "top.u0.bus")

    def test_value_before_first_change(self):
        # Every signal is dumped at #0, so no query time precedes the first
        # change; the 'no value' path is reached via value_before_start of
        # a window starting at 0.
        h = self.db.get_signal_history("top.clk", 0, 3)
        self.assertIsNone(h["value_before_start"]["value"])
        self.assertFalse(h["value_before_start"]["known"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
