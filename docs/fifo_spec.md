# sync_fifo specification (milestone 1)

## Parameters
| Name  | Default | Notes |
|-------|---------|-------|
| WIDTH | 8       | data width |
| DEPTH | 8       | number of entries; must be a power of 2, >= 2 |

## Ports
| Port        | Dir | Width | Description |
|-------------|-----|-------|-------------|
| clk         | in  | 1     | rising-edge clock |
| rst_n       | in  | 1     | active-low reset, asynchronous assert, release synchronous to clk |
| wr_en       | in  | 1     | write request |
| wr_data     | in  | WIDTH | write data |
| full        | out | 1     | FIFO holds DEPTH entries |
| overflow    | out | 1     | combinational: `wr_en && full` (write is being dropped this cycle) |
| rd_en       | in  | 1     | read request |
| rd_data     | out | WIDTH | registered read data |
| empty       | out | 1     | FIFO holds 0 entries |
| underflow   | out | 1     | combinational: `rd_en && empty` (read is being dropped this cycle) |
| count       | out | log2(DEPTH)+1 | number of stored entries, 0..DEPTH |

## Behavior
- **Reset:** while `rst_n=0`, pointers and `rd_data` are cleared immediately:
  `empty=1`, `full=0`, `count=0`, `rd_data=0`. Stored contents are discarded
  (the storage array itself is not reset, but is unreachable).
- **Write:** accepted on a rising edge when `wr_en && !full`. Writes while full
  are ignored and do not modify storage.
- **Read:** accepted on a rising edge when `rd_en && !empty`.
- **Read latency:** 1 cycle. The oldest entry appears on `rd_data` immediately
  after the rising edge that accepts the read. `rd_data` holds its value
  when no read is accepted (including dropped reads on empty).
- **Flags:** `full`, `empty` and `count` are derived from the registered
  pointers, so they always describe the state *before* the next edge.
- **Simultaneous read and write** (both requested in the same cycle):

  | State before edge | Write | Read | count after |
  |-------------------|-------|------|-------------|
  | empty             | accepted | dropped (no fall-through) | 1 |
  | 0 < count < DEPTH | accepted | accepted | unchanged |
  | full              | dropped  | accepted | DEPTH-1 |

## Verification (tb/tb_sync_fifo.sv)
Self-checking directed testbench with a cycle-accurate reference model.
Every cycle checks `full/empty/count/overflow/underflow` before the edge and
`rd_data/count/full/empty` after it. Tests: reset, fill, drain, overflow,
underflow, wraparound, simultaneous_rw, reset_mid_operation.
