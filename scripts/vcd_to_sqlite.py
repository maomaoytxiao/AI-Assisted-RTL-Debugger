#!/usr/bin/env python3
"""Convert a VCD waveform into a normalized SQLite database.

Usage:
    python3 scripts/vcd_to_sqlite.py build/sync_fifo.vcd build/waves.db

Design (simplified version of the BTTF waveform store):
  * signal_metadata - one row per declared hierarchical signal.
  * signal_changes  - one row per value-change event (not per-cycle samples).
  * waveform_info   - key/value facts about the source VCD (timescale, ...).

VCD identifier aliases (several hierarchical names sharing one identifier,
e.g. tb.clk and tb.dut.clk) are stored once: the first declaration owns the
change rows and later declarations point to it through `alias_of`.

The parser streams the file token by token, so memory use is bounded by the
number of declared signals plus one insert batch, not by waveform length.
Only the Python standard library is used.
"""

import argparse
import os
import sqlite3
import sys
import time

CONVERTER_VERSION = "1.0"
DEFAULT_BATCH_SIZE = 10000

SCHEMA = """
CREATE TABLE waveform_info (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE signal_metadata (
    signal_id         INTEGER PRIMARY KEY,
    hierarchical_name TEXT    NOT NULL UNIQUE,  -- e.g. tb_sync_fifo.dut.wr_ptr
    scope             TEXT    NOT NULL,         -- e.g. tb_sync_fifo.dut
    name              TEXT    NOT NULL,         -- e.g. wr_ptr
    scope_type        TEXT,                     -- module / begin / task / ...
    width             INTEGER NOT NULL,
    bit_range         TEXT,                     -- e.g. [3:0], NULL if none
    signal_type       TEXT    NOT NULL,         -- wire / reg / integer / real ...
    vcd_identifier    TEXT    NOT NULL,
    alias_of          INTEGER REFERENCES signal_metadata(signal_id),
    change_count      INTEGER NOT NULL DEFAULT 0
);

-- change_id is an INTEGER PRIMARY KEY so it is stable across VACUUM and
-- records the original event order within the VCD (ties at one timestamp).
CREATE TABLE signal_changes (
    change_id INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL REFERENCES signal_metadata(signal_id),
    timestamp INTEGER NOT NULL,                 -- in VCD timescale ticks
    value     TEXT    NOT NULL                  -- full-width binary, lowercase x/z
);
"""

# Indexes are created after the bulk load (faster than maintaining them).
INDEXES = """
CREATE INDEX idx_changes_signal_time ON signal_changes(signal_id, timestamp);
CREATE INDEX idx_changes_time        ON signal_changes(timestamp);
CREATE INDEX idx_meta_name           ON signal_metadata(name);
CREATE INDEX idx_meta_scope          ON signal_metadata(scope);
CREATE INDEX idx_meta_vcd_id         ON signal_metadata(vcd_identifier);

-- Convenience view: changes addressed by any hierarchical name, aliases
-- included. Intended for ad-hoc / schema-aware SQL.
CREATE VIEW v_signal_changes AS
SELECT m.hierarchical_name, m.width, c.timestamp, c.value, c.change_id
FROM signal_metadata AS m
JOIN signal_changes  AS c ON c.signal_id = COALESCE(m.alias_of, m.signal_id);
"""

TIME_UNITS = {"s": 0, "ms": -3, "us": -6, "ns": -9, "ps": -12, "fs": -15}


class VCDParseError(Exception):
    pass


