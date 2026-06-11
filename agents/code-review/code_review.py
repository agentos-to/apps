"""
code_review.py — Evaluate code changes against project principles and architecture.

Scores every diff against engine principles, design principles, and
skill SDK patterns. Uses llm.oneshot() for a single evaluation pass —
no tools, no agent loop.
"""

import json
import yaml
from pathlib import Path

from agentos import llm, returns, shell, test, timeout


# ---------------------------------------------------------------------------
# Knowledge loading
# ---------------------------------------------------------------------------

# Absolute paths — skill may run from any cwd
WORKSPACE_ROOT = Path.home() / "dev" / "agentos"
CORE_ROOT = WORKSPACE_ROOT / "core"
DOCS_ROOT = WORKSPACE_ROOT / "platform" / "docs"


def _load_principles() -> tuple[str, str, str]:
    """Load the three knowledge sources the reviewer depends on.

    Fails loudly if any file is missing — these are the entire basis for
    scoring, and silent fallback (the old behavior) let a stale GUIDE.md
    path run unreviewed for an unknown window.
    """
    engine_path = CORE_ROOT / "principles.md"
    design_path = (
        DOCS_ROOT / "src" / "content" / "docs" / "architecture" / "design-principles.md"
    )
    guide_path = DOCS_ROOT / "src" / "content" / "docs" / "skills" / "overview.md"

    missing = [
        f"{name} ({p})"
        for name, p in [
            ("engine principles", engine_path),
            ("design principles", design_path),
            ("skills guide", guide_path),
        ]
        if not p.is_file()
    ]
    if missing:
        raise FileNotFoundError(
            "code-review: missing knowledge sources:\n  " + "\n  ".join(missing)
        )
    return engine_path.read_text(), design_path.read_text(), guide_path.read_text()


async def _load_arch() -> str:
    try:
        result = await shell.run(str(CORE_ROOT / "dev.sh"), args=["arch"], cwd=str(CORE_ROOT))
        return result.get("stdout", "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

EVALUATOR_SYSTEM_PROMPT = """You are the CTO of agentOS — a pre-launch Rust+Python agent operating system.
You review every commit against the project's principles and architectural direction.

Be rigorous but fair. You are protecting the codebase from entropy — every violation
you let through makes the next fix harder.

## Cross-repo awareness

The diff may include changes from multiple repos: core (engine, Rust), docs (shapes +
SDK docs), skills (YAML + Python), sdk-skills (Python SDK), apps (TypeScript), and
sdk-apps (TypeScript SDK). Sections are labeled with their repo name and whether the
change is staged (being committed) or uncommitted (work-in-progress in a sibling repo).

Treat ALL changes holistically — this is one product with one team. Violations in
uncommitted sibling repo code are just as blocking as violations in the staged commit.
If an uncommitted skill has issues, flag them. If the staged commit is fine but a sibling
repo has problems, still fail the review. The developer wants to keep everything clean
across all repos before any commit lands.

Write your review as markdown with this YAML frontmatter:

---
verdict: pass | fail
---

Then include:

## Summary
One sentence: what this commit does and whether it's good.

## Violations
A markdown list. For each violation:
- **CRITICAL/MAJOR/MINOR** [`principle name`] `file/path`: What's wrong

Severity guide:
- CRITICAL: Entity-specific code in Rust, hardcoded provider/service names in generic
  code, forcing structured JSON output on LLMs, making LLMs do arithmetic/scoring,
  over-constraining agent output formats
- MAJOR: Rendering logic in wrong module, missing delegation pattern, not giving agents
  feedback channels or prior work context
- MINOR: Naming inconsistencies, unnecessary complexity, missing campsite-rule improvements

If the diff touches code that agents interact with (skills, prompts, SDK, tools),
also evaluate against the Agent Ergonomics Principles provided below.

verdict: pass if no critical violations and ≤2 major violations. Otherwise fail."""


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@returns({
    "score": "integer",
    "maxScore": "integer",
    "pass": "boolean",
    "violations": "string",
    "summary": "string",
})
@test.skip(reason='destructive or unsupported — migrated from yaml')
@timeout(120)
async def evaluate_commit(
    diff: str,
    files: str = "",
    threshold: int = 90,
    model: str = "sonnet",
    **params,
) -> dict:
    """Evaluate a git diff against project principles.

    Args:
        diff: The staged git diff to evaluate
        files: Newline-separated list of changed file paths
        threshold: Minimum passing score out of 100 (default 90)
        model: LLM model to use for evaluation (default sonnet)
    """
    engine_principles, design_principles, skill_guide = _load_principles()
    arch = await _load_arch()

    prompt_parts = [
        "Evaluate this git diff against the principles and specs below.",
        "",
    ]

    if engine_principles:
        prompt_parts += ["## Engine & Agent Ergonomics Principles", engine_principles, ""]
    if design_principles:
        prompt_parts += ["## Design Principles", design_principles, ""]
    if skill_guide:
        prompt_parts += ["## Skill SDK Guide", skill_guide, ""]
    if arch:
        prompt_parts += ["## Architecture (crate sizes, dependency chain, largest files)", arch, ""]

    prompt_parts += [
        "## Files changed",
        files,
        "",
        "## Diff",
        diff,
    ]

    result = await llm.oneshot(
        prompt="\n".join(prompt_parts),
        system=EVALUATOR_SYSTEM_PROMPT,
        model=model,
    )

    content = result.get("content", "")

    # Parse verdict from frontmatter
    verdict = "fail"
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
                verdict = fm.get("verdict", "fail")
            except yaml.YAMLError:
                pass

    # Count violations by severity from markdown list
    critical = content.lower().count("**critical**")
    major = content.lower().count("**major**")
    minor = content.lower().count("**minor**")

    # Compute score: start at 100, deduct per violation
    score = max(0, 100 - (critical * 30) - (major * 15) - (minor * 5))

    # Extract summary section
    summary = "No summary"
    for line in content.splitlines():
        line_s = line.strip()
        if line_s and not line_s.startswith("#") and not line_s.startswith("-") and not line_s.startswith("---") and not line_s.startswith("verdict"):
            summary = line_s
            break

    # Extract violations section
    violations_md = "None"
    in_violations = False
    violation_lines = []
    for line in content.splitlines():
        if line.strip().lower().startswith("## violation"):
            in_violations = True
            continue
        if in_violations and line.startswith("## "):
            break
        if in_violations and line.strip().startswith("- "):
            violation_lines.append(line.strip())
    if violation_lines:
        violations_md = "\n".join(violation_lines)

    return {"__result__": {
        "score": score,
        "maxScore": 100,
        "pass": verdict == "pass" and score >= threshold,
        "violations": violations_md,
        "summary": summary,
    }}
