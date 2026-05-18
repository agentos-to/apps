"""Health Records — parse on-disk lab reports into the health graph.

`import_lab_report` reads a dated blood-panel file, detects its format,
and returns health-observation rows wired to their panel, biomarker,
and source document. The engine ingests and dedups on deterministic
ids — re-importing a draw reconciles in place.

Structure-parsing only; no clinical judgment lives here (that is the
agent's job). New formats are added as `_parse_*` functions registered
in `_FORMATS` — the rest of the tool is format-agnostic.
"""

import csv
import os
import re

from agentos import returns, skill_error


# --- identity + value helpers ---------------------------------------------

def _slug(s: str) -> str:
    """A stable lowercase-hyphenated key for an analyte or lab name."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower().strip()).strip("-")


def _date_from_filename(path: str) -> str | None:
    """Pull a leading YYYY-MM-DD / YYYY.MM.DD date out of a filename."""
    m = re.match(r"(\d{4})[-.](\d{2})[-.](\d{2})", os.path.basename(path))
    return f"{m[1]}-{m[2]}-{m[3]}" if m else None


def _number(raw) -> float | None:
    """Coerce a value cell to a float, or None if it is not numeric."""
    try:
        return float(str(raw).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _parse_range(raw: str) -> dict:
    """Parse a printed reference range into refLow / refHigh / refText.

    Handles "100 - 199", "> 60", "< 5", "4.8 - 5.2" and free text.
    refText always keeps the verbatim string (the snapshot the report
    printed); numeric bounds are extracted when unambiguous.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    out = {"refText": raw}
    m = re.match(r"^([\d.]+)\s*[-–]\s*([\d.]+)$", raw)
    if m:
        out["refLow"], out["refHigh"] = float(m[1]), float(m[2])
    elif re.match(r"^[>≥]\s*[\d.]+$", raw):
        out["refLow"] = float(re.search(r"[\d.]+", raw)[0])
    elif re.match(r"^[<≤]\s*[\d.]+$", raw):
        out["refHigh"] = float(re.search(r"[\d.]+", raw)[0])
    return out


# --- format parsers -------------------------------------------------------
# Each returns a list of raw rows: {analyte, value|valueText, unit?,
# category?, refLow?, refHigh?, refText?}. Format-agnostic shaping into
# health-observation nodes happens in import_lab_report.

def _parse_superpower_csv(text: str) -> list[dict]:
    """Superpower panel export — columns Name,Category,Value,Unit,Range."""
    rows = []
    for r in csv.DictReader(text.splitlines()):
        analyte = (r.get("Name") or "").strip()
        if not analyte:
            continue
        raw_value = (r.get("Value") or "").strip()
        row = {
            "analyte": analyte,
            "category": (r.get("Category") or "").strip() or None,
            "unit": (r.get("Unit") or "").strip() or None,
            **_parse_range(r.get("Range") or ""),
        }
        num = _number(raw_value)
        row["value" if num is not None else "valueText"] = (
            num if num is not None else raw_value)
        rows.append(row)
    return rows


# detector(filename, first_line) -> bool   ·   parser(text) -> rows
_FORMATS = {
    "superpower-csv": (
        lambda fn, head: head.replace(" ", "").lower()
        == "name,category,value,unit,range",
        _parse_superpower_csv,
    ),
}


def _detect(path: str, text: str) -> str | None:
    head = text.splitlines()[0] if text else ""
    fn = os.path.basename(path)
    for fmt, (matches, _parser) in _FORMATS.items():
        if matches(fn, head):
            return fmt
    return None


def _infer_lab(path: str) -> str:
    """Lab name from the filename — words after the date, sans noise."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"^\d{4}[-.]\d{2}[-.]\d{2}\s*", "", stem)
    stem = re.sub(r"\b(panel|results?|bloodwork|labs?)\b", "", stem,
                  flags=re.I)
    return stem.strip() or "lab"


# --- the tool -------------------------------------------------------------

@returns("health-observation[]")
async def import_lab_report(path: str, date: str = None, lab: str = None,
                            **params) -> list[dict]:
    """Parse a dated lab-panel file into biomarker observations.

    Autodetects the file format, then returns one health-observation
    per analyte — each wired to its panel (fromPanel), its biomarker
    definition (measures), and the source file (document). Ids are
    deterministic, so re-importing the same draw reconciles in place
    instead of duplicating.

    Args:
        path: Absolute path to the lab report file.
        date: Draw date YYYY-MM-DD. Defaults to the date in the filename.
        lab: Issuing lab name. Defaults to inference from the filename.
    """
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        return skill_error(f"Not a file: {path}")

    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()

    fmt = _detect(path, text)
    if not fmt:
        return skill_error(
            f"Unrecognized lab-report format: {os.path.basename(path)}")

    draw_date = date or _date_from_filename(path)
    if not draw_date:
        return skill_error(
            "No draw date — pass date=YYYY-MM-DD (none in filename).")

    lab = lab or _infer_lab(path)
    lab_slug = _slug(lab)
    rows = _FORMATS[fmt][1](text)
    if not rows:
        return skill_error(
            f"No analyte rows parsed from {os.path.basename(path)}")

    # The source file and the panel both ride as 1-deep relations on each
    # observation (the engine extracts nested relations one level deep —
    # a relation nested under the panel would not become an edge). The
    # engine dedups both to a single node across all 72 observations.
    document = {
        "id": path,
        "name": os.path.basename(path),
        "filename": os.path.basename(path),
        "path": path,
        "format": os.path.splitext(path)[1].lstrip(".").upper(),
    }
    panel = {
        "id": f"panel:{lab_slug}:{draw_date}",
        "name": f"{lab} panel {draw_date}",
        "effectiveDate": draw_date,
        "panelCode": lab_slug,
    }

    observations = []
    for r in rows:
        a_slug = _slug(r["analyte"])
        biomarker = {
            "id": f"biomarker:{a_slug}",
            "name": r["analyte"],
            "measure": a_slug,
        }
        if r.get("category"):
            biomarker["category"] = r["category"]
        ob = {
            "id": f"obs:{lab_slug}:{draw_date}:{a_slug}",
            "name": r["analyte"],
            "effectiveDate": draw_date,
            "fromPanel": panel,
            "measures": biomarker,
            "document": document,
        }
        for k in ("value", "valueText", "unit", "refLow", "refHigh",
                  "refText"):
            if r.get(k) is not None:
                ob[k] = r[k]
        observations.append(ob)

    return observations