def tokens(path):
    """Yield whitespace-separated tokens, streaming the file line by line."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            for tok in line.split():
                yield tok


def read_until_end(tok_iter):
    """Collect tokens up to (not including) the next $end."""
    out = []
    for tok in tok_iter:
        if tok == "$end":
            return out
        out.append(tok)
    raise VCDParseError("unexpected end of file inside a $... $end section")


def parse_timescale(parts):
    text = "".join(parts)                    # handles "1ps" and "1 ps"
    digits = "".join(ch for ch in text if ch.isdigit())
    unit = text[len(digits):].strip().lower()
    if digits not in ("1", "10", "100") or unit not in TIME_UNITS:
        raise VCDParseError("unsupported $timescale: %r" % text)
    return int(digits), unit


def normalize_value(raw, width):
    """Return a full-width lowercase binary string.

    VCD allows vectors to be written with fewer bits than the declared width;
    IEEE 1364 left-extends with 0, except when the leftmost given bit is x or
    z, which is extended with x or z respectively.
    """
    v = raw.lower()
    if len(v) < width:
        fill = v[0] if v[0] in "xz" else "0"
        v = fill * (width - len(v)) + v
    return v


def convert(vcd_path, db_path, batch_size=DEFAULT_BATCH_SIZE, quiet=False):
    t_start = time.time()
    tmp_path = db_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    conn = sqlite3.connect(tmp_path)
    conn.execute("PRAGMA journal_mode = OFF")     # build-once file, written
    conn.execute("PRAGMA synchronous = OFF")      # atomically via rename
    conn.executescript(SCHEMA)

    info = {"source_vcd": os.path.abspath(vcd_path),
            "source_size_bytes": str(os.path.getsize(vcd_path)),
            "converter_version": CONVERTER_VERSION}
    scope_stack = []            # list of (type, name)
    id_to_db = {}               # vcd identifier -> owning signal_id
    id_width = {}               # vcd identifier -> width
    id_kind = {}                # vcd identifier -> signal_type
    meta_rows = []
    next_id = 1
    warnings = []

    tok_iter = tokens(vcd_path)

    # ---------------------------------------------------------------- header
    for tok in tok_iter:
        if tok == "$enddefinitions":
            read_until_end(tok_iter)
            break
        elif tok == "$scope":
            parts = read_until_end(tok_iter)
            scope_stack.append((parts[0], parts[1] if len(parts) > 1 else ""))
        elif tok == "$upscope":
            read_until_end(tok_iter)
            if not scope_stack:
                raise VCDParseError("$upscope without matching $scope")
            scope_stack.pop()
        elif tok == "$var":
            parts = read_until_end(tok_iter)
            if len(parts) < 4:
                raise VCDParseError("malformed $var: %r" % parts)
            kind, width, ident, name = parts[0], int(parts[1]), parts[2], parts[3]
            bit_range = "".join(parts[4:]) or None
            scope = ".".join(s[1] for s in scope_stack)
            scope_type = scope_stack[-1][0] if scope_stack else None
            hier = scope + "." + name if scope else name
            alias_of = None
            if ident in id_to_db:
                alias_of = id_to_db[ident]
                if id_width[ident] != width:
                    warnings.append("alias %s width %d != %d" %
                                    (hier, width, id_width[ident]))
            else:
                id_to_db[ident] = next_id
                id_width[ident] = width
                id_kind[ident] = kind
            meta_rows.append((next_id, hier, scope, name, scope_type, width,
                              bit_range, kind, ident, alias_of))
            next_id += 1
        elif tok == "$timescale":
            mag, unit = parse_timescale(read_until_end(tok_iter))
            info["timescale"] = "%d%s" % (mag, unit)
            info["timescale_magnitude"] = str(mag)
            info["timescale_unit"] = unit
            info["timescale_exponent"] = str(TIME_UNITS[unit])
        elif tok in ("$date", "$version", "$comment"):
            text = " ".join(read_until_end(tok_iter))
            if tok != "$comment":
                info[tok[1:]] = text
        elif tok.startswith("$"):
            read_until_end(tok_iter)          # unknown header section
        else:
            raise VCDParseError("unexpected token in header: %r" % tok)
    else:
        raise VCDParseError("no $enddefinitions found")

    if "timescale" not in info:
        # IEEE 1364 does not define a default; 1s is what most tools assume.
        info.update(timescale="1s", timescale_magnitude="1",
                    timescale_unit="s", timescale_exponent="0")
        warnings.append("no $timescale in VCD; assuming 1s")

    try:
        conn.executemany(
            "INSERT INTO signal_metadata (signal_id, hierarchical_name, scope,"
            " name, scope_type, width, bit_range, signal_type, vcd_identifier,"
            " alias_of) VALUES (?,?,?,?,?,?,?,?,?,?)", meta_rows)
    except sqlite3.IntegrityError as e:
        raise VCDParseError("duplicate hierarchical signal name: %s" % e)

    # ------------------------------------------------------------------ body
    insert_sql = ("INSERT INTO signal_changes (signal_id, timestamp, value)"
                  " VALUES (?,?,?)")
    batch = []
    n_changes = 0
    n_timestamps = 0
    cur_time = None
    first_time = None
    unknown_ids = set()

    def emit(ident, raw):
        nonlocal n_changes
        sid = id_to_db.get(ident)
        if sid is None:
            unknown_ids.add(ident)
            return
        if cur_time is None:
            raise VCDParseError("value change before any #timestamp")
        if id_kind[ident] in ("real", "realtime"):
            value = raw
        else:
            value = normalize_value(raw, id_width[ident])
        batch.append((sid, cur_time, value))
        n_changes += 1
        if len(batch) >= batch_size:
            conn.executemany(insert_sql, batch)
            batch.clear()

    for tok in tok_iter:
        c = tok[0]
        if c == "#":
            t = int(tok[1:])
            if cur_time is not None and t < cur_time:
                raise VCDParseError("timestamps go backwards: %d < %d" % (t, cur_time))
            cur_time = t
            if first_time is None:
                first_time = t
            n_timestamps += 1
        elif c in "01xXzZ":
            emit(tok[1:], c)
        elif c in "bB":
            emit(next(tok_iter), tok[1:])
        elif c in "rR":
            emit(next(tok_iter), tok[1:])
        elif c in "sS":                              # string values (rare)
            emit(next(tok_iter), tok[1:])
        elif tok == "$comment":
            read_until_end(tok_iter)
        elif tok in ("$dumpvars", "$dumpall", "$dumpon", "$dumpoff", "$end"):
            pass        # the values inside these blocks are ordinary changes
        else:
            raise VCDParseError("unexpected token in body: %r" % tok)

    if batch:
        conn.executemany(insert_sql, batch)

    if unknown_ids:
        warnings.append("%d undeclared identifiers in body ignored: %s"
                        % (len(unknown_ids), sorted(unknown_ids)[:10]))

    # --------------------------------------------------- indexes and summary
    conn.executescript(INDEXES)
    conn.execute(
        "UPDATE signal_metadata SET change_count = ("
        " SELECT COUNT(*) FROM signal_changes c"
        " WHERE c.signal_id = COALESCE(signal_metadata.alias_of,"
        "                              signal_metadata.signal_id))")

    info.update({
        "num_signals": str(len(meta_rows)),
        "num_unique_identifiers": str(len(id_to_db)),
        "num_value_changes": str(n_changes),
        "num_timestamps": str(n_timestamps),
        "start_time": str(first_time if first_time is not None else 0),
        "end_time": str(cur_time if cur_time is not None else 0),
        "created_unix": "%.0f" % time.time(),
    })
    conn.executemany("INSERT INTO waveform_info (key, value) VALUES (?, ?)",
                     sorted(info.items()))
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.close()
    os.replace(tmp_path, db_path)

    summary = {
        "signals": len(meta_rows),
        "unique_identifiers": len(id_to_db),
        "aliases": len(meta_rows) - len(id_to_db),
        "value_changes": n_changes,
        "timestamps": n_timestamps,
        "end_time": cur_time,
        "timescale": info["timescale"],
        "db_size_bytes": os.path.getsize(db_path),
        "seconds": round(time.time() - t_start, 3),
        "warnings": warnings,
    }
    if not quiet:
        print("vcd_to_sqlite: %s -> %s" % (vcd_path, db_path))
        for k in ("signals", "unique_identifiers", "aliases", "value_changes",
                  "timestamps", "end_time", "timescale", "db_size_bytes",
                  "seconds"):
            print("  %-19s %s" % (k, summary[k]))
        for w in warnings:
            print("  WARNING: %s" % w)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("vcd", help="input VCD file")
    ap.add_argument("db", help="output SQLite database (overwritten)")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        convert(args.vcd, args.db, args.batch_size, args.quiet)
    except (VCDParseError, OSError) as e:
        print("vcd_to_sqlite: ERROR: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
