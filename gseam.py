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
    r"^\(\s*(2d|3d|drill|spot|contour|pocket|adaptive|facing|slot|bore|tap|"
    r"thread|trace|engrave|face|ramp|helix|circular|operation|tool|change|"
    r"t\d+\s)", re.IGNORECASE)
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


# ---------------------------------------------------------------- analysis
RE_GWORD = re.compile(r"\bG(\d+(?:\.\d)?)\b", re.IGNORECASE)
RE_AXWORD = re.compile(r"\b([XYZF])\s*([-+]?\d*\.?\d+)", re.IGNORECASE)
RAPID_MMPM = 5000.0   # aanname voor de tijdschatting van rapids


class OpAnalysis:
    """Per-operation: drilled holes, extents, rough time, XY feed paths."""

    def __init__(self, tool: int | None):
        self.tool = tool
        self.holes: set[tuple[float, float]] = set()
        self.zmin: float | None = None
        self.feed_minutes = 0.0
        self.rapid_mm = 0.0
        self.segments: list[tuple] = []   # (x0,y0,x1,y1) feed-XY-segmenten


def analyze_op(op: Operation) -> OpAnalysis:
    an = OpAnalysis(op.tool)
    x = y = z = None
    feed = None
    motion = None          # 'G0' / 'G1' (G2/G3 tellen als feed)
    canned = False
    for line in op.lines:
        s = line.strip()
        if not s or is_comment(s):
            continue
        if RE_G53.search(s):
            x = y = z = None   # machine-coords beweging: positie onbekend
            continue
        gcodes = ["G" + g for g in RE_GWORD.findall(s)]
        if any(g in ("G81", "G82", "G83", "G73", "G85") for g in gcodes):
            canned = True
        if "G80" in gcodes:
            canned = False
        for g in gcodes:
            if g == "G0":
                motion = "G0"
            elif g in ("G1", "G2", "G3"):
                motion = "G1"
        words = {a.upper(): float(v) for a, v in RE_AXWORD.findall(s)}
        if "F" in words:
            feed = words["F"]
        nx = words.get("X", x)
        ny = words.get("Y", y)
        nz = words.get("Z", z)
        has_xy = "X" in words or "Y" in words
        has_z = "Z" in words

        # gat-detectie
        if canned and (has_xy or any(g.startswith("G8") and g != "G80"
                                     for g in gcodes)):
            if nx is not None and ny is not None:
                an.holes.add((round(nx, 2), round(ny, 2)))
        elif (motion == "G1" and has_z and not has_xy
                and words["Z"] < 0 and x is not None and y is not None):
            an.holes.add((round(x, 2), round(y, 2)))

        # zmin (canned: Z-woord is de bodem)
        if has_z and nz is not None and nz < (an.zmin if an.zmin is not None
                                              else 1e9):
            an.zmin = nz
        # afstand/tijd + feed-paden
        if motion in ("G0", "G1") and None not in (x, y, z, nx, ny, nz) \
                and not canned:
            dist = ((nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2) ** 0.5
            if motion == "G0":
                an.rapid_mm += dist
            elif feed:
                an.feed_minutes += dist / feed
                if has_xy and (nx, ny) != (x, y):
                    an.segments.append((x, y, nx, ny))
        elif canned and None not in (x, y, nx, ny):
            an.rapid_mm += ((nx - x) ** 2 + (ny - y) ** 2) ** 0.5
        x, y, z = nx, ny, nz
    return an


def tool_kind(tool: int | None, table: dict[int, dict]) -> str:
    desc = table.get(tool, {}).get("desc", "").lower()
    if "spot" in desc:
        return "spot"
    if "drill" in desc or "boor" in desc:
        return "drill"
    return "other"


def spot_coverage(op_infos: list[tuple], table: dict[int, dict]) -> list[str]:
    """op_infos: [(filename, op, analysis)]. Controleert dat elk boorgat
    eerst gespot is. Alleen actief als er spot- EN boor-operaties zijn."""
    spots: set = set()
    drills = []
    for name, op, an in op_infos:
        kind = tool_kind(op.tool, table)
        if kind == "spot":
            spots |= an.holes
        elif kind == "drill":
            drills.append((name, op, an))
    if not spots or not drills:
        return []
    problems = []
    for name, op, an in drills:
        missing = sorted(an.holes - spots)
        if missing:
            shown = ", ".join(f"({mx:g},{my:g})" for mx, my in missing[:6])
            more = f" (+{len(missing) - 6})" if len(missing) > 6 else ""
            problems.append(f"{name}: T{op.tool} boort {len(missing)} "
                            f"gat(en) dat NIET gespot is: {shown}{more}")
    return problems


# ---------------------------------------------------------------- --secure
def read_ini_limits(ini: Path) -> dict[str, tuple[float, float]]:
    limits, section = {}, None
    for line in ini.read_text(encoding="utf-8",
                              errors="replace").splitlines():
        s = line.strip()
        m = re.match(r"\[([A-Z_0-9]+)\]", s)
        if m:
            section = m.group(1)
            continue
        m = re.match(r"(MIN_LIMIT|MAX_LIMIT)\s*=\s*([-+0-9.]+)", s)
        if m and section in ("AXIS_X", "AXIS_Y", "AXIS_Z"):
            ax = section[-1]
            lo, hi = limits.get(ax, (None, None))
            if m.group(1) == "MIN_LIMIT":
                lo = float(m.group(2))
            else:
                hi = float(m.group(2))
            limits[ax] = (lo, hi)
    return {a: v for a, v in limits.items() if None not in v}


def read_var_g54(var: Path) -> dict[str, float]:
    vals = {}
    want = {"5221": "X", "5222": "Y", "5223": "Z", "5230": "R"}
    for line in var.read_text(encoding="utf-8",
                              errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in want:
            vals[want[parts[0]]] = float(parts[1])
    return vals


def secure_check(ext: "Extents", limits: dict, g54: dict) -> list[str]:
    """Werk-extents + G54 (incl. rotatie) vs machine-limieten."""
    import math
    problems = []
    if not all(a in ext.mins for a in "XY"):
        return ["--secure: geen XY-bewegingen gevonden om te controleren"]
    r = math.radians(g54.get("R", 0.0))
    c, s = math.cos(r), math.sin(r)
    xs, ys = [], []
    for wx in (ext.mins["X"], ext.maxs["X"]):
        for wy in (ext.mins["Y"], ext.maxs["Y"]):
            xs.append(g54.get("X", 0) + wx * c - wy * s)
            ys.append(g54.get("Y", 0) + wx * s + wy * c)
    ranges = {"X": (min(xs), max(xs)), "Y": (min(ys), max(ys))}
    if "Z" in ext.mins:
        zo = g54.get("Z", 0)
        ranges["Z"] = (ext.mins["Z"] + zo, ext.maxs["Z"] + zo)
    for ax, (lo, hi) in ranges.items():
        if ax not in limits:
            problems.append(f"--secure: geen limieten voor {ax}-as gevonden")
            continue
        mn, mx = limits[ax]
        if lo < mn - 1e-6:
            problems.append(f"{ax} onderschrijdt machine-limiet: "
                            f"{lo:.1f} < {mn:.1f} (machine-coords)")
        if hi > mx + 1e-6:
            problems.append(f"{ax} overschrijdt machine-limiet: "
                            f"{hi:.1f} > {mx:.1f} (machine-coords)")
    return problems


# ---------------------------------------------------------------- job card
def job_card(op_infos: list[tuple], table: dict[int, dict]) -> list[str]:
    lines = []
    total = 0.0
    for name, op, an in op_infos:
        mins = an.feed_minutes + an.rapid_mm / RAPID_MMPM
        total += mins
        desc = table.get(op.tool, {}).get("desc", "")
        desc = re.sub(r"[()]", "", desc)[:40].strip()
        bits = [f"T{op.tool}"]
        if desc:
            bits.append(desc)
        if an.holes:
            bits.append(f"{len(an.holes)} gaten")
        if an.zmin is not None:
            bits.append(f"Zmin {an.zmin:g}")
        bits.append(f"~{max(1, round(mins))} min")
        lines.append("(OP: " + " - ".join(bits) + ")")
    lines.insert(0, f"(JOB: {len(op_infos)} operaties, "
                    f"~{max(1, round(total))} min excl. toolwissels)")
    return lines


# ---------------------------------------------------------------- SVG preview
_SVG_COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
               "#42d4f4", "#f032e6", "#9a6324"]


def write_svg(path: Path, op_infos: list[tuple], table: dict[int, dict],
              ext: "Extents"):
    if "X" not in ext.mins or "Y" not in ext.mins:
        return False
    pad = 10.0
    x0, x1 = ext.mins["X"] - pad, ext.maxs["X"] + pad
    y0, y1 = ext.mins["Y"] - pad, ext.maxs["Y"] + pad
    w, h = x1 - x0, y1 - y0
    scale = 900.0 / max(w, h)
    W, H = w * scale, h * scale
    legend_h = 22 * (len({op.tool for _, op, _ in op_infos}) + 1)

    def sx(x):
        return (x - x0) * scale

    def sy(y):
        return H - (y - y0) * scale   # machine-Y omhoog

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" '
             f'width="{W:.0f}" height="{H + legend_h:.0f}" '
             f'viewBox="0 0 {W:.0f} {H + legend_h:.0f}">',
             f'<rect x="0" y="0" width="{W:.0f}" height="{H:.0f}" '
             f'fill="#fafafa" stroke="#999"/>']
    # werk-nulpunt
    if x0 < 0 < x1 and y0 < 0 < y1:
        parts.append(f'<path d="M {sx(0):.1f} {sy(0) - 8:.1f} v 16 '
                     f'M {sx(0) - 8:.1f} {sy(0):.1f} h 16" '
                     f'stroke="#000" stroke-width="1"/>')
    color_of = {}
    for name, op, an in op_infos:
        col = color_of.setdefault(
            op.tool, _SVG_COLORS[len(color_of) % len(_SVG_COLORS)])
        for (ax, ay, bx, by) in an.segments:
            parts.append(f'<line x1="{sx(ax):.1f}" y1="{sy(ay):.1f}" '
                         f'x2="{sx(bx):.1f}" y2="{sy(by):.1f}" '
                         f'stroke="{col}" stroke-width="1" opacity="0.6"/>')
        rad = max(table.get(op.tool, {}).get("diam", 2.0) / 2.0, 0.5)
        for (hx, hy) in sorted(an.holes):
            parts.append(f'<circle cx="{sx(hx):.1f}" cy="{sy(hy):.1f}" '
                         f'r="{rad * scale:.1f}" fill="none" '
                         f'stroke="{col}" stroke-width="1.5"/>')
    ly = H + 16
    parts.append(f'<text x="4" y="{ly:.0f}" font-size="12" '
                 f'font-family="monospace">X {ext.mins["X"]:g}..'
                 f'{ext.maxs["X"]:g}  Y {ext.mins["Y"]:g}..'
                 f'{ext.maxs["Y"]:g} mm</text>')
    for tool, col in color_of.items():
        ly += 22
        desc = re.sub(r"[()<>&]", "", table.get(tool, {}).get("desc", ""))
        holes = sum(len(an.holes) for _, op, an in op_infos
                    if op.tool == tool)
        parts.append(f'<circle cx="10" cy="{ly - 4:.0f}" r="6" fill="none" '
                     f'stroke="{col}" stroke-width="2"/>')
        parts.append(f'<text x="22" y="{ly:.0f}" font-size="12" '
                     f'font-family="monospace">T{tool} {desc[:48]} '
                     f'({holes} gaten)</text>')
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------- tool table
def read_tool_table(path: Path) -> dict[int, dict]:
    """T-number -> {'diam': float, 'desc': str} from a tool.tbl."""
    tools: dict[int, dict] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*T\s*(\d+)\b", line)
        if not m:
            continue
        dm = re.search(r"\bD\s*([-+]?[\d.]+)", line)
        cm = re.search(r";\s*(.+)$", line)
        tools[int(m.group(1))] = {
            "diam": float(dm.group(1)) if dm else 0.0,
            "desc": cm.group(1).strip() if cm else "",
        }
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
          keep_all_comments: bool, jobcard: list[str] | None = None
          ) -> tuple[list[str], dict]:
    """Return (lines-without-N-numbers, stats)."""
    stats = {"toolchanges": 0, "toolchange_calls": 0, "skipped_toolchanges": 0,
             "comments_kept": 0, "comments_removed": 0}
    out: list[str] = []

    out.append(f"({Path(out_name).stem.upper()} - merged by gseam)")
    out.append(f"(source: {', '.join(p.path.name for p in parsed)})")
    out.append(f"(generated: {datetime.now().isoformat(timespec='seconds')})")
    out.extend(jobcard or [])

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


