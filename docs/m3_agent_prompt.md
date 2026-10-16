# Milestone 3 — blind debugging prompt (template)

This is the prompt given to each Claude Code debugging subagent, with
`{EXP}` replaced by `exp_a`, `exp_b` or `exp_c`, and `{REPO}` replaced by the
absolute path of your local clone. (In the recorded run, `{REPO}` was the
author's local project directory; nothing else differed.) It contains no
information about the injected faults. See docs/milestone3_report.md for the
workflow.

---

You are a hardware design-verification engineer debugging a failing simulation. Work in {REPO}. Your experiment is **{EXP}**.

A synchronous FIFO design failed its self-checking testbench. Your job: find the root cause using the waveform database and the RTL, and write an evidence-based report. Read build/debug/{EXP}/README.md first.

## Allowed inputs (read ONLY these)
- build/debug/{EXP}/ (rtl/sync_fifo.sv, sim.log, waves.db, README.md)
- tb/tb_sync_fifo.sv (testbench) and docs/fifo_spec.md (intended behaviour)
- scripts/wavetool.py and scripts/waveform_query.py (you may read and run them)

## Strictly off-limits (do not open, list, grep, diff or query)
Anything else in the repo, in particular: faults/, rtl/ (top-level), build/experiments/, build/waves.db, build/sync_fifo.vcd, other build/debug/* directories, reports/ files of other experiments, scripts other than the two above, docs other than fifo_spec.md, and git history/diffs. Do not re-run the simulator, do not modify RTL or testbench, do not write to the database. This is a blind investigation: you must diagnose from the waveform and the RTL in your bundle only.

## Method
1. From sim.log identify the first functional failure (time, test, check, observed vs expected). Compute its cycle (number of rising clk edges at or before that time).
2. Use `python3 scripts/wavetool.py --exp {EXP} {find|value|history|info} ...` to locate relevant FIFO signals (pointers, count, full/empty, enables, fire/accept signals, data) and query values before, at, and after the failure. Trace back to the earliest point where DUT behaviour diverges from the spec.
3. Read the RTL and identify the line(s) whose logic explains the waveform. Form a root-cause hypothesis, and consider alternatives.
4. Only cite evidence you actually obtained from wavetool output. Copy values exactly as returned (binary strings in "value"). Never invent or estimate values.

## Deliverables (write exactly these two files)
1. reports/{EXP}.json with this structure:
```json
{
  "experiment": "{EXP}",
  "diagnosed_by": "Claude Code subagent",
  "first_failure": {"time": 0, "cycle": 0, "test": "", "check": "", "observed": "", "expected": ""},
  "root_cause": {"file": "build/debug/{EXP}/rtl/sync_fifo.sv", "line": 0, "signal": "<name of the RTL signal whose logic is wrong>", "hypothesis": "<one paragraph>"},
  "evidence": [
    {"tool": "value", "signal": "<full hierarchical name>", "time": 0, "observed_value": "<exact value string>", "interpretation": ""},
    {"tool": "history", "signal": "<full hierarchical name>", "start": 0, "end": 0, "observed_changes": [[0, "<value>"]], "interpretation": ""}
  ],
  "alternative_hypotheses_considered": [""],
  "confidence": "high|medium|low",
  "limitations": ""
}
```
Times are integer ticks (ps). For "history" evidence, observed_changes must list ALL changes the tool returned in that window (keep windows small). Include at least 6 evidence entries, covering values before and after the failure and the relevant pointer/flag/control signals.
2. reports/{EXP}.md: a human-readable debug report with: failure name, failure timestamp and cycle, relevant signal values (table), the waveform query commands you ran with their key outputs, suspected RTL root cause with file:line and the offending code, confidence and limitations. State clearly that the diagnosis is a hypothesis supported by waveform evidence, not a formal proof. Do not include any section on whether it matches a known fault.

In your final message, reply with: the root-cause line and signal, confidence, the list of files you read, and confirm you did not access any off-limits path.
