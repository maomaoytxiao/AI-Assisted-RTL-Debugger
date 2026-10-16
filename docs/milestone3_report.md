# Milestone 3 — AI-assisted FIFO waveform debugging

> **Contains ground truth.** Do not give this file to a debugging agent.

Goal: show that an AI coding agent (Claude Code), given only a failing
design, its simulation log and a SQLite waveform database, can find an
injected RTL bug on its own. It must support the diagnosis with timestamped
waveform evidence, and the result is scored afterwards against the hidden
ground truth.

## 1. Pipeline

```
faults/faults.json (ground truth: patch, accepted lines/signals, activation rule)
        │  make fault-test  (scripts/fault_campaign.py run)          no LLM
        ▼
build/experiments/{golden,exp_a,exp_b,exp_c}/
        rtl/sync_fifo.sv  sim.log  waves.vcd  waves.db  result.json
        │  make debug-data  (scripts/fault_campaign.py bundle)       no LLM
        ▼
build/debug/exp_*/   rtl/sync_fifo.sv  sim.log  waves.db  README.md   (agent-visible)
        │  INTERACTIVE: Claude Code subagent per experiment          LLM step
        │  (docs/m3_agent_prompt.md; tools: scripts/wavetool.py)
        ▼
reports/exp_*.json + reports/exp_*.md     (saved agent output)
        │  make eval  (scripts/evaluate_diagnosis.py)                no LLM
        ▼
reports/evaluation.{json,md}  + "Evaluation" section appended to each exp_*.md
```

## 2. Injected faults (ground truth, `faults/faults.json`)

Each fault is a text patch applied to a copy of the golden `rtl/sync_fifo.sv`,
which stays unmodified. Patches keep the line count, so line numbers match
the golden file. Experiment ids are neutral and shuffled relative to the
task list.

| Exp | Fault id | Injected change | Accepted root-cause lines / signal |
|---|---|---|---|
| exp_c | F1_wr_ptr_on_full | `if (wr_fire)` → `if (wr_en)` for the `wr_ptr` increment (storage write still gated by `wr_fire`) | 67–68 / `wr_ptr` |
| exp_b | F2_rd_ptr_on_empty | `rd_ptr` increment moved from `if (rd_fire)` to `if (rd_en)` | 71–72 / `rd_ptr` |
| exp_a | F3_full_flag | `full = (wr_ptr[AW-1:0] + 1 == rd_ptr[AW-1:0])` (asserts at 7 entries, ignores wrap bit) | 54–55 / `full` |

**Activation** is checked from each mutant's waveform database, independently
of the testbench. For F1 and F2: at a rising edge, the reset is released,
the request is high, the full/empty flag is high, and the pointer still
changes. For F3: `full != (count == 8)` while both values are known.
The same checks run on the golden waveform as a control and must find
nothing.

## 3. Fault campaign results (`make fault-test`, actual output)

```
exp     fault                activated acts   detected tb_errors first TB error (time ps, cycle, test, check)
golden  (none)               -         0      -        0         TEST PASSED
exp_a   F3_full_flag         True      6      True     178       126000, 13, fill, full
exp_b   F2_rd_ptr_on_empty   True      23     True     763       436000, 44, underflow, count
exp_c   F1_wr_ptr_on_full    True      5      True     217       326000, 33, overflow, count
injected=3 activated=3 detected=3 golden_passed=True golden_false_activations=0
fault-test: PASSED
```

| | Count |
|---|---|
| Injected faults | 3 |
| Activated faults (waveform-checked) | 3 |
| Detected by the testbench | 3 |
| Golden control false activations | 0 |

In each case the first activation is one rising edge (1 ns) before the first
testbench error (125/435/325 ns versus 126/436/326 ns). The testbench keeps
running after errors, so every experiment has the full failure window:

- exp_a: 6 failing tests
- exp_b: 4 failing tests
- exp_c: 5 failing tests

Commands that `fault_campaign.py` runs for each experiment (recorded in
`build/experiments/<exp>/result.json`):

