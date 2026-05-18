---
id: health
capabilities:
  - http
name: Health Records
description: Parses on-disk lab reports into the health graph
color: "#5BA88A"
website: https://agentos.to

test:
  import_lab_report:
    params:
      path: "/Users/joe/Documents/Wellness/Health Records/Test Results/Blood Panels/2024-12-05 Superpower panel.csv"
  loinc_search:
    params:
      component: Glucose
---

# Health Records

Turns on-disk health documents into AgentOS health-graph nodes.

`import_lab_report` reads a dated blood-panel file, autodetects its
format, and returns one `health-observation` per analyte — each wired
to its panel (`fromPanel`), its biomarker definition (`measures`), and
the source file (`document`). The engine ingests and dedups on
deterministic ids, so re-importing the same draw reconciles in place
instead of duplicating.

**Formats**

| Format | Detect | Status |
|---|---|---|
| Superpower panel CSV | header `Name,Category,Value,Unit,Range` | ✅ |
| PDF lab reports (Quest, LabCorp, One Medical, older panels) | `pdftotext` | planned |

`loinc_search` resolves a biomarker to its LOINC code against the
public `tx.fhir.org` terminology server — live, no auth, nothing
hardcoded. There is no reliable *mechanical* name→code search (the
everyday code drowns under hundreds of hits), so the split is: the
**agent** translates the report's wording into LOINC axis terms
("Total Cholesterol" → component `Cholesterol`, specimen `Ser/Plas`),
and the **skill** runs the precise six-axis query. The chosen
`loincCode` is then stamped on the biomarker node — each user's graph
accumulates their own resolved biomarkers; nothing personal lives in
the skill.

**Scope.** This skill parses *structure* and runs *precise lookups* —
it holds no biomarker knowledge and makes no clinical judgement.
Translating wording to LOINC terms, and picking among unit variants,
is the agent's job. Interpretive work (condition vs procedure, SNOMED)
stays in the health project's extraction.
