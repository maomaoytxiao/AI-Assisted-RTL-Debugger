#!/usr/bin/env python3
"""Read-only waveform query CLI for AI agents (JSON in, JSON out).

Thin wrapper over scripts/waveform_query.py exposing three tools:

  python3 scripts/wavetool.py --exp exp_a find <pattern>
  python3 scripts/wavetool.py --exp exp_a value <signal> <time>
  python3 scripts/wavetool.py --exp exp_a history <signal> <start> <end> [--max N]
  python3 scripts/wavetool.py --exp exp_a info

--exp NAME selects build/debug/NAME/waves.db; --db PATH selects any database
(default: build/waves.db, the golden milestone-2 waveform).
Times are ticks (integers, see "timescale") or strings such as "135ns".

Every call prints one JSON object:
  {"ok": true,  "tool": ..., "args": {...}, "db": ..., "result": ...}
  {"ok": false, "tool": ..., "args": {...}, "db": ..., "error": {"type":..., "message":...}}
Exit status: 0 on success, 2 on a query error.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from waveform_query import (WaveformDB, WaveformQueryError,  # noqa: E402
                            AmbiguousSignalError, SignalNotFoundError,
                            DEFAULT_DB, PROJECT_ROOT)

DEBUG_DIR = os.path.join(PROJECT_ROOT, "build", "debug")


def _time(s):
    return int(s) if s.strip().isdigit() else s


def db_path_for(exp=None, db=None):
    if exp and db:
        raise ValueError("use either --exp or --db, not both")
    if exp:
        if not all(ch.isalnum() or ch in "_-" for ch in exp):
            raise ValueError("invalid experiment name: %r" % exp)
        return os.path.join(DEBUG_DIR, exp, "waves.db")
    return db or DEFAULT_DB


def run(argv):
    ap = argparse.ArgumentParser(description="Read-only waveform query tools")
    ap.add_argument("--exp", help="experiment name (build/debug/<exp>/waves.db)")
    ap.add_argument("--db", help="explicit database path")
    sub = ap.add_subparsers(dest="tool", required=True)
    p = sub.add_parser("find", help="find_signal(pattern)")
    p.add_argument("pattern")
    p = sub.add_parser("value", help="get_value_at_time(signal, time)")
    p.add_argument("signal"); p.add_argument("time")
    p = sub.add_parser("history", help="get_signal_history(signal, start, end)")
    p.add_argument("signal"); p.add_argument("start"); p.add_argument("end")
    p.add_argument("--max", type=int, default=500, help="max events returned")
    sub.add_parser("info", help="waveform metadata (timescale, end time, ...)")
    a = ap.parse_args(argv)

    args = {k: v for k, v in vars(a).items() if k not in ("tool", "exp", "db")}
    out = {"ok": False, "tool": a.tool, "args": args, "db": None}
    try:
        path = db_path_for(a.exp, a.db)
        out["db"] = os.path.relpath(path, PROJECT_ROOT)
        with WaveformDB(path) as w:
            if a.tool == "find":
                res = w.find_signal(a.pattern)
            elif a.tool == "value":
                res = w.get_value_at_time(a.signal, _time(a.time))
            elif a.tool == "history":
                res = w.get_signal_history(a.signal, _time(a.start), _time(a.end),
                                           max_events=a.max)
            else:
                res = w.info
        out.update(ok=True, result=res)
        code = 0
    except (WaveformQueryError, ValueError) as e:
        err = {"type": type(e).__name__, "message": str(e)}
        if isinstance(e, AmbiguousSignalError):
            err["candidates"] = e.candidates
        if isinstance(e, SignalNotFoundError):
            err["suggestions"] = e.suggestions
        out["error"] = err
        code = 2
    return out, code


def main(argv=None):
    out, code = run(sys.argv[1:] if argv is None else argv)
    print(json.dumps(out, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