```
iverilog -g2012 -Wall -s tb_sync_fifo -DVCD_FILE="build/experiments/<exp>/waves.vcd" \
         -o build/experiments/<exp>/sim.vvp build/experiments/<exp>/rtl/sync_fifo.sv tb/tb_sync_fifo.sv
vvp -n build/experiments/<exp>/sim.vvp
python3 scripts/vcd_to_sqlite.py build/experiments/<exp>/waves.vcd build/experiments/<exp>/waves.db
```

## 4. Agent tool interface (`scripts/wavetool.py`)

A read-only JSON CLI over the milestone-2 functions:

```
python3 scripts/wavetool.py --exp exp_b find   <pattern>
python3 scripts/wavetool.py --exp exp_b value  <signal> <time>
python3 scripts/wavetool.py --exp exp_b history <signal> <start> <end> [--max N]
python3 scripts/wavetool.py --exp exp_b info
```

- **Database selection:** `--exp` selects `build/debug/<exp>/waves.db`, and
  `--db` takes any path.
- **Output:** every call returns
  `{"ok", "tool", "args", "db", "result" | "error"}`. Errors include the
  ambiguity candidates or "did you mean" suggestions.
- **Exit codes:** 0 on success, 2 on a query error.
- **Read-only:** the database is opened with `mode=ro` and `query_only`.

Tested in `tests/test_wavetool.py` (5 tests).

## 5. AI debugging experiment

**Agent.** Three Claude Code subagents (`general-purpose`), started from
this Claude Code session, ran in parallel, one per experiment. Each started
with a fresh context and received only the prompt in
`docs/m3_agent_prompt.md`, with nothing but the experiment id changed. No
fault description, fault class, or expected answer was given. No separate
API key or subscription was used.

**Blindness controls**
1. Neutral, shuffled experiment ids.
2. A per-experiment bundle with no ground truth.
3. An explicit list of off-limits paths: the ground truth, the golden RTL
   and golden waveform, the campaign and evaluation scripts, other bundles
   and reports, and git.
4. A **post-hoc transcript audit.** Every tool call the three agents made was
   extracted from their session transcripts and checked. The agents ran
   26 Bash commands plus 1 Write in total. Every file they accessed was in
   their own bundle, `tb/tb_sync_fifo.sv`, `docs/fifo_spec.md` or
   `scripts/wavetool.py`, apart from writing their own two report files.
   No off-limits path was accessed.

Investigation summary (from the saved reports; every value below was
re-verified by `make eval`):

| Exp | First failure | Agent's key evidence | Agent's root cause |
|---|---|---|---|
| exp_a | 126 000 ps, cycle 13, `fill`: `full` got 1, expected 0 | `wr_ptr` 0001→0111 over 65–125 ns, `rd_ptr`=0000, then at 125 ns `full`=1 and `overflow`=1 with `count`=0111; still full at count 7 after wrap (306 ns: wr 1110, rd 0111) | line 54, `full` off by one, ignores the wrap bit |
| exp_b | 436 000 ps, cycle 44, `underflow`: `count` got f, expected 0 | at 434 ns `empty`=1, `rd_en`=1, `rd_fire`=0, `underflow`=1; at 435 ns `rd_ptr` 0000→0001 while `wr_ptr` holds, so `count`=1111 and `empty`=0 | line 71, `rd_ptr` advanced by `rd_en` instead of `rd_fire` |
| exp_c | 326 000 ps, cycle 33, `overflow`: `count` got 9, expected 8 | full at 321 ns (wr 0000, rd 1000, `wr_fire`=0); at 325 ns `wr_ptr`→0001, so `count`=1001 and `full`=0; then `mem[1]` overwritten 0x0a→0xE1 at 335 ns | line 67, `wr_ptr` advanced by `wr_en` instead of `wr_fire` |

All three agents reported high confidence. Each stated that its diagnosis
is a hypothesis backed by waveform evidence, not a proof, and that no fix
was simulated. Each listed the alternative causes it ruled out, for example
the count and flag formulas, the memory write enable, and the testbench model.