def tool_check(parsed: list[ParsedFile], table: dict[int, dict]) -> list[str]:
    used = {op.tool for pf in parsed for op in pf.operations
            if op.tool is not None}
    missing = sorted(used - set(table))
    return [f"tool T{t} not in tool table" for t in missing]


# ---------------------------------------------------------------- files/CLI
RE_PART_OF = re.compile(r"P(\d+)of(\d+)", re.IGNORECASE)


def numbered_ngc_files(directory: Path) -> tuple[list[Path], list[str]]:
    """Order the .ngc files in a directory; returns (files, errors).

    Two naming schemes:
      * "PxofN" (e.g. job_P2of4.ngc): ordered by part number x, and the set
        must be COMPLETE (all 1..N exactly once) - a missing part aborts,
        because merging without e.g. the spot-drill pass is dangerous.
      * otherwise: sorted by the trailing number in the filename
        (op1.ngc, op2.ngc, ...); files without a number are ignored.
    Mixing both schemes in one directory is an error.
    """
    all_files = sorted(directory.glob("*.ngc"))
    parts = [(p, RE_PART_OF.search(p.name)) for p in all_files]
    matched = [(p, m) for p, m in parts if m]

    if matched:
        if len(matched) != len(all_files):
            rest = [p.name for p, m in parts if not m]
            return [], [f"mixed naming in {directory}: PxofN files together "
                        f"with {rest} - order would be a guess, aborting"]
        totals = {int(m.group(2)) for _, m in matched}
        if len(totals) > 1:
            return [], [f"PxofN sets with different totals in {directory}: "
                        f"{sorted(totals)}"]
        total = totals.pop()
        index = {}
        for p, m in matched:
            i = int(m.group(1))
            if i in index:
                return [], [f"duplicate part P{i}of{total}: "
                            f"{index[i].name} and {p.name}"]
            index[i] = p
        missing = [i for i in range(1, total + 1) if i not in index]
        if missing:
            return [], ["incomplete PxofN set: missing part(s) " +
                        ", ".join(f"P{i}of{total}" for i in missing)]
        return [index[i] for i in range(1, total + 1)], []

    def keynum(p: Path):
        m = re.search(r"(\d+)(?=\.ngc$)", p.name, re.IGNORECASE)
        return int(m.group(1)) if m else None
    files = [(keynum(p), p) for p in all_files]
    return [p for k, p in sorted((f for f in files if f[0] is not None),
                                 key=lambda f: f[0])], []


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
    ap.add_argument("--secure", action="store_true",
                    help="controleer extents + actuele G54 tegen de "
                         "machine-limieten uit de .ini; spot-dekking wordt "
                         "dan ook een fout i.p.v. waarschuwing")
    ap.add_argument("--ini", type=Path,
                    help="pad naar de LinuxCNC .ini (default: auto)")
    ap.add_argument("--var", type=Path,
                    help="pad naar linuxcnc.var (default: auto)")
    ap.add_argument("--preview", action="store_true",
                    help="schrijf een top-down SVG-preview naast de output")
    ap.add_argument("--preview-file", type=Path, metavar="SVG",
                    help="eigen pad voor de SVG-preview (impliceert "
                         "--preview)")
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
            found, order_errors = numbered_ngc_files(p)
            for e in order_errors:
                say(f"ERROR: {e}")
            if order_errors:
                return 2
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
    table: dict[int, dict] = {}
    table_path = args.tool_table or find_default_tool_table(Path(__file__))
    if not args.no_tool_check:
        if table_path and table_path.is_file():
            say(f"tool table: {table_path}")
            table = read_tool_table(table_path)
            errors += tool_check(parsed, table)
        elif args.tool_table:
            errors.append(f"tool table {args.tool_table} not found")
        else:
            warnings.append("no tool.tbl found - tool check skipped")

    # extents + per-operatie analyse (gaten, tijden, paden)
    ext = Extents()
    op_infos = []
    for pf in parsed:
        for op in pf.operations:
            for line in op.lines:
                if not is_comment(line):
                    ext.feed(line, op.tool)
            op_infos.append((pf.path.name, op, analyze_op(op)))
    say("extents (work coords, endpoint-based):")
    for line in ext.report():
        say(line)

    # spot-voor-boor dekking (alleen als er spot- EN boor-operaties zijn)
    coverage = spot_coverage(op_infos, table)
    if coverage:
        if args.secure:
            errors += coverage
        else:
            warnings += coverage
    elif table and any(tool_kind(op.tool, table) == "spot"
                       for _, op, _ in op_infos):
        say("spot-dekking: alle boorgaten zijn eerst gespot")

    # --secure: machine-limieten vs extents + actuele G54
    if args.secure:
        cfg_dir = (table_path.parent if table_path and table_path.is_file()
                   else Path(__file__).resolve().parent.parent)
        ini = args.ini or next(iter(sorted(cfg_dir.glob("*.ini"))), None)
        var = args.var or (cfg_dir / "linuxcnc.var")
        if not ini or not Path(ini).is_file():
            errors.append("--secure: geen .ini gevonden (geef --ini op)")
        elif not Path(var).is_file():
            errors.append("--secure: geen linuxcnc.var gevonden "
                          "(geef --var op)")
        else:
            limits = read_ini_limits(Path(ini))
            g54 = read_var_g54(Path(var))
            say(f"secure: limieten uit {Path(ini).name}, G54 uit "
                f"{Path(var).name} (X{g54.get('X', 0):.1f} "
                f"Y{g54.get('Y', 0):.1f} Z{g54.get('Z', 0):.1f} "
                f"R{g54.get('R', 0):.2f})")
            problems = secure_check(ext, limits, g54)
            if problems:
                errors += problems
            else:
                say("secure: programma past binnen de machine-limieten")

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

    card = job_card(op_infos, table)
    for line in card:
        say(line)

    merged, stats = merge(
        parsed, out_path.name,
        insert_toolchange_call=args.insert_toolchange_call,
        skip_same_tool=not args.keep_duplicate_toolchanges,
        keep_all_comments=args.keep_all_comments,
        jobcard=card)

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

    if args.preview or args.preview_file:
        svg = args.preview_file or out_path.with_suffix(".svg")
        if write_svg(svg, op_infos, table, ext):
            say(f"preview: {svg}")
        else:
            say("preview: geen XY-data om te tekenen")

    _write_log(args.log, report)
    return 0


def _write_log(log_path: Path | None, report: list[str]):
    if log_path:
        log_path.write_text("\n".join(report) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
