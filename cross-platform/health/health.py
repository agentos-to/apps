"""Health Records — parse on-disk lab reports into the health graph.

`import_lab_report` reads a dated blood-panel file, detects its format,
and returns measure rows wired to their panel, biomarker, and source
document. The engine ingests and dedups on deterministic
ids — re-importing a draw reconciles in place.

Structure-parsing only; no clinical judgment lives here (that is the
agent's job). New formats are added as `_parse_*` functions registered
in `_FORMATS` — the rest of the tool is format-agnostic.
"""

import csv
import json
import os
import re

from agentos import client, returns, app_error

# tx.fhir.org — HL7's public FHIR terminology server, LOINC loaded, no
# auth. Reached directly; `services: [http]` in the readme grants it.
LOINC_EXPAND = "https://tx.fhir.org/r4/ValueSet/$expand"


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

    Handles "100 - 199", "> 60", ">= 40", "< 5", "<= 39", "4.8 - 5.2"
    and free text. Both the ASCII two-char operators (">=", "<=") and the
    single glyphs ("≥", "≤") count as bounds. refText always keeps the
    verbatim string (the snapshot the report printed); numeric bounds are
    extracted when unambiguous.
    """
    raw = (raw or "").strip()
    if not raw:
        return {}
    out = {"refText": raw}
    m = re.match(r"^([\d.]+)\s*[-–]\s*([\d.]+)$", raw)
    if m:
        out["refLow"], out["refHigh"] = float(m[1]), float(m[2])
    elif re.match(r"^[>≥]=?\s*[\d.]+$", raw):
        out["refLow"] = float(re.search(r"[\d.]+", raw)[0])
    elif re.match(r"^[<≤]=?\s*[\d.]+$", raw):
        out["refHigh"] = float(re.search(r"[\d.]+", raw)[0])
    return out


# --- format parsers -------------------------------------------------------
# Each returns a list of raw rows: {analyte, value|valueText, unit?,
# category?, refLow?, refHigh?, refText?}. Format-agnostic shaping into
# measure nodes happens in import_lab_report.

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

@returns("measure[]")
async def import_lab_report(path: str, date: str = None, lab: str = None,
                            **params) -> list[dict]:
    """Parse a dated lab-panel file into biomarker measures.

    Autodetects the file format, then returns one measure per analyte
    — each wired to its panel (fromPanel), its biomarker
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
        return app_error(f"Not a file: {path}")

    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()

    fmt = _detect(path, text)
    if not fmt:
        return app_error(
            f"Unrecognized lab-report format: {os.path.basename(path)}")

    draw_date = date or _date_from_filename(path)
    if not draw_date:
        return app_error(
            "No draw date — pass date=YYYY-MM-DD (none in filename).")

    lab = lab or _infer_lab(path)
    lab_slug = _slug(lab)
    rows = _FORMATS[fmt][1](text)
    if not rows:
        return app_error(
            f"No analyte rows parsed from {os.path.basename(path)}")

    # The source file and the panel both ride as 1-deep relations on each
    # measure (the engine extracts nested relations one level deep —
    # a relation nested under the panel would not become an edge). The
    # engine dedups both to a single node across all measures.
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

    measures = []
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
            "at": draw_date,
            "fromPanel": panel,
            "measures": biomarker,
            "document": document,
        }
        # The unit is not its own field — it rides on each numeric val
        # (the memex stores a unit alongside every val). value / refLow /
        # refHigh carry it via the {value, unit} envelope; valueText and
        # refText are unitless strings.
        unit = r.get("unit")
        for k in ("value", "refLow", "refHigh"):
            v = r.get(k)
            if v is not None:
                ob[k] = {"value": v, "unit": unit} if unit else v
        for k in ("valueText", "refText"):
            if r.get(k) is not None:
                ob[k] = r[k]
        measures.append(ob)

    return measures


# --- LOINC resolution -----------------------------------------------------
# A lab report names a biomarker in free wording ("Total Cholesterol",
# "TSH"); the universal identity is its LOINC code. There is no reliable
# mechanical name→code search — confirmed against the NLM API and the
# tx.fhir.org text filter, both of which bury the everyday code under
# hundreds of irrelevant hits. But the *precise* query — LOINC's six
# axes — is exact. So the split is: the agent translates the report
# wording into LOINC axis terms (its medical knowledge: "Total
# Cholesterol" → component "Cholesterol", specimen "Ser/Plas"), and this
# tool runs the precise query against the public terminology server.
# No biomarker knowledge is hardcoded here — it works for any biomarker,
# any user.

