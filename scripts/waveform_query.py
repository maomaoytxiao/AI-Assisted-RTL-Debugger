#!/usr/bin/env python3
"""Read-only waveform query tools over the SQLite store from vcd_to_sqlite.py.

Python API (all results are JSON-serializable dicts/lists, so they can be
exposed as LLM tool calls later):

    find_signal(pattern)
    get_value_at_time(signal_name, timestamp)
    get_signal_history(signal_name, start_time, end_time)

Each function takes an optional `db` argument (path or WaveformDB); the
default is $WAVES_DB or build/waves.db in the project root.

Times may be given as integers (VCD ticks, see waveform_info.timescale) or
as strings with a unit, e.g. "56ns", "1.5 us", "56000ps".

CLI:
    python3 scripts/waveform_query.py find wr_ptr
    python3 scripts/waveform_query.py value dut.count 100ns
    python3 scripts/waveform_query.py history dut.count 50ns 150ns
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from decimal import Decimal, InvalidOperation

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.environ.get("WAVES_DB",
                            os.path.join(PROJECT_ROOT, "build", "waves.db"))

TIME_UNITS = {"s": 0, "ms": -3, "us": -6, "ns": -9, "ps": -12, "fs": -15}
DEFAULT_MAX_EVENTS = 10000


class WaveformQueryError(Exception):
    """Base class for query errors (safe to show to a user / LLM)."""


class SignalNotFoundError(WaveformQueryError):
    def __init__(self, name, suggestions):
        self.name = name
        self.suggestions = suggestions
        msg = "signal not found: %r" % name
        if suggestions:
            msg += "; did you mean: " + ", ".join(suggestions)
        super().__init__(msg)


class AmbiguousSignalError(WaveformQueryError):
    def __init__(self, name, candidates):
        self.name = name
        self.candidates = candidates
        super().__init__("ambiguous signal name %r matches %d signals: %s"
                         % (name, len(candidates), ", ".join(candidates)))


def _glob_escape(text):
    """Escape SQLite GLOB metacharacters so they match literally."""
    return re.sub(r"([\[\]*?])", lambda m: "[" + m.group(1) + "]", text)


def describe_value(value, width, signal_type):
    """Decode a stored value string into a structured description."""
    if value is None:
        return {"value": None, "known": False, "has_x": False, "has_z": False,
                "int": None, "hex": None,
                "note": "no value recorded at or before this time"}
    if signal_type in ("real", "realtime"):
        return {"value": value, "known": True, "has_x": False, "has_z": False,
                "int": None, "hex": None, "real": float(value)}
    has_x = "x" in value
    has_z = "z" in value
    known = not (has_x or has_z)
    as_int = int(value, 2) if known and value else None
    out = {"value": value, "known": known, "has_x": has_x, "has_z": has_z,
           "int": as_int,
           "hex": ("%0*x" % ((width + 3) // 4, as_int)) if as_int is not None else None}
    if not known:
        if set(value) == {"x"}:
            out["note"] = "unknown (all bits X)"
        elif set(value) == {"z"}:
            out["note"] = "high impedance (all bits Z)"
        else:
            out["note"] = "partially unknown: contains X/Z bits"
    return out


class WaveformDB:
    """Read-only connection to a waves.db file."""

    def __init__(self, path=DEFAULT_DB):
        if not os.path.isfile(path):
            raise WaveformQueryError("waveform database not found: %s "
                                     "(run 'make db')" % path)
        self.path = path
        uri = "file:%s?mode=ro" % os.path.abspath(path)
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON")
        self.info = {r["key"]: r["value"] for r in
                     self.conn.execute("SELECT key, value FROM waveform_info")}
        self.tick_exponent = int(self.info["timescale_exponent"])
        self.tick_magnitude = int(self.info["timescale_magnitude"])
        self.timescale = self.info["timescale"]

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ time
    def to_ticks(self, t):
        """Convert an int (ticks) or a '<number><unit>' string to ticks."""
        if isinstance(t, bool):
            raise ValueError("invalid time: %r" % (t,))
        if isinstance(t, int):
            ticks = t
        elif isinstance(t, str):
            m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*", t)
            if not m:
                raise ValueError("invalid time: %r" % t)
            num, unit = m.group(1), m.group(2).lower()
            if not unit:
                if "." in num:
                    raise ValueError("tick counts must be integers: %r" % t)
                ticks = int(num)
            else:
                if unit not in TIME_UNITS:
                    raise ValueError("unknown time unit %r in %r" % (unit, t))
                try:
                    exact = (Decimal(num) * Decimal(10) ** TIME_UNITS[unit]
                             / (self.tick_magnitude
                                * Decimal(10) ** self.tick_exponent))
                except InvalidOperation:
                    raise ValueError("invalid time: %r" % t)
                if exact != exact.to_integral_value():
                    raise ValueError("%r is not a whole number of %s ticks"
                                     % (t, self.timescale))
                ticks = int(exact)
        else:
            raise ValueError("invalid time: %r" % (t,))
        if ticks < 0:
            raise ValueError("time must be non-negative: %r" % (t,))
        return ticks

    # ------------------------------------------------------- signals
    def _meta(self, row):
        return {"hierarchical_name": row["hierarchical_name"],
                "scope": row["scope"],
                "name": row["name"],
                "width": row["width"],
                "bit_range": row["bit_range"],
                "signal_type": row["signal_type"],
                "scope_type": row["scope_type"],
                "vcd_identifier": row["vcd_identifier"],
                "change_count": row["change_count"],
                "alias_of": (self._name_of(row["alias_of"])
                             if row["alias_of"] is not None else None)}

    def _name_of(self, signal_id):
        r = self.conn.execute("SELECT hierarchical_name FROM signal_metadata"
                              " WHERE signal_id = ?", (signal_id,)).fetchone()
        return r[0] if r else None

    def find_signal(self, pattern, limit=200):
        """Search hierarchical names.

        The pattern is a case-sensitive substring match anywhere in the
        hierarchical name; '*' (any run) and '?' (one char) act as wildcards
        inside it. All other characters (including '[', ']', '.') are
        literal. Exact matches are listed first.
        """
        if not isinstance(pattern, str) or not pattern.strip():
            raise ValueError("pattern must be a non-empty string")
        pattern = pattern.strip()
        parts = re.split(r"([*?])", pattern)
        glob = "*" + "".join(p if p in ("*", "?") else _glob_escape(p)
                             for p in parts) + "*"
        rows = self.conn.execute(
            "SELECT * FROM signal_metadata WHERE hierarchical_name GLOB ?"
            " ORDER BY (hierarchical_name = ?) DESC, (name = ?) DESC,"
            " hierarchical_name LIMIT ?",
            (glob, pattern, pattern, int(limit))).fetchall()
        return [self._meta(r) for r in rows]

    def resolve(self, signal_name):
        """Resolve a name to exactly one signal row.

        Accepts a full hierarchical name or any dot-separated suffix of one
        (e.g. 'dut.wr_ptr', 'wr_ptr'). Raises AmbiguousSignalError when the
        suffix matches several distinct nets, SignalNotFoundError when it
        matches none. Several matches that are aliases of the same VCD net
        (e.g. tb.clk and tb.dut.clk) resolve to the shortest name.
        """
        if not isinstance(signal_name, str) or not signal_name.strip():
            raise ValueError("signal name must be a non-empty string")
        signal_name = signal_name.strip()
        row = self.conn.execute(
            "SELECT * FROM signal_metadata WHERE hierarchical_name = ?",
            (signal_name,)).fetchone()
        if row:
            return row
        rows = self.conn.execute(
            "SELECT * FROM signal_metadata WHERE hierarchical_name GLOB ?"
            " ORDER BY length(hierarchical_name), hierarchical_name",
            ("*." + _glob_escape(signal_name),)).fetchall()
        if not rows:
            sugg = [m["hierarchical_name"] for m in
                    self.find_signal(signal_name.split(".")[-1], limit=5)]
            raise SignalNotFoundError(signal_name, sugg)
        nets = {r["alias_of"] if r["alias_of"] is not None else r["signal_id"]
                for r in rows}
        if len(nets) > 1:
            raise AmbiguousSignalError(signal_name,
                                       [r["hierarchical_name"] for r in rows])
        return rows[0]

    @staticmethod
    def _owner(row):
        return row["alias_of"] if row["alias_of"] is not None else row["signal_id"]

    # ---------------------------------------------------------- values
    def get_value_at_time(self, signal_name, timestamp):
        row = self.resolve(signal_name)
        t = self.to_ticks(timestamp)
        ch = self.conn.execute(
            "SELECT timestamp, value FROM signal_changes"
            " WHERE signal_id = ? AND timestamp <= ?"
            " ORDER BY timestamp DESC, change_id DESC LIMIT 1",
            (self._owner(row), t)).fetchone()
        result = {"signal": row["hierarchical_name"],
                  "width": row["width"],
                  "time": t,
                  "timescale": self.timescale,
                  "last_change_time": ch["timestamp"] if ch else None}
        result.update(describe_value(ch["value"] if ch else None,
                                     row["width"], row["signal_type"]))
        end_time = int(self.info["end_time"])
        if t > end_time:
            result["warning"] = ("time is after the end of the waveform (%d);"
                                 " value is the last recorded one" % end_time)
        return result

    def get_signal_history(self, signal_name, start_time, end_time,
                           max_events=DEFAULT_MAX_EVENTS):
        row = self.resolve(signal_name)
        t0 = self.to_ticks(start_time)
        t1 = self.to_ticks(end_time)
        if t0 > t1:
            raise ValueError("start_time (%d) is after end_time (%d)" % (t0, t1))
        owner = self._owner(row)
        rows = self.conn.execute(
            "SELECT timestamp, value FROM signal_changes"
            " WHERE signal_id = ? AND timestamp BETWEEN ? AND ?"
            " ORDER BY timestamp, change_id LIMIT ?",
            (owner, t0, t1, int(max_events) + 1)).fetchall()
        truncated = len(rows) > max_events
        rows = rows[:max_events]
        # Value in effect just before the window, for context.
        prev = self.conn.execute(
            "SELECT timestamp, value FROM signal_changes"
            " WHERE signal_id = ? AND timestamp < ?"
            " ORDER BY timestamp DESC, change_id DESC LIMIT 1",
            (owner, t0)).fetchone()
        width, kind = row["width"], row["signal_type"]
        changes = []
        for r in rows:
            d = describe_value(r["value"], width, kind)
            d["time"] = r["timestamp"]
            changes.append(d)
        before = describe_value(prev["value"] if prev else None, width, kind)
        before["time"] = prev["timestamp"] if prev else None
        return {"signal": row["hierarchical_name"],
                "width": width,
                "start_time": t0,
                "end_time": t1,
                "timescale": self.timescale,
                "value_before_start": before,
                "num_changes": len(changes),
                "truncated": truncated,
                "changes": changes}


# --------------------------------------------------------------------------
# Module-level convenience functions (the tool-call surface)
# --------------------------------------------------------------------------
def _with_db(db, fn):
    if isinstance(db, WaveformDB):
        return fn(db)
    with WaveformDB(db or DEFAULT_DB) as wdb:
        return fn(wdb)


def find_signal(pattern, db=None):
    return _with_db(db, lambda w: w.find_signal(pattern))


def get_value_at_time(signal_name, timestamp, db=None):
    return _with_db(db, lambda w: w.get_value_at_time(signal_name, timestamp))


def get_signal_history(signal_name, start_time, end_time, db=None,
                       max_events=DEFAULT_MAX_EVENTS):
    return _with_db(db, lambda w: w.get_signal_history(
        signal_name, start_time, end_time, max_events))


def _time_arg(s):
    return int(s) if s.isdigit() else s


def main(argv=None):
    ap = argparse.ArgumentParser(description="Query a waves.db file")
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("find");    p.add_argument("pattern")
    p = sub.add_parser("value");   p.add_argument("signal"); p.add_argument("time")
    p = sub.add_parser("history"); p.add_argument("signal")
    p.add_argument("start"); p.add_argument("end")
    sub.add_parser("info")
    args = ap.parse_args(argv)
    try:
        with WaveformDB(args.db) as w:
            if args.cmd == "find":
                out = w.find_signal(args.pattern)
            elif args.cmd == "value":
                out = w.get_value_at_time(args.signal, _time_arg(args.time))
            elif args.cmd == "history":
                out = w.get_signal_history(args.signal, _time_arg(args.start),
                                           _time_arg(args.end))
            else:
                out = w.info
    except (WaveformQueryError, ValueError) as e:
        print(json.dumps({"error": type(e).__name__, "message": str(e)}, indent=2))
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
