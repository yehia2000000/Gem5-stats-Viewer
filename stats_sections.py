#!/usr/bin/env python3

import argparse
import csv
import re
import signal
import sys

if hasattr(signal, "SIGPIPE"):                       # not on Windows
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # quiet exit when piped into head / less

BEGIN = "Begin Simulation Statistics"
END = "End Simulation Statistics"
DEFAULT_STATS = [r"^system\.cpu\.numCycles$",
                 r"^system\.cpu\.committedInsts(::0)?$",
                 r"^system\.cpu\.ipc(::0)?$"]
# Ratios cannot be added across sections: --by-stage gives their cycle-weighted mean
# (exact for IPC: sum(ipc_i * cycles_i) / sum(cycles_i) = sum(insts) / sum(cycles)).
RATIO = re.compile(r"(ipc|cpi|[Rr]ate|[Rr]atio|[Aa]vg|[Ff]rac|[Pp]ercent)")
CYCLES = "system.cpu.numCycles"


def read_sections(path):
    """List of sections; each is (raw lines, {stat name: first value})."""
    sections, lines, stats, inside = [], [], {}, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if BEGIN in line:
                lines, stats, inside = [line], {}, True
            elif END in line and inside:
                lines.append(line)
                sections.append((lines, stats))
                inside = False
            elif inside:
                lines.append(line)
                tok = line.split()
                if len(tok) >= 2 and not tok[0].startswith("#"):
                    try:
                        stats[tok[0]] = float(tok[1])
                    except ValueError:
                        pass                      # text values: not a number
    return sections


def read_legend(path):
    """{section number: (frame, stage)} from the benchmark output."""
    legend = {}
    pat = re.compile(r"section\s+(\d+)\s*=\s*frame\s+(\d+)\s+(\S+)")
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pat.search(line)
            if m:
                legend[int(m.group(1))] = (int(m.group(2)), m.group(3))
    return legend


def all_names(sections):
    """Every stat name, in stats.txt order (union over the sections)."""
    seen = {}
    for _, st in sections:
        for n in st:
            seen.setdefault(n, None)
    return list(seen)


def read_descriptions(sections):
    """{stat name: text after '#'} from the first section that has the stat."""
    desc = {}
    for lines, _ in sections:
        for line in lines:
            tok = line.split(None, 1)
            if tok and tok[0] not in desc and "#" in line:
                desc[tok[0]] = line.split("#", 1)[1].strip()
    return desc


def by_stage(sections, legend, names):
    """Sections of the same stage combined, per frame.

    Returns (order, calls, values, frames): stage names in run order, calls per frame,
    {stage: {name: value or None}}, and the frame numbers. Counts: sum over the stage's
    sections / frames. Ratios (RATIO): cycle-weighted mean (exact for IPC)."""
    frames = sorted({f for f, _ in legend.values()})
    order, acc = [], {}
    for i, (_, st) in enumerate(sections, 1):
        if i not in legend:
            continue
        stage = legend[i][1]
        if stage not in acc:
            order.append(stage)
            acc[stage] = {"calls": 0, "sum": {}, "w": {}}
        e = acc[stage]
        e["calls"] += 1
        cyc = st.get(CYCLES, 1.0)
        for c in names:
            v = st.get(c)
            if v is None or v != v:
                continue
            if RATIO.search(c):
                e["sum"][c] = e["sum"].get(c, 0.0) + v * cyc
                e["w"][c] = e["w"].get(c, 0.0) + cyc
            else:
                e["sum"][c] = e["sum"].get(c, 0.0) + v
    nf = len(frames) or 1
    values = {}
    for stage in order:
        e = acc[stage]
        values[stage] = {c: (e["sum"][c] / e["w"][c] if e["w"].get(c) else None) if RATIO.search(c)
                         else (e["sum"][c] / nf if c in e["sum"] else None) for c in names}
    calls = {s: acc[s]["calls"] / nf for s in order}
    return order, calls, values, frames


def pick_columns(sections, patterns):
    """Stat names matching any pattern, in stats.txt order, from the first section."""
    regs = [re.compile(p) for p in patterns]
    names = list(sections[0][1]) if sections else []
    cols = [n for n in names if any(r.search(n) for r in regs)]
    for p, r in zip(patterns, regs):
        if not any(r.search(n) for n in names):
            print(f"warning: no stat matches {p!r} (try --grep)", file=sys.stderr)
    return cols