def _loinc_valueset(component: str, system: str, scale: str) -> dict:
    """An inline LOINC ValueSet filtered to one component (+ optional
    system / scale), active codes only."""
    flt = [{"property": "COMPONENT", "op": "=", "value": component},
           {"property": "STATUS", "op": "=", "value": "ACTIVE"}]
    if system:
        flt.append({"property": "SYSTEM", "op": "=", "value": system})
    if scale:
        flt.append({"property": "SCALE_TYP", "op": "=", "value": scale})
    return {"resourceType": "ValueSet",
            "compose": {"include": [
                {"system": "http://loinc.org", "filter": flt}]}}


@returns({"component": "string", "narrowed": "boolean", "matches": "array"})
async def loinc_search(component: str, specimen: str = "Ser/Plas",
                       scale: str = "Qn", **params) -> dict:
    """Resolve a biomarker to its LOINC code(s) via the public
    tx.fhir.org FHIR terminology server (no auth, live).

    Supply LOINC *axis* terms, not the lab report's wording — translate
    first: "Total Cholesterol" → component "Cholesterol", specimen
    "Ser/Plas"; "TSH" → component "Thyrotropin"; "HbA1c" → component
    "Hemoglobin A1c/Hemoglobin.total", specimen "Bld". Returns the
    active LOINC codes for that component — usually 1-3, differing by
    unit (Mass/volume vs Moles/volume); pick the one matching the
    report's unit.

    If the precise query finds nothing, it retries with the component
    alone (`narrowed: false`) so you can see what LOINC actually calls
    it and adjust.

    Gotcha — COMPONENT is an *exact* match on LOINC's component axis,
    which is frequently NOT the everyday name. An empty result means
    the component term is wrong, not that no code exists. Known
    translations that bite (found unifying a real biomarker set):
      · HbA1c %        → "Hemoglobin A1c/Hemoglobin.total"
      · MCH / MCHC     → component "Hemoglobin", specimen "RBC"
      · MPV            → component "Platelet"
      · Vitamin B12    → "Cobalamins"
      · Lipoprotein(a) → "Lipoprotein (little a)"
      · HDL / LDL      → "Cholesterol.in HDL" / "Cholesterol.in LDL"
    A handful resist every component string on tx.fhir.org — Hematocrit
    (4544-3), MCV (787-2), RDW (788-0), and the "/100 leukocytes"
    differential percentages (Neutrophils 770-8, Lymphocytes 736-9,
    Monocytes 5905-5, Eosinophils 713-8, Basophils 706-2). For those
    the agent supplies the code from medical knowledge directly.

    Args:
        component: LOINC Component term — "Glucose", "Cholesterol",
                   "Hemoglobin A1c", "Ferritin", "Thyrotropin".
        specimen:  LOINC System token — "Ser/Plas" (default; serum or
                   plasma), "Bld" (whole blood — CBC, HbA1c), "Urine".
        scale:     LOINC Scale — "Qn" quantitative (default), "Ord", "Nom".
    """
    async def _expand(vs: dict) -> list:
        resp = await client.post(
            LOINC_EXPAND, params={"count": 40}, json=vs,
            headers={"Content-Type": "application/fhir+json",
                     "Accept": "application/fhir+json"})
        # The server content-negotiates on Accept — without it tx.fhir.org
        # serves an HTML page. Parse the JSON body ourselves.
        body = json.loads(resp.get("body") or "{}")
        contains = body.get("expansion", {}).get("contains", [])
        return [{"loincCode": c["code"], "display": c.get("display", "")}
                for c in contains]

    matches = await _expand(_loinc_valueset(component, specimen, scale))
    narrowed = True
    if not matches:
        # component term may be right but specimen/scale off — broaden
        matches = await _expand(_loinc_valueset(component, None, None))
        narrowed = False

    return {"component": component, "narrowed": narrowed, "matches": matches}
