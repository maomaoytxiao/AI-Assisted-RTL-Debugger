# Milestone 2 — VCD → SQLite waveform store and query tools

Reference: *Back to the Future: Rethinking EDA Infrastructure for Agentic
Systems in Chip Design Verification* (BTTF, arXiv:2610.06790). This milestone
is a simplified re-implementation of BTTF's waveform data layer: a simulation
dump is distilled into a normalized SQLite database (`waves.db`) with a
`signal_metadata` / `signal_changes` split, plus a small set of read-only
query functions intended to become LLM tool calls in a later milestone.
No agent is implemented here.

## 1. Architecture

```
 rtl/ + tb/ ──(make test: iverilog/vvp)──► build/sync_fifo.vcd
                                                │
                       (make db: scripts/vcd_to_sqlite.py, streaming parser,
                        batched executemany, indexes built after load)
                                                ▼
                                         build/waves.db
                                                │
                       scripts/waveform_query.py (read-only, parameterized SQL)
                         find_signal / get_value_at_time / get_signal_history
                                                │
                     tests/test_waveform_query.py (make db-test, unittest)
                     cross-checked against an independent VCD reader
```

Only the Python 3.8 standard library is used (`sqlite3` with SQLite 3.31.1,
`unittest`). No packages were installed.

## 2. Inspection of the existing VCD (`build/sync_fifo.vcd`)

| Property | Value |
|---|---|
| Producer | Icarus Verilog 10.3 |
| `$timescale` | `1ps` (one tick = 1 ps; the TB clock period is 10 ns = 10 000 ticks) |
| Size | 65 386 bytes |
| `$var` declarations | 64, under 56 unique VCD identifiers (8 identifiers are aliased) |
| Scope types | `module` (tb_sync_fifo, dut), `begin` (g_mem[0..7]), `task` (TB tasks) |
| Variable types | `wire`, `reg`, `integer` |
| Widths | 1, 4, 8, 32, 128, 256 bits (the 128/256-bit regs are TB string registers) |
| Timestamps (`#t`) | 808, from `#0` to `#2036000` |
| Value-change records | 4 787 (779 scalar, 4 008 vector, 0 real, 0 Z) |

FIFO signals of interest (all under `tb_sync_fifo.dut`): `wr_ptr[3:0]`,
`rd_ptr[3:0]` (reg), `count[3:0]`, `full`, `empty`, `overflow`, `underflow`,
`wr_en`, `rd_en`, `wr_fire`, `rd_fire` (wire), `wr_data[7:0]` (wire),
`rd_data[7:0]` (reg). Memory words appear as
`tb_sync_fifo.g_mem[i].word[7:0]` (TB wires that mirror `dut.mem[i]`,
because Icarus does not dump unpacked arrays).

**How the VCD encodes transitions.** The header declares each variable once:
`$var <type> <width> <id> <name> [<range>] $end`, nested in `$scope`/`$upscope`
blocks that form the hierarchy. The body is a stream of `#<time>` markers,
each followed by only the variables that changed at that time:

* scalar: `<0|1|x|z><id>` (e.g. `1'` = clk rises),
* vector: `b<bits> <id>`, where leading bits may be omitted
  (`b0 &` means count = 0000; omitted bits are 0, or x/z if the leftmost given
  bit is x/z),
* real: `r<number> <id>`.

The `#0` block is wrapped in `$dumpvars … $end` and holds the initial value
of every variable (many are `x` before reset). Several declarations may share
one identifier when they are the same net, e.g. `'` is both
`tb_sync_fifo.clk` and `tb_sync_fifo.dut.clk`; the change is written once.
Nothing is written for cycles in which a signal does not change, which is
why the database stores events, not per-cycle samples.

## 3. Database schema

