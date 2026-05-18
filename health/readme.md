---
id: health
name: Health Records
description: Parses on-disk lab reports into the health graph
color: "#5BA88A"
website: https://agentos.to

test:
  import_lab_report:
    params:
      path: "/Users/joe/Documents/Wellness/Health Records/Test Results/Blood Panels/2024-12-05 Superpower panel.csv"
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

**Scope.** This skill parses *structure* only. Anything that needs
clinical judgment — is a finding a condition or a procedure, which
SNOMED code applies — is the agent's job, not the skill's. That work
stays in the health project's interpretive extraction.
