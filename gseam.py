#!/usr/bin/env python3
"""
gseam.py - merge, clean and validate CAM G-code for LinuxCNC.

Merges multiple CAM-exported .ngc files (Fusion 360, FreeCAD, ...) into one
program, or just validates files with --check. Successor of the original
GSeam f360_seam.py, rewritten structure-based:

  * per file: preamble / operations / footer are detected structurally
    (first toolchange line + the comment block directly above it), not via
    hardcoded line numbers
  * the merged program gets ONE header and ONE footer - no duplicated
    setup lines or duplicate N numbers
  * consecutive files that use the same tool skip the redundant "Tn M6"
    (no pointless M0 tool-change stop mid-job); disable with
    --keep-duplicate-toolchanges
  * safety checks: units (G21 required, G20 = hard error unless
    --allow-inch), G90 present, consistent work offsets (G54..G59.3),
    every T number present in tool.tbl (auto-found in the config dir)
  * extents report: X/Y/Z min/max overall + Zmin per tool (endpoint-based,
    arcs approximated by their endpoints)
  * Fusion "personal use" nag comments and machine-vendor boilerplate are
    dropped; operation/tool comments are kept (--keep-all-comments keeps all)

USAGE:
  gseam.py opdir/ merged.ngc              # all numbered .ngc files in opdir,
                                          # sorted by trailing number
  gseam.py op1.ngc op2.ngc merged.ngc     # explicit order
  gseam.py --check op1.ngc op2.ngc        # validate only, no output file
  gseam.py --dry-run opdir/ merged.ngc    # show the plan, write nothing

OPTIONS:
  --check                      validate input files, write nothing
  --dry-run                    show merge plan + report, write nothing
  --insert-toolchange-call     insert "O <toolchange> call" before each Tn M6
                               (for setups WITHOUT an M6 REMAP, like this one)
  --keep-duplicate-toolchanges keep Tn M6 even when the tool does not change
  --no-renumber                do not renumber (numbers are stripped instead)
  --step N                     renumber step (default 10)
  --keep-all-comments          keep every comment
  --allow-inch                 allow G20 files (default: hard error)
  --tool-table PATH            tool table to check T numbers against
                               (default: auto-detect ../tool.tbl next to
                               this script); --no-tool-check disables
  --log FILE                   also write the report to FILE
  --verbose                    debug output
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- regexes
RE_N_PREFIX = re.compile(r"^\s*N\d+\s*", re.IGNORECASE)
RE_TOOLCHANGE = re.compile(r"^\s*T(\d+)\s+M0?6\b", re.IGNORECASE)
RE_TOOL_ONLY = re.compile(r"^\s*T(\d+)\s*$", re.IGNORECASE)
RE_WCS = re.compile(r"\bG(5[4-9](?:\.[1-3])?)\b")
RE_UNITS = re.compile(r"\bG2([01])\b")
RE_G53 = re.compile(r"\bG53\b", re.IGNORECASE)
RE_G90 = re.compile(r"\bG90\b")
RE_MOTION_WORD = re.compile(r"\b([XYZ])\s*([-+]?\d*\.?\d+)", re.IGNORECASE)
RE_M30 = re.compile(r"\bM30\b|\bM0?2\b")
RE_OWORD = re.compile(r"^\s*[Oo][\s<]")

# comments considered "important" (operation/tool info) - kept by default
IMPORTANT_COMMENT = re.compile(
    r"^\(\s*(2d|3d|drill|contour|pocket|adaptive|facing|slot|bore|tap|"
    r"thread|trace|engrave|operation|tool|change|t\d+\s)", re.IGNORECASE)
# boilerplate that is actively dropped even with default filtering
NAG_COMMENT = re.compile(
    r"^\(\s*(when using fusion|moves is reduced|which can increase|"
    r"are available with|machine\b|\s*vendor|\s*model|\s*description)",
    re.IGNORECASE)


def strip_n(line: str) -> str:
    return RE_N_PREFIX.sub("", line)


def is_comment(line: str) -> bool:
    s = line.strip()
    return s.startswith("(") or s.startswith(";")


# ---------------------------------------------------------------- model
class Operation:
    def __init__(self, tool: int | None, lines: list[str]):
        self.tool = tool          # tool number of the Tn M6 (None = geen wissel)
        self.lines = lines        # stripped lines incl. op-comments + Tn M6


class ParsedFile:
    def __init__(self, path: Path):
        self.path = path
        self.preamble: list[str] = []   # setup lines from before the first op
        self.tool_comments: list[str] = []
        self.operations: list[Operation] = []
        self.units: str | None = None   # 'G21' / 'G20'
        self.wcs: set[str] = set()
        self.has_g90 = False
        self.warnings: list[str] = []
        self.errors: list[str] = []


class Extents:
    def __init__(self):
        self.mins: dict[str, float] = {}
        self.maxs: dict[str, float] = {}
        self.zmin_per_tool: dict[int, float] = {}

    def feed(self, line: str, tool: int | None):
        if RE_G53.search(line):
            return  # machine coords, not work coords
        for axis, val in RE_MOTION_WORD.findall(line):
            axis = axis.upper()
            v = float(val)
            self.mins[axis] = min(self.mins.get(axis, v), v)
            self.maxs[axis] = max(self.maxs.get(axis, v), v)
            if axis == "Z" and tool is not None:
                self.zmin_per_tool[tool] = min(
                    self.zmin_per_tool.get(tool, v), v)

    def report(self) -> list[str]:
        out = []
        for axis in ("X", "Y", "Z"):
            if axis in self.mins:
                out.append(f"  {axis}: {self.mins[axis]:10.3f} .. "
                           f"{self.maxs[axis]:10.3f} mm")
        for tool in sorted(self.zmin_per_tool):
            out.append(f"  Zmin T{tool}: {self.zmin_per_tool[tool]:.3f} mm")
        return out


# ---------------------------------------------------------------- parsing
def parse_file(path: Path, allow_inch: bool) -> ParsedFile:
    pf = ParsedFile(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = [ln.rstrip("\r\n") for ln in raw.splitlines()]
    # drop the % wrappers
    lines = [ln for ln in lines if ln.strip() != "%"]
    stripped = [strip_n(ln) for ln in lines]

    # file-wide facts
    for s in stripped:
        if is_comment(s):
            continue
        m = RE_UNITS.search(s)
        if m:
            pf.units = "G20" if m.group(1) == "0" else "G21"
        if RE_G90.search(s) and not RE_G53.search(s):
            pf.has_g90 = True
        for w in RE_WCS.findall(s):
            pf.wcs.add("G" + w)

    if pf.units == "G20" and not allow_inch:
        pf.errors.append("uses G20 (inch) - refuse to merge with a metric "
                         "setup (--allow-inch overrides)")
    if pf.units is None:
        pf.warnings.append("no G21/G20 found - units inherited at runtime")
    if not pf.has_g90:
        pf.warnings.append("no G90 found - absolute mode not guaranteed")

    # find operation starts: every Tn M6, with its comment block directly above
    tc_idx = [i for i, s in enumerate(stripped) if RE_TOOLCHANGE.match(s)]
    if not tc_idx:
        pf.errors.append("no toolchange (Tn M6) found - not a CAM operation "
                         "file?")
        return pf

    starts = []
    for i in tc_idx:
        j = i
        while j > 0 and is_comment(stripped[j - 1]):
            j -= 1
        starts.append(j)

    # preamble = everything before the first operation
    for s in stripped[:starts[0]]:
        st = s.strip()
        if not st:
            continue
        if is_comment(st):
            if re.match(r"^\(t\d+\s", st, re.IGNORECASE):
                pf.tool_comments.append(st)
            continue  # other header comments are rebuilt by the merger
        pf.preamble.append(st)

    # footer trimming: drop trailing end-of-program markers from the body
    end = len(stripped)
    while end > 0:
        st = stripped[end - 1].strip()
        if not st or RE_M30.fullmatch(st) or st == "%":
            end -= 1
            continue
        break

    # slice operations
    bounds = starts + [end]
    for k, i in enumerate(tc_idx):
        seg = [s for s in stripped[bounds[k]:bounds[k + 1]]
               if s.strip() != ""]
        tool = int(RE_TOOLCHANGE.match(stripped[i]).group(1))
        # tool comments inside the segment also go to the header collection
        for s in seg:
            if re.match(r"^\(t\d+\s", s.strip(), re.IGNORECASE):
                pf.tool_comments.append(s.strip())
        pf.operations.append(Operation(tool, seg))
    return pf


# ---------------------------------------------------------------- tool table
def read_tool_table(path: Path) -> set[int]:
    tools = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*T(\d+)\b", line)
        if m:
            tools.add(int(m.group(1)))
    return tools


def find_default_tool_table(script_path: Path) -> Path | None:
    cand = script_path.resolve().parent.parent / "tool.tbl"
    return cand if cand.is_file() else None


# ---------------------------------------------------------------- merging
def comment_filter(line: str, keep_all: bool) -> bool:
    """True = keep this comment line."""
    if keep_all:
        return True
    s = line.strip()
    if NAG_COMMENT.match(s):
        return False
    return bool(IMPORTANT_COMMENT.match(s))


def merge(parsed: list[ParsedFile], out_name: str, *,
          insert_toolchange_call: bool, skip_same_tool: bool,
          keep_all_comments: bool) -> tuple[list[str], dict]:
    """Return (lines-without-N-numbers, stats)."""
    stats = {"toolchanges": 0, "toolchange_calls": 0, "skipped_toolchanges": 0,
             "comments_kept": 0, "comments_removed": 0}
    out: list[str] = []

    out.append(f"({Path(out_name).stem.upper()} - merged by gseam)")
    out.append(f"(source: {', '.join(p.path.name for p in parsed)})")
    out.append(f"(generated: {datetime.now().isoformat(timespec='seconds')})")

    # collect unique tool-doc comments from all files, in order
    seen = set()
    for pf in parsed:
        for c in pf.tool_comments:
            if c not in seen:
                seen.add(c)
                out.append(c)

    # setup from the first file (deduplicated, in original order)
    seen_setup = set()
    for s in parsed[0].preamble:
        if s not in seen_setup:
            seen_setup.add(s)
            out.append(s)

    current_tool: int | None = None
    for pf in parsed:
        for op in pf.operations:
            for line in op.lines:
                st = line.strip()
                if is_comment(st):
                    if comment_filter(st, keep_all_comments):
                        out.append(st)
                        stats["comments_kept"] += 1
                    else:
                        stats["comments_removed"] += 1
                    continue
                if RE_TOOLCHANGE.match(st):
                    if skip_same_tool and op.tool == current_tool:
                        stats["skipped_toolchanges"] += 1
                        continue
                    if insert_toolchange_call:
                        out.append("O <toolchange> call")
                        stats["toolchange_calls"] += 1
                    stats["toolchanges"] += 1
                    current_tool = op.tool
                    out.append(st)
                    continue
                if RE_TOOL_ONLY.match(st):
                    continue  # bare Tn prefetch line - pointless after merge
                out.append(st)

    # single program footer
    out.append("M5")
    out.append("G53 G0 Z0.")
    out.append("M30")
    return out, stats


def renumber(lines: list[str], step: int) -> list[str]:
    out = []
    n = step
    for line in lines:
        s = line.strip()
        if not s or is_comment(s) or s == "%" or RE_OWORD.match(s):
            out.append(line)
        else:
            out.append(f"N{n} {line}")
            n += step
    return out


# ---------------------------------------------------------------- checks
def cross_file_checks(parsed: list[ParsedFile]) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    units = {pf.units for pf in parsed if pf.units}
    if len(units) > 1:
        errors.append(f"unit mismatch across files: {sorted(units)}")
    wcs_sets = [pf.wcs for pf in parsed if pf.wcs]
    if wcs_sets:
        union = set().union(*wcs_sets)
        if len(union) > 1:
            warnings.append(f"multiple work offsets used: {sorted(union)} - "
                            "intentional?")
    return errors, warnings


def tool_check(parsed: list[ParsedFile], table: set[int]) -> list[str]:
    used = {op.tool for pf in parsed for op in pf.operations
            if op.tool is not None}
    missing = sorted(used - table)
    return [f"tool T{t} not in tool table" for t in missing]


# ---------------------------------------------------------------- files/CLI
def numbered_ngc_files(directory: Path) -> list[Path]:
    def keynum(p: Path):
        m = re.search(r"(\d+)(?=\.ngc$)", p.name, re.IGNORECASE)
        return int(m.group(1)) if m else None
    files = [(keynum(p), p) for p in sorted(directory.glob("*.ngc"))]
    return [p for k, p in sorted((f for f in files if f[0] is not None),
                                 key=lambda f: f[0])]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Merge, clean and validate CAM G-code for LinuxCNC.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+",
                    help="input .ngc files or a directory, then the output "
                         "file (omit output with --check)")
    ap.add_argument("--check", action="store_true",
                    help="validate inputs only, write nothing")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--insert-toolchange-call", action="store_true")
    ap.add_argument("--keep-duplicate-toolchanges", action="store_true")
    ap.add_argument("--no-renumber", action="store_true")
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--keep-all-comments", action="store_true")
    ap.add_argument("--allow-inch", action="store_true")
    ap.add_argument("--tool-table", type=Path)
    ap.add_argument("--no-tool-check", action="store_true")
    ap.add_argument("--log", type=Path)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    report: list[str] = []

    def say(msg: str):
        print(msg)
        report.append(msg)

    # resolve inputs/output
    if args.check:
        in_args, out_path = args.inputs, None
    else:
        if len(args.inputs) < 2:
            ap.error("need at least one input and one output "
                     "(or use --check)")
        in_args, out_path = args.inputs[:-1], Path(args.inputs[-1])

    files: list[Path] = []
    for a in in_args:
        p = Path(a)
        if p.is_dir():
            found = numbered_ngc_files(p)
            if not found:
                say(f"ERROR: no numbered .ngc files in {p}")
                return 2
            files.extend(found)
        elif p.is_file():
            files.append(p)
        else:
            say(f"ERROR: {p} not found")
            return 2

    say("input order:")
    for f in files:
        say(f"  {f}")

    # parse + per-file checks
    parsed = [parse_file(f, args.allow_inch) for f in files]
    errors, warnings = [], []
    for pf in parsed:
        errors += [f"{pf.path.name}: {e}" for e in pf.errors]
        warnings += [f"{pf.path.name}: {w}" for w in pf.warnings]
    xe, xw = cross_file_checks(parsed)
    errors += xe
    warnings += xw

    # tool table check
    if not args.no_tool_check:
        table_path = args.tool_table or find_default_tool_table(Path(__file__))
        if table_path and table_path.is_file():
            say(f"tool table: {table_path}")
            errors += tool_check(parsed, read_tool_table(table_path))
        elif args.tool_table:
            errors.append(f"tool table {args.tool_table} not found")
        else:
            warnings.append("no tool.tbl found - tool check skipped")

    # extents
    ext = Extents()
    for pf in parsed:
        for op in pf.operations:
            for line in op.lines:
                if not is_comment(line):
                    ext.feed(line, op.tool)
    say("extents (work coords, endpoint-based):")
    for line in ext.report():
        say(line)

    for w in warnings:
        say(f"WARNING: {w}")
    for e in errors:
        say(f"ERROR: {e}")

    ops = [(pf.path.name, op.tool) for pf in parsed for op in pf.operations]
    say(f"operations: {len(ops)}  " +
        " ".join(f"[{name}:T{tool}]" for name, tool in ops))

    if errors:
        say("FAILED - fix the errors above; nothing written")
        _write_log(args.log, report)
        return 1

    if args.check:
        say("check OK")
        _write_log(args.log, report)
        return 0

    merged, stats = merge(
        parsed, out_path.name,
        insert_toolchange_call=args.insert_toolchange_call,
        skip_same_tool=not args.keep_duplicate_toolchanges,
        keep_all_comments=args.keep_all_comments)

    body = merged if args.no_renumber else renumber(merged, args.step)
    out_lines = ["%"] + body + ["%"]

    say("summary:")
    say(f"  output lines:          {len(out_lines)}")
    say(f"  toolchanges kept:      {stats['toolchanges']}")
    say(f"  toolchanges skipped:   {stats['skipped_toolchanges']} "
        "(same tool)")
    say(f"  toolchange calls:      {stats['toolchange_calls']}")
    say(f"  comments kept/removed: {stats['comments_kept']}"
        f"/{stats['comments_removed']}")

    if args.dry_run:
        say("dry-run: nothing written")
        _write_log(args.log, report)
        return 0

    out_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    say(f"written: {out_path}")
    _write_log(args.log, report)
    return 0


def _write_log(log_path: Path | None, report: list[str]):
    if log_path:
        log_path.write_text("\n".join(report) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
