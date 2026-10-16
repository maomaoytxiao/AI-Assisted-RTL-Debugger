#!/usr/bin/env python3
"""Fault-injection campaign for the sync FIFO (milestone 3).

    python3 scripts/fault_campaign.py run      # make fault-test
    python3 scripts/fault_campaign.py bundle   # make debug-data

`run` builds one RTL variant per fault in faults/faults.json (plus the golden
RTL as a control), simulates each with the unchanged self-checking testbench,
saves the log, VCD and SQLite database under build/experiments/<name>/, and
checks from the waveform database - independently of the testbench - that
the fault was actually activated. It fails unless every fault is activated
and detected and the golden control passes with no activation.

`bundle` copies only what a debugging agent may see (buggy RTL, simulation
log, waveform database, neutral README) into build/debug/<experiment>/.
No ground-truth information is written there.

This script reads the ground truth; the debugging agent must not read it.
"""

import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import vcd_to_sqlite                       # noqa: E402
from waveform_query import WaveformDB      # noqa: E402

FAULTS_JSON = os.path.join(ROOT, "faults", "faults.json")
GOLDEN_RTL = os.path.join(ROOT, "rtl", "sync_fifo.sv")
TB = os.path.join(ROOT, "tb", "tb_sync_fifo.sv")
EXP_DIR = os.path.join(ROOT, "build", "experiments")
DEBUG_DIR = os.path.join(ROOT, "build", "debug")
DUT = "tb_sync_fifo.dut."
ERROR_RE = re.compile(r"ERROR @(\d+) \[([^\]]*)\] (\S+): got 0x(\S+) expected 0x(\S+)")


def load_faults():
    with open(FAULTS_JSON) as f:
        return json.load(f)["faults"]


def make_variant(fault):
    with open(GOLDEN_RTL) as f:
        golden = f.read()
    find, repl = fault["patch"]["find"], fault["patch"]["replace"]
    if golden.count(find) != 1:
        raise SystemExit("patch for %s does not match golden RTL exactly once"
                         % fault["experiment"])
    if find.count("\n") != repl.count("\n"):
        raise SystemExit("patch for %s changes the line count" % fault["experiment"])
    return golden.replace(find, repl)


def simulate(name, rtl_text):
    d = os.path.join(EXP_DIR, name)
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(os.path.join(d, "rtl"))
    rtl = os.path.join(d, "rtl", "sync_fifo.sv")
    with open(rtl, "w") as f:
        f.write(rtl_text)
    vcd, vvp, log = (os.path.join(d, n) for n in ("waves.vcd", "sim.vvp", "sim.log"))
    compile_cmd = ["iverilog", "-g2012", "-Wall", "-s", "tb_sync_fifo",
                   '-DVCD_FILE="%s"' % vcd, "-o", vvp, rtl, TB]
    run_cmd = ["vvp", "-n", vvp]
    c = subprocess.run(compile_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True)
    if c.returncode != 0:
        raise SystemExit("compile failed for %s:\n%s" % (name, c.stdout))
    r = subprocess.run(run_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, timeout=120)
    with open(log, "w") as f:
        f.write(r.stdout)
    db = os.path.join(d, "waves.db")
    vcd_to_sqlite.convert(vcd, db, quiet=True)
    return {"dir": d, "rtl": rtl, "log": log, "vcd": vcd, "db": db,
            "commands": [" ".join(compile_cmd), " ".join(run_cmd),
                         "python3 scripts/vcd_to_sqlite.py %s %s" % (vcd, db)]}


def parse_log(log_path):
    with open(log_path) as f:
        text = f.read()
    errors = ERROR_RE.findall(text)
    first = None
    if errors:
        t, test, what, got, exp = errors[0]
        first = {"time": int(t), "test": test, "check": what,
                 "got": got, "expected": exp}
    failed_tests, current = [], None
    for line in text.splitlines():
        m = re.search(r"TEST \d+: (\S+)", line)
        if m:
            current = m.group(1)
        elif "-> FAIL" in line and current:
            failed_tests.append(current)
    return {"passed": "\nTEST PASSED" in text,
            "failed": "\nTEST FAILED" in text,
            "num_errors": len(errors),
            "first_error": first,
            "failed_tests": failed_tests}


# --------------------------------------------------------------- activation
def rising_edges(db):
    h = db.get_signal_history("tb_sync_fifo.clk", 0, int(db.info["end_time"]),
                              max_events=10 ** 7)
    return [c["time"] for c in h["changes"] if c["value"] == "1"]


def cycle_of(edges, t):
    """Number of rising clock edges at or before time t."""
    import bisect
    return bisect.bisect_right(edges, t)


def check_activation(db, spec):
    v = lambda sig, t: db.get_value_at_time(DUT + sig, t)["value"]
    hits = []
    if spec["kind"] == "ptr_advances_while":
        for t in rising_edges(db):
            if t == 0:
                continue
            if (v("rst_n", t - 1) == "1" and v(spec["request"], t - 1) == "1"
                    and v(spec["flag"], t - 1) == "1"
                    and v(spec["ptr"], t - 1) != v(spec["ptr"], t)):
                hits.append(t)
    elif spec["kind"] == "flag_mismatch":
        end = int(db.info["end_time"])
        times = sorted({c["time"] for s in ("count", spec["flag"]) for c in
                        db.get_signal_history(DUT + s, 0, end)["changes"]})
        for t in times:
            cnt, flag = db.get_value_at_time(DUT + "count", t), db.get_value_at_time(DUT + spec["flag"], t)
            if cnt["known"] and flag["known"] and \
                    flag["int"] != int(cnt["int"] == spec["count_value"]):
                hits.append(t)
    else:
        raise ValueError("unknown activation kind %r" % spec["kind"])
    return hits


