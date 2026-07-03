"""Unit tests for f360_toollib_convert.py — run with:
python3 -m unittest discover tests"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import f360_toollib_convert as conv  # noqa: E402


def tool_entry(number, diam=3.0, ttype="flat end mill", desc="3x6x8",
               flutes=4, **geom_extra):
    geom = {"DC": diam, "NOF": flutes}
    geom.update(geom_extra)
    return {
        "type": ttype,
        "description": desc,
        "expressions": {"tool_diameter": f"{diam} mm"},
        "geometry": geom,
        "post-process": {"number": number},
    }


def library(*entries):
    return {"data": list(entries), "version": 18}


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def write_lib(self, lib):
        p = self.dir / "Library.json"
        p.write_text(json.dumps(lib), encoding="utf-8")
        return p

    def run_main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = conv.main(argv)
        return code, buf.getvalue()


class TestParsing(unittest.TestCase):
    def test_parse_number(self):
        self.assertEqual(conv.parse_number(3), 3.0)
        self.assertEqual(conv.parse_number("3 mm"), 3.0)
        self.assertEqual(conv.parse_number("3,5 mm"), 3.5)
        self.assertEqual(conv.parse_number("-1.25"), -1.25)
        self.assertIsNone(conv.parse_number(None))
        self.assertIsNone(conv.parse_number("not a number"))
        self.assertEqual(conv.parse_number("x", 7.0), 7.0)

    def test_parse_pocket_map(self):
        self.assertEqual(conv.parse_pocket_map("T1:5,T2:3"), {1: 5, 2: 3})
        self.assertEqual(conv.parse_pocket_map(None), {})
        with self.assertRaises(ValueError):
            conv.parse_pocket_map("1:5")
        with self.assertRaises(ValueError):
            conv.parse_pocket_map("T1-5")

    def test_resolve_diameter_fallback(self):
        e = tool_entry(1, diam=6.0)
        self.assertEqual(conv.resolve_diameter(e), 6.0)
        del e["geometry"]["DC"]
        self.assertEqual(conv.resolve_diameter(e), 6.0)  # from expressions fallback

    def test_build_comment(self):
        c = conv.build_comment(tool_entry(1, ttype="spot drill",
                                          desc="90 degrees", SIG=90))
        self.assertIn("spot drill", c)
        self.assertIn("90 degrees", c)
        self.assertIn("angle=90deg", c)
        self.assertIn("4F", c)


class TestExistingTable(TempDirTest):
    def test_reads_new_and_old_spaced_format(self):
        p = self.dir / "tool.tbl"
        p.write_text("; header\n"
                     "T40  P40  D+3.000  Z-12.345   ; new format\n"
                     "T  1  P  1  D+3.000  Z+0.500   ; old format\n",
                     encoding="utf-8")
        t = conv.read_existing_table(p)
        self.assertEqual(t[40]["z"], -12.345)
        self.assertEqual(t[1]["z"], 0.5)

    def test_missing_file_is_empty(self):
        self.assertEqual(
            conv.read_existing_table(self.dir / "nope.tbl"), {})


class TestMainEndToEnd(TempDirTest):
    def test_write_format_single_tokens(self):
        lib = self.write_lib(library(tool_entry(40), tool_entry(1)))
        out = self.dir / "tool.tbl"
        code, _ = self.run_main([str(lib), "-o", str(out)])
        self.assertEqual(code, 0)
        lines = out.read_text().splitlines()
        rows = [ln for ln in lines if ln.startswith("T")]
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0].startswith("T1 "))    # sorted by tool
        self.assertTrue(rows[1].startswith("T40 "))   # a SINGLE token, never 'T 40'
        self.assertIn("D+3.000", rows[0])
        self.assertIn("Z+0.000", rows[0])

    def test_z_preserved_on_resync(self):
        lib = self.write_lib(library(tool_entry(40), tool_entry(41)))
        out = self.dir / "tool.tbl"
        self.run_main([str(lib), "-o", str(out)])
        # simulate a measured Z
        txt = out.read_text().replace(
            "T40  P40  D+3.000  Z+0.000", "T40  P40  D+3.000  Z-12.345")
        out.write_text(txt, encoding="utf-8")
        code, text = self.run_main([str(lib), "-o", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("Z-12.345", out.read_text())      # preserved
        self.assertIn("Z+0.000", out.read_text())       # T41 still plain 0

    def test_z_source_zero_resets(self):
        lib = self.write_lib(library(tool_entry(40)))
        out = self.dir / "tool.tbl"
        self.run_main([str(lib), "-o", str(out)])
        txt = out.read_text().replace("Z+0.000", "Z-9.999")
        out.write_text(txt, encoding="utf-8")
        code, text = self.run_main([str(lib), "-o", str(out),
                                    "--z-source", "zero"])
        self.assertEqual(code, 0)
        self.assertNotIn("Z-9.999", out.read_text())
        self.assertIn("Z -9.999 -> 0.000", text.replace("Z -", "Z -"))

    def test_diff_output(self):
        old = self.dir / "tool.tbl"
        old.write_text("T7 P7 D+2.000 Z+0.000 ; to be removed\n",
                       encoding="utf-8")
        lib = self.write_lib(library(tool_entry(40)))
        code, text = self.run_main([str(lib), "-o", str(old), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("+ T40 (new)", text)
        self.assertIn("- T7", text)
        self.assertIn("dry-run", text)
        # dry-run: file untouched
        self.assertIn("T7", old.read_text())

    def test_backup_created(self):
        lib = self.write_lib(library(tool_entry(40)))
        out = self.dir / "tool.tbl"
        out.write_text("T40 P40 D+3.000 Z+0.000\n", encoding="utf-8")
        self.run_main([str(lib), "-o", str(out)])
        self.assertTrue((self.dir / "tool.tbl.bak").exists())

    def test_duplicate_tool_number_skipped(self):
        lib = self.write_lib(library(tool_entry(40, desc="first"),
                                     tool_entry(40, desc="duplicate")))
        out = self.dir / "tool.tbl"
        code, text = self.run_main([str(lib), "-o", str(out)])
        self.assertEqual(code, 0)
        rows = [ln for ln in out.read_text().splitlines()
                if ln.startswith("T40")]
        self.assertEqual(len(rows), 1)
        self.assertIn("first", rows[0])
        self.assertIn("duplicate tool number T40", text)

    def test_pocket_collision_warns(self):
        lib = self.write_lib(library(tool_entry(1), tool_entry(2)))
        out = self.dir / "tool.tbl"
        code, text = self.run_main([str(lib), "-o", str(out),
                                    "--pocket-fixed", "9"])
        self.assertEqual(code, 0)
        self.assertIn("pocket P9 used by multiple tools", text)

    def test_pocket_offset_and_map(self):
        lib = self.write_lib(library(tool_entry(1), tool_entry(2)))
        out = self.dir / "tool.tbl"
        self.run_main([str(lib), "-o", str(out), "--pocket-offset", "100",
                       "--pocket-map", "T2:7"])
        txt = out.read_text()
        self.assertIn("T1   P101", txt)   # offset
        self.assertIn("T2   P7", txt)     # explicit map wins

    def test_tool_without_number_skipped(self):
        e = tool_entry(0)
        del e["post-process"]["number"]
        lib = self.write_lib(library(e, tool_entry(40)))
        out = self.dir / "tool.tbl"
        code, text = self.run_main([str(lib), "-o", str(out)])
        self.assertEqual(code, 0)
        self.assertIn("without post-process number", text)
        rows = [ln for ln in out.read_text().splitlines()
                if ln.startswith("T")]
        self.assertEqual(len(rows), 1)

    def test_empty_library_errors(self):
        lib = self.write_lib({"data": [], "version": 18})
        code, text = self.run_main([str(lib), "-o",
                                    str(self.dir / "t.tbl")])
        self.assertEqual(code, 2)
        self.assertIn("no tools", text)


if __name__ == "__main__":
    unittest.main()