```sql
CREATE TABLE waveform_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- timescale, timescale_magnitude/unit/exponent, num_signals,
-- num_unique_identifiers, num_value_changes, num_timestamps, start_time,
-- end_time, source_vcd, source_size_bytes, date, version, converter_version

CREATE TABLE signal_metadata (
    signal_id         INTEGER PRIMARY KEY,
    hierarchical_name TEXT NOT NULL UNIQUE,   -- tb_sync_fifo.dut.wr_ptr
    scope             TEXT NOT NULL,          -- tb_sync_fifo.dut
    name              TEXT NOT NULL,          -- wr_ptr
    scope_type        TEXT,                   -- module / begin / task
    width             INTEGER NOT NULL,
    bit_range         TEXT,                   -- [3:0]
    signal_type       TEXT NOT NULL,          -- wire / reg / integer / real
    vcd_identifier    TEXT NOT NULL,
    alias_of          INTEGER REFERENCES signal_metadata(signal_id),
    change_count      INTEGER NOT NULL DEFAULT 0);

CREATE TABLE signal_changes (
    change_id INTEGER PRIMARY KEY,            -- global VCD event order
    signal_id INTEGER NOT NULL REFERENCES signal_metadata(signal_id),
    timestamp INTEGER NOT NULL,               -- VCD ticks (see timescale)
    value     TEXT NOT NULL);                 -- full-width binary, x/z kept

CREATE INDEX idx_changes_signal_time ON signal_changes(signal_id, timestamp);
CREATE INDEX idx_changes_time        ON signal_changes(timestamp);
CREATE INDEX idx_meta_name  ON signal_metadata(name);
CREATE INDEX idx_meta_scope ON signal_metadata(scope);
CREATE INDEX idx_meta_vcd_id ON signal_metadata(vcd_identifier);

CREATE VIEW v_signal_changes AS            -- addressable by any name/alias
SELECT m.hierarchical_name, m.width, c.timestamp, c.value, c.change_id
FROM signal_metadata m
JOIN signal_changes c ON c.signal_id = COALESCE(m.alias_of, m.signal_id);
```

Design decisions:

* **Aliases.** The first declaration of a VCD identifier owns the change
  rows. Later declarations get their own metadata row (so every hierarchical
  name is searchable) with `alias_of` pointing to the owner. Change data is
  never duplicated. This plays the role of BTTF's `vcd_id_to_db_id` map.
* **Values** are stored as full-width lowercase binary strings. Short VCD
  vectors are left-extended per IEEE 1364 (0, or x/z when the leftmost bit
  is x/z). X and Z are preserved bit-for-bit; only their case is normalized
  (`X`→`x`, `Z`→`z`). Real values are stored as their decimal text.
* **Time** is stored as integer ticks of the VCD timescale; the timescale
  is stored in `waveform_info`. Query functions accept either ticks or
  strings with units (`"56ns"`, `"1.5us"`) and reject times that are not a
  whole number of ticks.
* **Event order.** `change_id` is an `INTEGER PRIMARY KEY`, so it keeps the
  original VCD order (stable across `VACUUM`). When a signal changes twice in
  one timestamp, "value at time t" returns the last event.

## 4. Preprocessing workflow (`scripts/vcd_to_sqlite.py`)

1. Stream the VCD line by line and split it into tokens. Memory scales with
   the number of declared signals plus one insert batch, not with how long
   the waveform is.
2. Header: track the `$scope` stack to build dotted hierarchical names.
   Record width, type, range and identifier. Map the identifier to its
   owning `signal_id`. Parse `$timescale` (`1ps` and `1 ps` both work).
3. Body: `#t` sets the current time (it must not go backwards). Every
   scalar, vector and real change becomes one row
   `(owner_signal_id, t, normalized_value)`. Rows are written with
   `executemany` in batches of 10 000. The `$dumpvars/$dumpall/$dumpon/$dumpoff`
   keywords are skipped, and the values inside those blocks are stored as
   ordinary events.
4. After loading: create the indexes, fill `change_count`, write
   `waveform_info`, and run `ANALYZE`.
5. The build goes to `waves.db.tmp`, which is then renamed with `os.replace`
   so a partly written database is never visible. The journal and fsync are
   turned off during the build because the file is built once and replaced
   as a whole.

Measured output for the FIFO VCD (`make db`):

| Metric | Value |
|---|---|
| Signals (hierarchical names) | **64** (56 unique nets + 8 aliases) |
| Value-change events | **4 787** |
| Distinct timestamps | 808 (0 … 2 036 000 ps) |
| SQLite database size | **380 928 bytes** (VCD: 65 386 bytes) |
| Conversion time | 0.01–0.06 s |

Space breakdown (SQLite `dbstat`): `signal_changes` 209 KB,
`idx_changes_signal_time` 70 KB, `idx_changes_time` 57 KB, everything else
about 45 KB. The database is about 5.8× larger than this tiny VCD, mainly
because of per-row and per-page overhead, the two indexes, and wide testbench
string registers (55 K of the 133 K stored value characters come from
signals wider than 32 bits).