# ---------------------------------------------------------------- commands
def cmd_run():
    faults = load_faults()
    with open(GOLDEN_RTL) as f:
        golden_text = f.read()
    rows = []
    # golden control: must pass and show no activation of any fault condition
    sim = simulate("golden", golden_text)
    log = parse_log(sim["log"])
    with WaveformDB(sim["db"]) as db:
        ctrl_hits = {fl["experiment"]: len(check_activation(db, fl["activation"]))
                     for fl in faults}
    rows.append({"name": "golden", "fault_id": None, "tb_passed": log["passed"],
                 "tb_errors": log["num_errors"], "activations": ctrl_hits,
                 "commands": sim["commands"]})
    ok = log["passed"] and not any(ctrl_hits.values())

    for fl in faults:
        sim = simulate(fl["experiment"], make_variant(fl))
        log = parse_log(sim["log"])
        with WaveformDB(sim["db"]) as db:
            hits = check_activation(db, fl["activation"])
            edges = rising_edges(db)
            info = dict(db.info)
        first = log["first_error"]
        row = {"name": fl["experiment"], "fault_id": fl["fault_id"],
               "activated": bool(hits), "num_activations": len(hits),
               "first_activation_time": hits[0] if hits else None,
               "first_activation_cycle": cycle_of(edges, hits[0]) if hits else None,
               "detected": log["failed"] and log["num_errors"] > 0,
               "tb_errors": log["num_errors"], "failed_tests": log["failed_tests"],
               "first_error": first,
               "first_error_cycle": cycle_of(edges, first["time"]) if first else None,
               "db_signals": int(info["num_signals"]),
               "db_value_changes": int(info["num_value_changes"]),
               "commands": sim["commands"]}
        if hits and first and hits[0] > first["time"]:
            row["note"] = "first TB error precedes first activation"
        with open(os.path.join(sim["dir"], "result.json"), "w") as f:
            json.dump(row, f, indent=2)
        rows.append(row)
        ok = ok and row["activated"] and row["detected"]

    faults_rows = rows[1:]
    summary = {"injected": len(faults_rows),
               "activated": sum(r["activated"] for r in faults_rows),
               "detected": sum(r["detected"] for r in faults_rows),
               "golden_passed": rows[0]["tb_passed"],
               "golden_false_activations": rows[0]["activations"],
               "experiments": rows}
    with open(os.path.join(EXP_DIR, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)

    print("%-7s %-20s %-9s %-6s %-8s %-9s %s" % ("exp", "fault", "activated",
          "acts", "detected", "tb_errors", "first TB error (time ps, cycle, test, check)"))
    g = rows[0]
    print("%-7s %-20s %-9s %-6s %-8s %-9s %s" % ("golden", "(none)", "-",
          sum(g["activations"].values()), "-", g["tb_errors"],
          "TEST PASSED" if g["tb_passed"] else "TEST FAILED"))
    for r in faults_rows:
        fe = r["first_error"]
        print("%-7s %-20s %-9s %-6d %-8s %-9d %s" % (
            r["name"], r["fault_id"], r["activated"], r["num_activations"],
            r["detected"], r["tb_errors"],
            "%d, %d, %s, %s" % (fe["time"], r["first_error_cycle"], fe["test"],
                                fe["check"]) if fe else "-"))
    print("injected=%d activated=%d detected=%d golden_passed=%s golden_false_activations=%d"
          % (summary["injected"], summary["activated"], summary["detected"],
             summary["golden_passed"], sum(g["activations"].values())))
    print("fault-test: %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


BUNDLE_README = """# Debug bundle: {exp}

A FIFO design failed its self-checking testbench. Find the root cause.

Files in this directory:
- rtl/sync_fifo.sv : the design that was simulated (may contain a defect)
- sim.log          : testbench output (ERROR lines give time in ps, test, check)
- waves.db         : SQLite waveform database of the failing simulation

Also available (read-only): tb/tb_sync_fifo.sv (testbench),
docs/fifo_spec.md (intended FIFO behaviour).

Waveform queries (JSON output):
  python3 scripts/wavetool.py --exp {exp} find <pattern>
  python3 scripts/wavetool.py --exp {exp} value <signal> <time>
  python3 scripts/wavetool.py --exp {exp} history <signal> <start> <end>
  python3 scripts/wavetool.py --exp {exp} info

Time base: 1 tick = 1 ps; the clock period is 10 ns; rising edges are at
5 ns + n*10 ns. "Cycle" of a time t = number of rising clock edges at or
before t.
"""


def cmd_bundle():
    faults = load_faults()
    if os.path.isdir(DEBUG_DIR):
        shutil.rmtree(DEBUG_DIR)
    for fl in faults:
        exp = fl["experiment"]
        src = os.path.join(EXP_DIR, exp)
        if not os.path.isfile(os.path.join(src, "waves.db")):
            raise SystemExit("missing %s - run 'make fault-test' first" % src)
        dst = os.path.join(DEBUG_DIR, exp)
        os.makedirs(os.path.join(dst, "rtl"))
        shutil.copy(os.path.join(src, "rtl", "sync_fifo.sv"), os.path.join(dst, "rtl"))
        shutil.copy(os.path.join(src, "sim.log"), dst)
        shutil.copy(os.path.join(src, "waves.db"), dst)
        with open(os.path.join(dst, "README.md"), "w") as f:
            f.write(BUNDLE_README.format(exp=exp))
        print("debug bundle: %s" % os.path.relpath(dst, ROOT))
    return 0


if __name__ == "__main__":
    cmds = {"run": cmd_run, "bundle": cmd_bundle}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        print("usage: fault_campaign.py {run|bundle}", file=sys.stderr)
        sys.exit(2)
    sys.exit(cmds[sys.argv[1]]())