def fmt(v):
    if v is None:
        return "-"
    if v != v:                                      # nan
        return "nan"
    if abs(v) >= 1000 or (abs(v - round(v)) < 1e-9 and abs(v) < 1e15):
        return f"{v:,.0f}"                          # counts, and per-frame averages of counts
    return f"{v:.4g}"                               # ratios (IPC, miss rates)


def print_table(header, rows, csv_path):
    width = [max(len(str(h)), *(len(fmt(r[i]) if not isinstance(r[i], str) else r[i]) for r in rows))
             for i, h in enumerate(header)]
    cell = lambda v: v if isinstance(v, str) else fmt(v)
    print("  ".join(str(h).ljust(w) if i < 3 else str(h).rjust(w) for i, (h, w) in enumerate(zip(header, width))))
    for r in rows:
        print("  ".join(cell(v).ljust(w) if i < 3 else cell(v).rjust(w) for i, (v, w) in enumerate(zip(r, width))))
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows([[v if isinstance(v, str) else ("" if v is None else v) for v in r] for r in rows])
        print(f"\nwrote {csv_path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stats", help="m5out/stats.txt")
    ap.add_argument("log", nargs="?", help="benchmark output with the 'section n = ...' lines")
    ap.add_argument("-s", "--stat", action="append", help="regex of stat names to show (repeatable)")
    ap.add_argument("--by-stage", action="store_true", help="sum the sections of each stage, per frame")
    ap.add_argument("--grep", metavar="REGEX", help="list the stat names (first section) that match")
    ap.add_argument("--section", type=int, metavar="N", help="print section N (1-based) as in stats.txt")
    ap.add_argument("--csv", metavar="FILE", help="also write the table as CSV")
    a = ap.parse_args()

    sections = read_sections(a.stats)
    if not sections:
        sys.exit(f"{a.stats}: no '{BEGIN}' sections")

    if a.section:
        if not 1 <= a.section <= len(sections):
            sys.exit(f"section {a.section}: stats.txt has {len(sections)}")
        sys.stdout.writelines(sections[a.section - 1][0])
        return
    if a.grep:
        r = re.compile(a.grep)
        for name, v in sections[0][1].items():
            if r.search(name):
                print(f"{name}  {fmt(v)}")
        return

    legend = read_legend(a.log) if a.log else {}
    if legend and len(sections) != len(legend) + 1:
        print(f"warning: stats.txt has {len(sections)} sections, the legend {len(legend)} + 1 at exit. "
              "An extra section usually means --warmup-insts-no-switch; the names below may be shifted.",
              file=sys.stderr)
    cols = pick_columns(sections, a.stat or DEFAULT_STATS)

    def label(i):                                   # i is 1-based
        if i in legend:
            return legend[i]
        return (None, "exit (ignore)" if legend and i == len(sections) else "?")

    if not a.by_stage:
        rows = []
        for i, (_, st) in enumerate(sections, 1):
            frame, stage = label(i)
            rows.append([str(i), "" if frame is None else str(frame), stage] + [st.get(c) for c in cols])
        print_table(["section", "frame", "stage"] + cols, rows, a.csv)
        return

    if not legend:
        sys.exit("--by-stage needs the benchmark output (LOG) to know the stages")
    order, calls, values, frames = by_stage(sections, legend, cols)
    rows, tot = [], {c: 0.0 for c in cols}
    for stage in order:
        vals = [values[stage][c] for c in cols]
        for c, v in zip(cols, vals):
            if not RATIO.search(c) and v is not None:
                tot[c] += v
        rows.append([stage, f"{calls[stage]:g}", ""] + vals)
    rows.append(["sum of stages", "", ""] + [None if RATIO.search(c) else tot[c] for c in cols])
    print(f"per frame, averaged over frames {frames[0]}-{frames[-1]} "
          f"(ratios: cycle-weighted mean)", file=sys.stderr)
    print_table(["stage", "calls/frame", ""] + cols, rows, a.csv)


if __name__ == "__main__":
    main()