## 5. Example SQL queries (actual output)

```sql
-- value of dut.count at 100 ns (100000 ticks)
SELECT c.timestamp, c.value FROM signal_changes c
JOIN signal_metadata m ON c.signal_id = COALESCE(m.alias_of, m.signal_id)
WHERE m.hierarchical_name = 'tb_sync_fifo.dut.count' AND c.timestamp <= 100000
ORDER BY c.timestamp DESC, c.change_id DESC LIMIT 1;
-- (95000, '0100')

-- transitions of full and overflow between 130 ns and 250 ns
SELECT hierarchical_name, timestamp, value FROM v_signal_changes
WHERE hierarchical_name IN ('tb_sync_fifo.dut.full','tb_sync_fifo.dut.overflow')
  AND timestamp BETWEEN 130000 AND 250000 ORDER BY timestamp, change_id;
-- overflow 135000 '1' | full 135000 '1' | overflow 140000 '0'
-- full 155000 '0'     | full 225000 '0'

-- first time the FIFO became full
SELECT MIN(timestamp) FROM v_signal_changes
WHERE hierarchical_name = 'tb_sync_fifo.dut.full' AND value = '1';
-- 135000

-- busiest nets (candidates for BTTF-style pruning)
SELECT hierarchical_name, change_count FROM signal_metadata
WHERE alias_of IS NULL ORDER BY change_count DESC LIMIT 3;
-- tb_sync_fifo.clk 408 | tb_sync_fifo.checks 401 | tb_sync_fifo.check.exp 401
```

## 6. Example Python queries (actual output)

```python
from waveform_query import find_signal, get_value_at_time, get_signal_history

find_signal("dut.*_ptr")          # -> tb_sync_fifo.dut.rd_ptr, tb_sync_fifo.dut.wr_ptr
                                  #    (width 4, [3:0], reg, change_count 105 / 109)
get_value_at_time("dut.count", "100ns")
# {'signal': 'tb_sync_fifo.dut.count', 'width': 4, 'time': 100000, 'timescale': '1ps',
#  'last_change_time': 95000, 'value': '0100', 'known': True, 'has_x': False,
#  'has_z': False, 'int': 4, 'hex': '4'}
get_value_at_time("g_mem[0].word", 0)
# {..., 'value': 'xxxxxxxx', 'known': False, 'has_x': True, 'int': None,
#  'note': 'unknown (all bits X)'}
get_signal_history("dut.full", "100ns", "250ns")
# num_changes 3: [(135000,'1'), (155000,'0'), (225000,'0')]  + value_before_start
get_value_at_time("rd_data", 0)
# AmbiguousSignalError: ... matches 2 signals: tb_sync_fifo.rd_data, tb_sync_fifo.dut.rd_data
get_value_at_time("wr_pt", 0)
# SignalNotFoundError: signal not found: 'wr_pt'; did you mean: tb_sync_fifo.dut.wr_ptr
```

The same calls are available from the command line, with JSON output:
`python3 scripts/waveform_query.py {find|value|history|info} ...`

Name resolution rules: an exact hierarchical name always wins. Otherwise
the name must match a whole dot-separated suffix (`dut.count`, `wr_ptr`; but
`ptr` does not match `wr_ptr`). A suffix that matches several distinct nets
raises `AmbiguousSignalError` with the candidate list. A suffix that matches
only aliases of one net (`clk` → `tb_sync_fifo.clk`/`tb_sync_fifo.dut.clk`)
resolves safely to the shortest name. `find_signal` treats `[`, `]` and `.`
literally, so `g_mem[3]` works, and supports `*` and `?` wildcards. Every
query is parameterized, and the connection is opened `mode=ro` plus
`PRAGMA query_only`.

## 7. Test results

Workflow, run from `make clean`:

```
make test     -> TEST PASSED (8/8 directed FIFO tests, unchanged from milestone 1)
make db       -> 64 signals, 4787 value changes, 380928 bytes
make db-test  -> Ran 36 tests in 0.475s  OK
```

`tests/test_waveform_query.py` has 26 tests on the FIFO waveform and 10 on
a handwritten fixture (`tests/fixtures/mini.vcd`). The fixture covers Z
values, uppercase X/Z, real values, `$dumpoff/$dumpon`, two changes at the
same timestamp, and a `1 ns` timescale written with a space, none of which
appear in the FIFO dump. Every value assertion is cross-checked against
`ReferenceVCD`, a separate regex-based VCD reader inside the test file that
shares no code with the pipeline:

* metadata (name, width, type, identifier) of all 64 signals matches the VCD;
* event count matches (4 787);
* the full history of every signal matches the VCD exactly;
* `get_value_at_time` matches at every change time, ±1 tick, and the
  midpoint between consecutive changes, for every signal;
* a design-level check run through the API: `full == (count==8)`,
  `empty == (count==0)` and `count == (wr_ptr − rd_ptr) mod 16` at every
  relevant change time once all values are known (more than 100 time points).

Coverage of the requested items: exact/partial lookup (1), value retrieval
(2), multi-bit including 256-bit (3), between two events (4), time-0 values
(5), X/Z (6), empty ranges (7), ambiguity (8), hierarchical resolution (9),
invalid/missing signals and times (10). There are also tests for read-only
access, index use (`EXPLAIN QUERY PLAN`), batch-size independence,
truncation, and timescale conversion.

**Test sensitivity check.** Six bugs were injected one at a time into
scratch copies of the scripts, and each one made the suite fail:
no x/z extension (29 failures), aliases ignored (13 failures, 1 error),
wrong tie order (2), `<` instead of `<=` (10 failures, 1 error), uppercase
X/Z not normalized (4 errors), ambiguity check removed (2).

## 8. Differences from BTTF

| BTTF | This prototype | Reason |
|---|---|---|
| VCD and FSDB, SoC-scale (4.2 GB VCD, 18 450 nets) | VCD only, 64 signals | Icarus emits VCD; FSDB needs proprietary readers |
| Pruning (clock removal, zero-toggle) to shrink the database (Table 1) | Nothing pruned; `change_count` stored so pruning can be added later | Small design; keeping everything makes cross-checking exact |
| `signal_metadata` holds name, type and id | Also stores scope, scope type, width, bit range, alias owner, change count | Needed for safe name resolution and tool output |
| Duplicate VCD ids handled by a `vcd_id_to_db_id` map | Same map at ingest, plus a stored `alias_of` column and the `v_signal_changes` view | Aliases stay searchable without duplicating changes |
| Tools: `find_signal_in_scope`, `get_value_of_signal_at_time`, `get_signal_toggle`, `get_first_time_of_signal_value`, ... | `find_signal`, `get_value_at_time`, `get_signal_history` | Scope of milestone 2; the others are easy to add on this schema |
| Scope-path normalization listed as future work | Strict dotted names, whole-segment suffix resolution, explicit ambiguity errors | Needed so tools fail safely instead of guessing |
| Multi-agent NL→SQL engine | Not implemented | Out of scope (milestone 3) |

The paper does not publish its column-level schema, indexes, value encoding
or time units. The choices above are therefore ours, not a reproduction
of BTTF's.

## 9. Known limitations

* **Same-value events are kept.** Icarus writes some events that repeat
  the previous value (817 of the 4 787 here, e.g. `dut.full` 0→0 at 225 ns).
  They are stored as recorded. A future toggle-count tool must compare
  each value with the previous one.
* **Values are binary text.** Numeric comparisons in SQL (`value > 5`) need
  conversion; the Python tools return `int` and `hex` instead. Very wide
  signals cost one byte per bit.
* **Size.** For this tiny dump the database is about 5.8× the VCD size.
  Pruning testbench scopes, encoding values as hex or BLOB, or dropping
  `idx_changes_time` would shrink it. Nothing has been benchmarked beyond
  this VCD.
* **Rebuilds.** `make db` rebuilds the whole database every time; there is
  no incremental update and no multi-file merge.
* **Unsupported VCD forms.** No FSDB, no extended VCD (`$dumpports`), and only
  timescales of 1/10/100 × {s, ms, us, ns, ps, fs}. Escaped identifiers that
  contain `.` would be split wrongly by suffix resolution.
* **Separate nets for ports.** Icarus gives the TB-side and DUT-side ports
  different identifiers (`tb.rd_data` vs `dut.rd_data`), so they are
  separate nets and a bare `rd_data` is ambiguous by design.
* **Memory arrays.** These are only visible through the TB mirror wires
  `g_mem[i].word`.
* **Single-threaded conversion.** Streaming keeps memory bounded, but this
  has not been tested on multi-GB dumps.
