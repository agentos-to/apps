"""Regression tests for the Superpower lab-report parser.

Run standalone — no engine, no pytest:  python3 test_health.py

The bug this guards against: an early `import_lab_report` split each CSV
line on raw commas, ignoring quoting. Any analyte whose Name contained a
comma ("Iron, Total", "Testosterone, Total, MS", "Bilirubin, Direct")
had its columns shifted — value/unit/range landed in the wrong fields —
and analytes sharing a leading word collapsed onto ONE deduped id,
silently losing rows. The fix: parse with the `csv` module (honours
quoting) and derive the id/slug from the FULL analyte name.
"""

import os
import sys
import unittest

# Make the app module and the SDK importable without any env setup.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SDK = os.path.abspath(os.path.join(_HERE, "../../../../platform/sdk/python"))
for p in (_HERE, _SDK):
    if p not in sys.path:
        sys.path.insert(0, p)

import health  # noqa: E402


# A draw whose Names carry commas inside quoted fields, plus families that
# share a leading word — exactly the shape that collapsed under the bug.
QUOTED_COMMA_CSV = (
    "Name,Category,Value,Unit,Range\n"
    '"Iron, Total",Iron Panel,132,mcg/dL,50-180\n'
    '"Testosterone, Free",Testosterone Panel,78.4,pg/mL,46.0-224.0\n'
    '"Testosterone, Bioavailable",Testosterone Panel,161.2,ng/dL,110.0-575.0\n'
    '"Testosterone, Total, MS",Testosterone Panel,605,ng/dL,250-1100\n'
    '"Bilirubin, Total",CMP,0.5,mg/dL,0.2-1.2\n'
    '"Bilirubin, Direct",Bilirubin Fractionated,0.1,mg/dL,<=0.2\n'
    '"Bilirubin, Indirect",Bilirubin Fractionated,0.4,mg/dL,0.2-1.2\n'
)


class TestQuotedCommaParsing(unittest.TestCase):
    def setUp(self):
        self.rows = health._parse_superpower_csv(QUOTED_COMMA_CSV)
        self.slugs = [health._slug(r["analyte"]) for r in self.rows]

    def test_no_rows_dropped(self):
        # All seven rows survive — none collapsed, none merged.
        self.assertEqual(len(self.rows), 7)

    def test_ids_distinct(self):
        # Distinct analytes never collide on a shared leading word.
        self.assertEqual(len(set(self.slugs)), len(self.slugs))
        self.assertEqual(
            set(self.slugs),
            {
                "iron-total",
                "testosterone-free",
                "testosterone-bioavailable",
                "testosterone-total-ms",
                "bilirubin-total",
                "bilirubin-direct",
                "bilirubin-indirect",
            },
        )

    def test_quoted_name_kept_whole(self):
        # The comma stays inside the Name; it does not bleed into Category.
        by_slug = dict(zip(self.slugs, self.rows))
        self.assertEqual(by_slug["iron-total"]["analyte"], "Iron, Total")
        self.assertEqual(
            by_slug["testosterone-total-ms"]["analyte"],
            "Testosterone, Total, MS",
        )

    def test_columns_not_shifted(self):
        # Value parses as a number (no stray valueText), unit + range land
        # in their own fields — the columns did not slide.
        by_slug = dict(zip(self.slugs, self.rows))
        iron = by_slug["iron-total"]
        self.assertEqual(iron["value"], 132.0)
        self.assertIsNone(iron.get("valueText"))
        self.assertEqual(iron["unit"], "mcg/dL")
        self.assertEqual(iron["refLow"], 50.0)
        self.assertEqual(iron["refHigh"], 180.0)

    def test_testosterone_family_three_distinct(self):
        teston = [s for s in self.slugs if s.startswith("testosterone-")]
        self.assertEqual(len(teston), 3)

    def test_bilirubin_family_three_distinct(self):
        bili = [s for s in self.slugs if s.startswith("bilirubin-")]
        self.assertEqual(len(bili), 3)


class TestRangeParsing(unittest.TestCase):
    """Bounds extraction, incl. the two-char ASCII operators a report
    prints (">=", "<=") that the single-glyph regex used to miss."""

    def test_closed_range(self):
        r = health._parse_range("50-180")
        self.assertEqual((r["refLow"], r["refHigh"]), (50.0, 180.0))

    def test_strict_bounds(self):
        self.assertEqual(health._parse_range("<200")["refHigh"], 200.0)
        self.assertEqual(health._parse_range("> 60")["refLow"], 60.0)

    def test_inclusive_ascii_bounds(self):
        # "<=39" and ">=40" must yield numeric bounds, not just refText.
        hi = health._parse_range("<=39")
        self.assertEqual(hi["refHigh"], 39.0)
        self.assertNotIn("refLow", hi)
        lo = health._parse_range(">= 40")
        self.assertEqual(lo["refLow"], 40.0)
        self.assertNotIn("refHigh", lo)

    def test_inclusive_glyph_bounds(self):
        self.assertEqual(health._parse_range("≤ 0.2")["refHigh"], 0.2)
        self.assertEqual(health._parse_range("≥ 60")["refLow"], 60.0)

    def test_reftext_always_verbatim(self):
        self.assertEqual(health._parse_range("<=39")["refText"], "<=39")

    def test_freetext_no_bounds(self):
        r = health._parse_range("Not Estab.")
        self.assertEqual(r, {"refText": "Not Estab."})


if __name__ == "__main__":
    unittest.main(verbosity=2)
