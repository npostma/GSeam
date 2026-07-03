"""Unit tests for gseam.py — run with:  python3 -m unittest discover tests"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gseam  # noqa: E402


def fusion_file(name="SIMPLE1", tool=3, zmin=-5.0, units="G21",
                wcs="G54", with_g90=True):
    """Synthetic minimal Fusion-360-style .ngc content."""
    setup = "G90 G94 G17 G91.1" if with_g90 else "G94 G17 G91.1"
    return f"""%
({name})
(Machine)
(  vendor LinuxCNC)
(T{tool} D=3. CR=0. - ZMIN={zmin} - flat end mill)
N10 {setup}
N15 {units}
(When using Fusion for personal use, the feedrate of rapid)
(moves is reduced to match the feedrate of cutting moves,)
N20 G53 G0 Z0.
(2D Pocket1)
N25 T{tool} M6
N30 S10000 M3
N40 {wcs}
N50 G0 X10. Y20.
N60 G1 Z{zmin} F100.
N70 M5
N80 G53 G0 Z0.
N90 M30
%
"""


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def write(self, name, content):
        p = self.dir / name
        p.write_text(content, encoding="utf-8")
        return p


class TestHelpers(unittest.TestCase):
    def test_strip_n(self):
        self.assertEqual(gseam.strip_n("N120 G1 X5"), "G1 X5")
        self.assertEqual(gseam.strip_n("G1 X5"), "G1 X5")
        self.assertEqual(gseam.strip_n("  n10 G0"), "G0")

    def test_is_comment(self):
        self.assertTrue(gseam.is_comment("(iets)"))
        self.assertTrue(gseam.is_comment("; iets"))
        self.assertFalse(gseam.is_comment("G1 X0 (inline)"))

    def test_comment_filter(self):
        keep = ["(2D Pocket1)", "(T3 D=3. CR=0.)", "(Drill5)",
                "(SPOTDRILLING)", "(Face2)", "(TOOL something)"]
        drop = ["(Machine)", "(  vendor LinuxCNC)", "(SIMPLE1)",
                "(When using Fusion for personal use, x)"]
        for c in keep:
            self.assertTrue(gseam.comment_filter(c, False), c)
        for c in drop:
            self.assertFalse(gseam.comment_filter(c, False), c)
        for c in keep + drop:
            self.assertTrue(gseam.comment_filter(c, True), c)


class TestParseFile(TempDirTest):
    def test_basic_structure(self):
        p = self.write("a.ngc", fusion_file())
        pf = gseam.parse_file(p, allow_inch=False)
        self.assertEqual(pf.errors, [])
        self.assertEqual(pf.units, "G21")
        self.assertTrue(pf.has_g90)
        self.assertEqual(pf.wcs, {"G54"})
        self.assertEqual(len(pf.operations), 1)
        self.assertEqual(pf.operations[0].tool, 3)
        # preamble: alleen setup-code, geen comments
        self.assertIn("G21", pf.preamble)
        self.assertIn("G53 G0 Z0.", pf.preamble)
        self.assertFalse(any(gseam.is_comment(s) for s in pf.preamble))
        # tool-doc comment verzameld
        self.assertEqual(len(pf.tool_comments), 1)
        self.assertIn("ZMIN=-5.0", pf.tool_comments[0])
        # op-comment zit bij de operatie, footer (M30/%) is weggeknipt
        op_lines = pf.operations[0].lines
        self.assertEqual(op_lines[0], "(2D Pocket1)")
        self.assertFalse(any("M30" in s for s in op_lines))

    def test_g20_is_error(self):
        p = self.write("inch.ngc", fusion_file(units="G20"))
        pf = gseam.parse_file(p, allow_inch=False)
        self.assertTrue(any("G20" in e for e in pf.errors))
        pf2 = gseam.parse_file(p, allow_inch=True)
        self.assertEqual(pf2.errors, [])
        self.assertEqual(pf2.units, "G20")

    def test_no_toolchange_is_error(self):
        p = self.write("x.ngc", "%\nG21 G90\nG1 X1 F10\nM30\n%\n")
        pf = gseam.parse_file(p, allow_inch=False)
        self.assertTrue(any("toolchange" in e for e in pf.errors))

    def test_missing_g90_warns(self):
        p = self.write("w.ngc", fusion_file(with_g90=False))
        pf = gseam.parse_file(p, allow_inch=False)
        self.assertTrue(any("G90" in w for w in pf.warnings))


class TestCrossChecks(TempDirTest):
    def test_unit_mismatch(self):
        a = gseam.parse_file(self.write("a.ngc", fusion_file()), True)
        b = gseam.parse_file(self.write("b.ngc", fusion_file(units="G20")),
                             True)
        errors, _ = gseam.cross_file_checks([a, b])
        self.assertTrue(any("unit mismatch" in e for e in errors))

    def test_mixed_wcs_warns(self):
        a = gseam.parse_file(self.write("a.ngc", fusion_file()), False)
        b = gseam.parse_file(self.write("b.ngc", fusion_file(wcs="G55")),
                             False)
        errors, warnings = gseam.cross_file_checks([a, b])
        self.assertEqual(errors, [])
        self.assertTrue(any("work offsets" in w for w in warnings))

    def test_tool_check(self):
        a = gseam.parse_file(self.write("a.ngc", fusion_file(tool=99)),
                             False)
        problems = gseam.tool_check([a], {1, 2, 3})
        self.assertEqual(problems, ["tool T99 not in tool table"])
        self.assertEqual(gseam.tool_check([a], {99}), [])


class TestMerge(TempDirTest):
    def parsed(self, *contents):
        return [gseam.parse_file(self.write(f"f{i}.ngc", c), False)
                for i, c in enumerate(contents)]

    def test_same_tool_skipped(self):
        out, stats = gseam.merge(
            self.parsed(fusion_file(), fusion_file(name="SIMPLE2")),
            "out.ngc", insert_toolchange_call=False, skip_same_tool=True,
            keep_all_comments=False)
        self.assertEqual(stats["toolchanges"], 1)
        self.assertEqual(stats["skipped_toolchanges"], 1)
        self.assertEqual(sum("T3 M6" in s for s in out), 1)

    def test_different_tools_kept(self):
        out, stats = gseam.merge(
            self.parsed(fusion_file(tool=3), fusion_file(tool=5)),
            "out.ngc", insert_toolchange_call=True, skip_same_tool=True,
            keep_all_comments=False)
        self.assertEqual(stats["toolchanges"], 2)
        self.assertEqual(stats["toolchange_calls"], 2)
        calls = [i for i, s in enumerate(out) if s == "O <toolchange> call"]
        self.assertEqual(len(calls), 2)
        # de call staat direct voor de toolwissel
        self.assertRegex(out[calls[0] + 1], r"^T3 M6")

    def test_single_footer_and_header(self):
        # NB: verschillende zmin -> verschillende tool-comments; identieke
        # tool-comments worden bewust gededupliceerd
        out, _ = gseam.merge(
            self.parsed(fusion_file(), fusion_file(name="B", zmin=-9.0)),
            "out.ngc", insert_toolchange_call=False, skip_same_tool=True,
            keep_all_comments=False)
        self.assertEqual(sum(s == "M30" for s in out), 1)
        self.assertEqual(out[-1], "M30")
        # setup maar 1x (dedup): G21 komt exact 1x voor
        self.assertEqual(sum(s == "G21" for s in out), 1)
        # tool-doc comments van BEIDE bestanden in de kop
        self.assertEqual(sum("ZMIN" in s for s in out), 2)
        # nag-comments weg
        self.assertFalse(any("personal use" in s.lower() for s in out))


class TestRenumber(unittest.TestCase):
    def test_renumber_skips_comments_and_owords(self):
        lines = ["(comment)", "G21", "O <toolchange> call", "T3 M6", ""]
        out = gseam.renumber(lines, 10)
        self.assertEqual(out[0], "(comment)")
        self.assertEqual(out[1], "N10 G21")
        self.assertEqual(out[2], "O <toolchange> call")
        self.assertEqual(out[3], "N20 T3 M6")
        self.assertEqual(out[4], "")


class TestExtents(unittest.TestCase):
    def test_extents_and_g53_skip(self):
        ext = gseam.Extents()
        ext.feed("G0 X10. Y20.", tool=3)
        ext.feed("G1 Z-5. F100.", tool=3)
        ext.feed("G1 Z-9. F100.", tool=5)
        ext.feed("G53 G0 Z0.", tool=5)      # machine coords: negeren
        self.assertEqual(ext.mins["Z"], -9.0)
        self.assertEqual(ext.maxs["X"], 10.0)
        self.assertEqual(ext.zmin_per_tool, {3: -5.0, 5: -9.0})


class TestFilesAndTable(TempDirTest):
    def test_numbered_sort(self):
        for n in ("op2.ngc", "op10.ngc", "op1.ngc", "nonumber.ngc"):
            self.write(n, "x")
        files = gseam.numbered_ngc_files(self.dir)
        self.assertEqual([f.name for f in files],
                         ["op1.ngc", "op2.ngc", "op10.ngc"])

    def test_read_tool_table(self):
        p = self.write("tool.tbl",
                       "; kop\nT1 P1 D+3.000 Z+0.000 ; x\n"
                       "T40  P40  D+6.000  Z-1.200\n")
        self.assertEqual(gseam.read_tool_table(p), {1, 40})


class TestMainEndToEnd(TempDirTest):
    def run_main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = gseam.main(argv)
        return code, buf.getvalue()

    def test_merge_end_to_end(self):
        a = self.write("op1.ngc", fusion_file())
        b = self.write("op2.ngc", fusion_file(name="B", tool=5, zmin=-9.0))
        tbl = self.write("tool.tbl", "T3 P3 D+3.000 Z0\nT5 P5 D+6.000 Z0\n")
        out = self.dir / "merged.ngc"
        code, text = self.run_main(
            [str(a), str(b), str(out), "--tool-table", str(tbl)])
        self.assertEqual(code, 0, text)
        content = out.read_text().splitlines()
        self.assertEqual(content[0], "%")
        self.assertEqual(content[-1], "%")
        self.assertEqual(sum("M30" in s for s in content), 1)
        # geen dubbele N-nummers
        ns = [s.split()[0] for s in content if s.startswith("N")]
        self.assertEqual(len(ns), len(set(ns)))
        self.assertIn("Zmin T5: -9.000", text)

    def test_check_mode_and_exit_codes(self):
        good = self.write("g1.ngc", fusion_file())
        code, text = self.run_main(["--check", str(good),
                                    "--no-tool-check"])
        self.assertEqual(code, 0)
        self.assertIn("check OK", text)

        inch = self.write("inch1.ngc", fusion_file(units="G20"))
        code, text = self.run_main(["--check", str(inch),
                                    "--no-tool-check"])
        self.assertEqual(code, 1)
        self.assertIn("G20", text)

    def test_unknown_tool_fails_merge(self):
        a = self.write("op1.ngc", fusion_file(tool=99))
        tbl = self.write("tool.tbl", "T3 P3 D+3.000 Z0\n")
        out = self.dir / "m.ngc"
        code, text = self.run_main(
            [str(a), str(out), "--tool-table", str(tbl)])
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())
        self.assertIn("T99", text)

    def test_dry_run_writes_nothing(self):
        a = self.write("op1.ngc", fusion_file())
        out = self.dir / "m.ngc"
        code, _ = self.run_main(["--dry-run", str(a), str(out),
                                 "--no-tool-check"])
        self.assertEqual(code, 0)
        self.assertFalse(out.exists())

    def test_directory_mode(self):
        (self.dir / "ops").mkdir()
        self.write("ops/deel1.ngc", fusion_file())
        self.write("ops/deel2.ngc", fusion_file(name="B"))
        out = self.dir / "m.ngc"
        code, text = self.run_main([str(self.dir / "ops"), str(out),
                                    "--no-tool-check"])
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())
        self.assertLess(text.index("deel1.ngc"), text.index("deel2.ngc"))


if __name__ == "__main__":
    unittest.main()
