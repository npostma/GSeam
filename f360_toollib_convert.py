#!/usr/bin/env python3
"""
f360_toollib_convert.py - Fusion 360 tool library (Library.json) -> LinuxCNC
tool.tbl.

Improvements over the original GSeam version:
  * PRESERVES measured Z length offsets: by default, tools that already
    exist in the output tool.tbl keep their current Z (re-syncing the
    library no longer wipes what you measured). Override with --z-source.
  * shows a DIFF (added / removed / changed tools) before writing
  * makes a .bak backup of the existing table
  * skips duplicate tool numbers with a warning, warns on pocket collisions
  * output format matches this config's tool.tbl (D+3.000 Z+0.000 style)

USAGE:
  f360_toollib_convert.py Library.json -o ../tool.tbl        # sync, keep Z
  f360_toollib_convert.py Library.json -o ../tool.tbl --dry-run
  f360_toollib_convert.py Library.json --z-source zero       # reset all Z
  f360_toollib_convert.py Library.json --z-source assembly   # gauge length
  f360_toollib_convert.py Library.json --z-value -142.3      # fixed Z

Pocket mapping (default: P == T):
  --pocket-offset N            pocket = tool + N
  --pocket-fixed N             same pocket for every tool
  --pocket-map "T1:5,T2:3"     explicit mapping
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

UNIT_NUMBER_RE = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)")
# tolerant: ook oude tabellen met 'T  1' / 'T 40' (spatie) worden gelezen
TBL_LINE_RE = re.compile(
    r"^\s*T\s*(\d+)\s+P\s*(\d+)\s+D\s*([-+]?[\d.]+)\s+Z\s*([-+]?[\d.]+)")


def parse_number(val, default=None):
    """Parse float from float/int/'3 mm'-style strings."""
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    m = UNIT_NUMBER_RE.match(str(val))
    if not m:
        return default
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return default


def build_comment(entry: dict) -> str:
    ttype = (entry.get("type") or "").strip()
    expr = entry.get("expressions", {})
    desc = entry.get("description") or expr.get("tool_description") or ""
    desc = str(desc).strip().strip("'")

    geom = entry.get("geometry", {})
    extras = []
    re_corner = parse_number(geom.get("RE"))
    if re_corner:
        extras.append(f"cornerR={re_corner:.3f}")
    sig = parse_number(geom.get("SIG"))
    if sig is not None:
        extras.append(f"angle={sig:g}deg")
    nof = parse_number(geom.get("NOF"))
    if nof:
        extras.append(f"{int(nof)}F")

    comment = ttype
    if desc:
        comment = f"{comment} - {desc}" if comment else desc
    if extras:
        comment = f"{comment} ({', '.join(extras)})" if comment \
            else f"({', '.join(extras)})"
    return comment


def resolve_diameter(entry: dict) -> float:
    dc = parse_number(entry.get("geometry", {}).get("DC"))
    if dc is not None:
        return dc
    return parse_number(
        entry.get("expressions", {}).get("tool_diameter"), 0.0) or 0.0


def parse_pocket_map(spec: str | None) -> dict[int, int]:
    mapping: dict[int, int] = {}
    if not spec:
        return mapping
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"[Tt](\d+):(\d+)", part)
        if not m:
            raise ValueError(f"invalid pocket map item '{part}' "
                             "(expected 'T1:5')")
        mapping[int(m.group(1))] = int(m.group(2))
    return mapping


def read_existing_table(path: Path) -> dict[int, dict]:
    """T-number -> {pocket, diam, z} from an existing tool.tbl."""
    existing: dict[int, dict] = {}
    if not path.is_file():
        return existing
    for line in path.read_text(encoding="utf-8",
                               errors="replace").splitlines():
        m = TBL_LINE_RE.match(line)
        if m:
            existing[int(m.group(1))] = {
                "pocket": int(m.group(2)),
                "diam": float(m.group(3)),
                "z": float(m.group(4)),
            }
    return existing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Convert Fusion 360 Library.json to LinuxCNC tool.tbl "
                    "(preserves measured Z by default)")
    ap.add_argument("input", type=Path, help="Fusion 360 Library.json")
    ap.add_argument("-o", "--output", type=Path, default=Path("tool.tbl"))
    ap.add_argument("--z-source", choices=["preserve", "zero", "assembly",
                                           "value"], default="preserve",
                    help="Z offsets: 'preserve' existing tool.tbl Z (default;"
                         " new tools get 0), 'zero' all 0, 'assembly' gauge "
                         "length from the library, 'value' fixed --z-value")
    ap.add_argument("--z-value", type=float,
                    help="fixed Z for --z-source value")
    ap.add_argument("--pocket-fixed", type=int)
    ap.add_argument("--pocket-offset", type=int)
    ap.add_argument("--pocket-map", type=str)
    ap.add_argument("--sort", choices=["tool", "pocket"], default="tool")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the diff, write nothing")
    args = ap.parse_args(argv)

    if args.z_value is not None and args.z_source != "value":
        args.z_source = "value"
    if args.z_source == "value" and args.z_value is None:
        ap.error("--z-source value requires --z-value")

    lib = json.loads(args.input.read_text(encoding="utf-8"))
    entries = lib.get("data", [])
    if not entries:
        print("ERROR: no tools found under 'data' in the JSON")
        return 2

    pocket_map = parse_pocket_map(args.pocket_map)
    existing = read_existing_table(args.output)

    rows, seen, warnings = [], set(), []
    for entry in entries:
        tnum = parse_number(entry.get("post-process", {}).get("number"))
        if tnum is None:
            desc = (entry.get("description") or "?")
            warnings.append(f"tool without post-process number skipped "
                            f"({desc})")
            continue
        tnum = int(tnum)
        if tnum in seen:
            warnings.append(f"duplicate tool number T{tnum} skipped "
                            "(first one wins)")
            continue
        seen.add(tnum)

        if tnum in pocket_map:
            pocket = pocket_map[tnum]
        elif args.pocket_fixed is not None:
            pocket = args.pocket_fixed
        elif args.pocket_offset is not None:
            pocket = tnum + args.pocket_offset
        else:
            pocket = tnum

        if args.z_source == "zero":
            z = 0.0
        elif args.z_source == "value":
            z = args.z_value
        elif args.z_source == "assembly":
            z = parse_number(entry.get("geometry", {})
                             .get("assemblyGaugeLength"), 0.0) or 0.0
        else:  # preserve
            z = existing.get(tnum, {}).get("z", 0.0)

        rows.append({"tool": tnum, "pocket": pocket,
                     "diam": resolve_diameter(entry), "z": z,
                     "comment": build_comment(entry)})

    # pocket collision check
    pockets: dict[int, list[int]] = {}
    for r in rows:
        pockets.setdefault(r["pocket"], []).append(r["tool"])
    for p, tools in sorted(pockets.items()):
        if len(tools) > 1:
            warnings.append(f"pocket P{p} used by multiple tools: "
                            f"{', '.join('T%d' % t for t in tools)}")

    rows.sort(key=lambda r: r["tool" if args.sort == "tool" else "pocket"])

    # diff vs existing
    new_tools = {r["tool"] for r in rows}
    for t in sorted(new_tools - existing.keys()):
        print(f"  + T{t} (new)")
    for t in sorted(existing.keys() - new_tools):
        print(f"  - T{t} (no longer in library - REMOVED)")
    for r in rows:
        old = existing.get(r["tool"])
        if old:
            changes = []
            if abs(old["diam"] - r["diam"]) > 1e-4:
                changes.append(f"D {old['diam']:.3f} -> {r['diam']:.3f}")
            if abs(old["z"] - r["z"]) > 1e-4:
                changes.append(f"Z {old['z']:.3f} -> {r['z']:.3f}")
            if old["pocket"] != r["pocket"]:
                changes.append(f"P {old['pocket']} -> {r['pocket']}")
            if changes:
                print(f"  ~ T{r['tool']}: {', '.join(changes)}")
    for w in warnings:
        print(f"  WARNING: {w}")
    print(f"{len(rows)} tools "
          f"({len(new_tools - existing.keys())} new, "
          f"{len(existing.keys() - new_tools)} removed)")

    if args.dry_run:
        print("dry-run: nothing written")
        return 0

    header = [
        "; LinuxCNC tool table generated from Fusion 360 library",
        f"; Source: {args.input.name}",
        f"; Generated: {datetime.now().isoformat(timespec='seconds')}",
        "; Fields: T (tool), P (pocket), D (diameter mm), "
        "Z (length offset mm)",
        "; NOTE: Z offsets are PRESERVED from the previous table on re-sync"
        " (--z-source preserve).",
        "; ---------------------------------------------------------------"
        "--------------",
    ]
    # LET OP: 'T40' moet EEN token zijn (geen spatie erin) voor de
    # LinuxCNC-parser; uitlijnen doen we links van het token
    lines = header + [
        f"{'T%d' % r['tool']:<4s} {'P%d' % r['pocket']:<4s} "
        f"D{r['diam']:+.3f}  Z{r['z']:+.3f}   ; {r['comment']}".rstrip()
        for r in rows
    ]

    if args.output.is_file():
        shutil.copy2(args.output, args.output.with_suffix(
            args.output.suffix + ".bak"))
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written: {args.output} (backup: {args.output}.bak)")
    print("NB: reload the tool table in LinuxCNC (or restart) to pick "
          "up changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
