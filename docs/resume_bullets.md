# Resume bullets — AI-Assisted RTL Debugger

Two bullets for entry-level ASIC Design Verification roles. Every number
comes from the final regression (`make clean && make test && make db &&
make db-test && make fault-test && make debug-data && make eval`).

- **Verified a parameterized SystemVerilog synchronous FIFO** using a
  self-checking directed testbench with a cycle-accurate reference model.
  8/8 tests covering reset, overflow/underflow, pointer wraparound and
  simultaneous read/write passed with 1,994 checks and 0 errors (Icarus
  Verilog). Built a **Python VCD-to-SQLite waveform database** (64 signals,
  4,787 value-change events) with read-only temporal query tools, validated
  by 41 unit tests against an independent VCD reader.

- **Built a fault-injection and AI-assisted root-cause-analysis flow**
  inspired by the BTTF paper (arXiv:2610.06790). All 3 injected RTL faults
  were activated and detected by the testbench, with a golden control
  showing 0 false activations. Blind Claude Code agents, using only
  waveform-database queries and RTL, localized **3/3 faults to the correct
  RTL line and signal**. A deterministic evaluator reproduced **51/51** of
  their cited waveform evidence entries.

Short variant (one line each):

- Verified a SystemVerilog FIFO with a self-checking testbench (8/8 directed
  tests, 1,994 checks); built a Python VCD→SQLite waveform database and query
  tools (41/41 unit tests).
- Built a fault-injection flow in which blind AI agents diagnosed 3/3 injected
  RTL bugs from waveform-database queries; 51/51 cited evidence entries were
  verified automatically.