## 6. Evaluation (`make eval`, actual output)

```
| Experiment | Fault | Location | Signal | Diagnosis | First failure | Evidence verified |
| exp_a | F3_full_flag | match | match | CORRECT | match | 16/16 |
| exp_b | F2_rd_ptr_on_empty | match | match | CORRECT | match | 17/17 |
| exp_c | F1_wr_ptr_on_full | match | match | CORRECT | match | 18/18 |
correct=3/3  incorrect=none  first_failure_matches=3  evidence_verified=51/51
eval: OK
```

**How a diagnosis is judged correct.** All three must hold:
- the reported file is the experiment's buggy RTL;
- the reported line is one of the injected-block lines that carry the fault;
- the blamed signal is the faulty one.

**Other checks in `make eval`**
- **Evidence:** every `value` or `history` entry is re-run against the
  experiment's `waves.db` and must match exactly. A single invented value
  fails the run.
- **First failure:** the reported time and cycle must match `sim.log` and the
  clock edges in the database.

**Self-test of the evaluator** (tampered copies of the reports, in the
scratchpad):
- With the line and signal of exp_a changed, it reports exp_a as INCORRECT.
- With one exp_b evidence value replaced, it reports
  `Unverified evidence: value tb_sync_fifo.dut.wr_ptr @ 434000: actual value
  '0000'`.
- With exp_c deleted, it reports `missing report`.
- In that run it exits with status 1.

| | Result |
|---|---|
| Successful AI diagnoses | **3 / 3** |
| Incorrect diagnoses | none |
| Evidence entries reproduced from the database | 51 / 51 |

## 7. Reproducing

```bash
make test && make db && make db-test   # milestones 1-2 (41 Python tests incl. wavetool)
make fault-test                        # simulate golden + 3 mutants, check activation/detection
make debug-data                        # build/debug/exp_{a,b,c}
# --- interactive step (Claude Code session, no API key needed) ---
#   For each exp in exp_a exp_b exp_c: start a fresh Claude Code subagent
#   (or a new `claude` session in this directory) with docs/m3_agent_prompt.md,
#   {EXP} replaced. It writes reports/<exp>.json and reports/<exp>.md.
# ------------------------------------------------------------------
make eval                              # deterministic scoring of the saved reports
```

`make fault-test`, `make debug-data` and `make eval` need no LLM. `make eval`
scores the **saved** reports in `reports/` and never creates or edits a
diagnosis; the only file it changes is the auto-generated Evaluation
section it appends to each `reports/<exp>.md`.

## 8. Limitations

- **The LLM step is not automated by `make`.** Claude Code was the agent,
  started as subagents from an interactive session. A fully scripted loop
  would need either the Claude API (excluded here) or headless runs of the
  `claude` CLI; the latter was not set up or tested.
- **Blindness relies on instructions plus an audit, not a sandbox.** The
  agents could technically read the ground truth. The audit shows they did
  not in this run; repeating the audit is a manual step.
- **The sample is small and the bugs are easy.** Three single-site faults
  in a 100-line design, each found one edge after activation, with a very
  clear testbench log. This shows the workflow works; it does not measure
  accuracy at scale or on bugs spread across several sites.
- **The same model family wrote the faults and diagnosed them.** The
  diagnosing agents had separate, fresh contexts, but there is shared
  "style" knowledge of typical FIFO bugs.
- **Line-based scoring.** A correct diagnosis that cites a nearby line
  outside the accepted lines would be scored as incorrect. The accepted
  signal list is strict (for example, `rd_fire` would not count for F2).
- **Non-deterministic agents.** A rerun of the AI step may produce different
  wording, evidence or line choices. The saved reports are what was
  evaluated.
- **No fix is checked.** No agent's proposed fix was re-simulated.
- **Generated outputs are not tracked.** The `build/` outputs, including the
  databases, are gitignored. `make debug-data` regenerates them
  deterministically from the RTL, the testbench and the patches.
