#!/usr/bin/env python3
"""
Convert a Fusion 360 tool library JSON (Library.json) to a LinuxCNC tool table (tool.tbl).

Usage:
  python F360_toollib_convert.py Library.json -o tool.tbl \
      --z-source zero            # default, set Z=0.000 (touchoff workflow)
      # or: --z-source assembly  # use geometry.assemblyGaugeLength if present
      # or: --z-value -142.357   # force a constant Z for all tools

Optional pocket mapping:
  By default, P (pocket) == T (tool number).
  You can shift pockets with --pocket-offset N, or set a fixed pocket with --pocket-fixed N,
  or provide explicit mappings with --pocket-map "T1:5,T2:3,T40:1".

Notes:
- D is taken from geometry.DC (tool diameter) if available, otherwise from expressions.tool_diameter.
- Angle (geometry.SIG) and corner radius (geometry.RE) are emitted in comments for reference.
- The script is lenient with units like "3 mm" and will parse the number part.
- Output lines follow:  T{num}  P{pocket}  D{diameter_mm}  Z{length_mm}   ; comment
"""
from __future__ import annotations
import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

Number = Optional[float]

UNIT_NUMBER_RE = re.compile(r"^\s*([-+]?\d+(?:[\.,]\d+)?)")


def parse_number(val: Any, default: Number = None) -> Number:
    """Parse numbers that may come as float, int, or strings with units (e.g., '3 mm').
    Returns float or default if not parseable.
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val)
    m = UNIT_NUMBER_RE.match(s)
    if not m:
        return default
    num = m.group(1).replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return default


def parse_tool_number(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except Exception:
        try:
            return int(str(val).strip())
        except Exception:
            return None


def build_comment(entry: Dict[str, Any]) -> str:
    ttype = entry.get("type", "").strip()
    expr = entry.get("expressions", {})
    desc = entry.get("description") or expr.get("tool_description") or ""
    if isinstance(desc, str):
        d = desc.strip()
        if d.startswith("'") and d.endswith("'"):
            d = d[1:-1]
    else:
        d = ""

    geom = entry.get("geometry", {})
    re_corner = parse_number(geom.get("RE"))
    sig = parse_number(geom.get("SIG"))  # tool tip angle

    extras = []
    if re_corner is not None:
        extras.append(f"cornerR={re_corner:.3f}")
    if sig is not None:
        # angle is usually integral but format safe
        if abs(sig - round(sig)) < 1e-3:
            extras.append(f"angle={int(round(sig))}°")
        else:
            extras.append(f"angle={sig:.1f}°")

    comment = ttype
    if d:
        comment = f"{comment} – {d}" if comment else d
    if extras:
        comment = f"{comment} ({', '.join(extras)})" if comment else f"({', '.join(extras)})"
    return comment or ""


def resolve_diameter(entry: Dict[str, Any]) -> float:
    geom = entry.get("geometry", {})
    dc = parse_number(geom.get("DC"))
    if dc is not None:
        return dc
    expr = entry.get("expressions", {})
    td = parse_number(expr.get("tool_diameter"), 0.0)
    return float(td or 0.0)


def resolve_z(entry: Dict[str, Any], z_source: str, z_value_cli: Optional[float]) -> float:
    if z_source == "zero":
        return 0.0
    if z_source == "value":
        return float(z_value_cli or 0.0)
    if z_source == "assembly":
        geom = entry.get("geometry", {})
        z = parse_number(geom.get("assemblyGaugeLength"), 0.0)
        return float(z or 0.0)
    # fallback
    return 0.0


def parse_pocket_map(spec: Optional[str]) -> Dict[int, int]:
    mapping: Dict[int, int] = {}
    if not spec:
        return mapping
    for part in spec.split(","):
        if not part.strip():
            continue
        if ":" not in part:
            raise ValueError(f"Invalid pocket map item: '{part}'. Expected 'Txx:yy'.")
        left, right = part.split(":", 1)
        left = left.strip().upper()
        if not left.startswith("T"):
            raise ValueError(f"Invalid key '{left}'; must start with 'T'.")
        tnum = parse_tool_number(left[1:])
        pnum = parse_tool_number(right)
        if tnum is None or pnum is None:
            raise ValueError(f"Invalid mapping '{part}'.")
        mapping[tnum] = pnum
    return mapping


def decide_pocket(tnum: int, args: argparse.Namespace, pocket_map: Dict[int, int]) -> int:
    if tnum in pocket_map:
        return pocket_map[tnum]
    if args.pocket_fixed is not None:
        return int(args.pocket_fixed)
    if args.pocket_offset is not None:
        return int(tnum + args.pocket_offset)
    return int(tnum)  # default: pocket == tool


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert Fusion 360 Library.json to LinuxCNC tool.tbl")
    ap.add_argument("input", type=Path, help="Path to Fusion 360 Library.json")
    ap.add_argument("-o", "--output", type=Path, default=Path("tool.tbl"), help="Output tool.tbl path (default: ./tool.tbl)")

    zgrp = ap.add_mutually_exclusive_group()
    zgrp.add_argument("--z-source", choices=["zero", "assembly", "value"], default="zero",
                      help="Z length source: 'zero' (0.000, default), 'assembly' (geometry.assemblyGaugeLength), or 'value' (use --z-value)")
    zgrp.add_argument("--z-zero", action="store_true", help="Shorthand for --z-source zero")
    ap.add_argument("--z-value", type=float, help="When --z-source value, use this constant Z length (mm)")

    ap.add_argument("--pocket-fixed", type=int, help="Force the same pocket number for all tools")
    ap.add_argument("--pocket-offset", type=int, help="Pocket = tool + offset (e.g., offset 100 → T5 => P105)")
    ap.add_argument("--pocket-map", type=str, help="Explicit pocket map like 'T1:5,T2:3,T40:1'")

    ap.add_argument("--sort", choices=["tool", "pocket"], default="tool", help="Sort output by tool or pocket (default: tool)")

    args = ap.parse_args()

    if args.z_zero:
        args.z_source = "zero"

    # If the user provided --z-value explicitly, assume z-source=value unless overridden
    if args.z_value is not None and args.z_source != "value":
        args.z_source = "value"

    if args.z_source == "value" and args.z_value is None:
        ap.error("--z-source value requires --z-value")

    with args.input.open("r", encoding="utf-8") as f:
        lib = json.load(f)

    items = lib.get("data", [])

    pocket_map = parse_pocket_map(args.pocket_map)

    rows = []
    for entry in items:
        pp = entry.get("post-process", {})
        tnum = parse_tool_number(pp.get("number"))
        if tnum is None:
            continue
        diam = resolve_diameter(entry)
        zlen = resolve_z(entry, args.z_source, args.z_value)
        pocket = decide_pocket(tnum, args, pocket_map)
        comment = build_comment(entry)
        rows.append({
            "tool": tnum,
            "pocket": pocket,
            "diam": float(diam or 0.0),
            "z": float(zlen or 0.0),
            "comment": comment,
        })

    # sort
    key = (lambda r: r["tool"]) if args.sort == "tool" else (lambda r: r["pocket"])
    rows.sort(key=key)

    # header
    header = [
        "; LinuxCNC tool table generated from Fusion 360 library",
        f"; Source: {args.input.name}",
        f"; Generated: {datetime.now().isoformat(timespec='seconds')}",
        "; Fields: T (tool), P (pocket), D (diameter mm), Z (length offset mm)",
        "; NOTE: Z may be 0.000 when touchoff is used. Adjust if you measure/probe tool lengths.",
        "; -----------------------------------------------------------------------------",
    ]

    lines = header + [
        f"T{r['tool']:>3d}  P{r['pocket']:>3d}  D{r['diam']:.3f}  Z{r['z']:.3f}   ; {r['comment']}".rstrip()
        for r in rows
    ]

    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
