#!/usr/bin/env python3
"""Golden-set evals for the seeded skills (quotation math, client-reply safety, ...).

Each case sends one prompt through ``hermes -z`` with the skill preloaded and ONLY
the side-effect-free ``todo`` toolset (``-z`` auto-approves actions, so a test
prompt must never be able to send, write, or run commands), then applies
deterministic checks to the reply. Use it as a gate: record a baseline report,
change a skill, rerun with ``--baseline``; a drop in any skill's pass rate exits 1.

    run_skill_evals.py --dry-run                       # validate case files, no model calls
    run_skill_evals.py --report before.json            # run everything
    run_skill_evals.py --skill quotation --baseline before.json

It spends real model tokens on your configured provider; the report includes cost
when Hermes provides it. Run it on demand, not from cron.

Case files: ``<cases dir>/<skill>.json`` = {"skill": str, "cases": [{"id", "prompt",
"checks": [...], "enabled": bool?}]}. Check types: contains, not_contains, regex,
not_regex (``value``; optional ``"case_sensitive": true``), numbers (``values``: digit
strings that must appear once thousands separators are ignored), max_chars (``value``).

Stdlib only. Exit codes: 0 ok, 1 regression/min-pass-rate failure, 2 invalid cases or usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SAFE_TOOLSETS = "todo"
CHECK_TYPES = {"contains", "not_contains", "regex", "not_regex", "numbers", "max_chars"}
DEFAULT_RUNNER = ["hermes", "-z", "{prompt}", "--toolsets", SAFE_TOOLSETS, "--skills", "{skill}",
                  "--usage-file", "{usage_file}"]


class CaseError(ValueError):
    pass


# ── case loading / validation ────────────────────────────────────────────────
def validate_case(case: Any, where: str) -> None:
    if not isinstance(case, dict):
        raise CaseError(f"{where}: case must be an object")
    for key in ("id", "prompt"):
        if not isinstance(case.get(key), str) or not case[key].strip():
            raise CaseError(f"{where}: missing '{key}'")
    checks = case.get("checks")
    if not isinstance(checks, list) or not checks:
        raise CaseError(f"{where} ({case['id']}): needs at least one check")
    for check in checks:
        kind = check.get("type") if isinstance(check, dict) else None
        if kind not in CHECK_TYPES:
            raise CaseError(f"{where} ({case['id']}): unknown check type {kind!r}")
        if kind == "numbers":
            vals = check.get("values")
            if not isinstance(vals, list) or not vals or not all(isinstance(v, str) and v.isdigit() for v in vals):
                raise CaseError(f"{where} ({case['id']}): numbers.values must be digit strings")
        elif kind == "max_chars":
            if not isinstance(check.get("value"), int) or check["value"] <= 0:
                raise CaseError(f"{where} ({case['id']}): max_chars.value must be a positive integer")
        elif not isinstance(check.get("value"), str) or not check["value"]:
            raise CaseError(f"{where} ({case['id']}): {kind} needs a string 'value'")
        if kind in ("regex", "not_regex"):
            try:
                re.compile(check["value"])
            except re.error as exc:
                raise CaseError(f"{where} ({case['id']}): bad regex: {exc}") from exc


def load_cases(cases_dir: Path, only_skills: Optional[list] = None) -> list[dict]:
    """Return flat case dicts (with ``skill``) from every ``*.json`` in the directory."""
    cases_dir = Path(cases_dir)
    if not cases_dir.is_dir():
        raise CaseError(f"cases directory not found: {cases_dir}")
    out: list[dict] = []
    seen: set = set()
    for path in sorted(cases_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CaseError(f"{path.name}: invalid JSON: {exc}") from exc
        if isinstance(doc, dict) and "generated" in doc and "summary" in doc:
            continue  # a saved report that happens to live next to the case files
        skill = doc.get("skill") if isinstance(doc, dict) else None
        if not isinstance(skill, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", skill):
            raise CaseError(f"{path.name}: 'skill' must be a skill name")
        if only_skills and skill not in only_skills:
            continue
        for case in doc.get("cases", []):
            validate_case(case, path.name)
            key = (skill, case["id"])
            if key in seen:
                raise CaseError(f"{path.name}: duplicate case id {case['id']!r}")
            seen.add(key)
            out.append({**case, "skill": skill})
    return out


# ── checks ───────────────────────────────────────────────────────────────────
_THOUSANDS = re.compile(r"[.,'   ](?=\d{3}(?!\d))")


def normalize_numbers(text: str) -> str:
    """Drop thousands separators (1.190.000 / 1,190,000 / 1 190 000 -> 1190000)."""
    return _THOUSANDS.sub("", text)


def run_check(check: dict, reply: str) -> Optional[str]:
    """None when the check passes, else a short failure description."""
    kind = check["type"]
    flags = 0 if check.get("case_sensitive") else re.IGNORECASE
    if kind in ("contains", "not_contains"):
        value = check["value"]
        hit = value in reply if check.get("case_sensitive") else value.lower() in reply.lower()
        if kind == "contains":
            return None if hit else f"missing {value!r}"
        return f"contains forbidden {value!r}" if hit else None
    if kind == "regex":
        return None if re.search(check["value"], reply, flags) else f"no match for /{check['value']}/"
    if kind == "not_regex":
        return f"forbidden pattern /{check['value']}/ matched" if re.search(check["value"], reply, flags) else None
    if kind == "numbers":
        flat = normalize_numbers(reply)
        missing = [v for v in check["values"] if not re.search(rf"(?<!\d){re.escape(v)}(?!\d)", flat)]
        return f"missing numbers {missing}" if missing else None
    if kind == "max_chars":
        return None if len(reply) <= check["value"] else f"{len(reply)} chars > {check['value']}"
    raise CaseError(f"unknown check type {kind!r}")


def evaluate(case: dict, reply: str) -> list[str]:
    return [msg for check in case["checks"] if (msg := run_check(check, reply))]


# ── running ──────────────────────────────────────────────────────────────────
Runner = Callable[[dict], tuple]


def make_runner(template: list, profile: Optional[str], timeout: int) -> Runner:
    """Build a runner: ``case -> (reply_text, cost_usd_or_None)``; raises on failure."""

    def run(case: dict) -> tuple:
        with tempfile.TemporaryDirectory(prefix="skill-eval-") as tmp:
            usage_file = str(Path(tmp) / "usage.json")
            argv = [part.replace("{prompt}", case["prompt"]).replace("{skill}", case["skill"])
                    .replace("{usage_file}", usage_file) for part in template]
            if profile:
                argv = [argv[0], "-p", profile, *argv[1:]]
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:300])
            cost = None
            try:
                usage = json.loads(Path(usage_file).read_text(encoding="utf-8"))
                cost = usage.get("estimated_cost_usd")
                cost = float(cost) if cost is not None else None
            except (OSError, ValueError, TypeError):
                pass
            return proc.stdout.strip(), cost

    return run


def run_cases(cases: list, runner: Runner) -> dict:
    results = []
    for case in cases:
        if case.get("enabled") is False:
            results.append({"id": case["id"], "skill": case["skill"], "status": "skipped"})
            continue
        try:
            reply, cost = runner(case)
        except Exception as exc:  # noqa: BLE001 - a runner failure is a failed case, not a crash
            results.append({"id": case["id"], "skill": case["skill"], "status": "error",
                            "error": str(exc)[:300], "cost_usd": None})
            continue
        failures = evaluate(case, reply)
        results.append({"id": case["id"], "skill": case["skill"],
                        "status": "pass" if not failures else "fail",
                        "failures": failures, "cost_usd": cost, "reply_preview": reply[:160]})
    return {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cases": results, "summary": summarize(results)}


def summarize(results: list) -> dict:
    per_skill: dict = {}
    total_cost = 0.0
    for r in results:
        if r["status"] == "skipped":
            continue
        bucket = per_skill.setdefault(r["skill"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += r["status"] == "pass"
        total_cost += r.get("cost_usd") or 0.0
    for bucket in per_skill.values():
        bucket["pass_rate"] = round(bucket["passed"] / bucket["total"], 4) if bucket["total"] else 0.0
    total = sum(b["total"] for b in per_skill.values())
    passed = sum(b["passed"] for b in per_skill.values())
    return {"total": total, "passed": passed, "pass_rate": round(passed / total, 4) if total else 0.0,
            "skipped": sum(r["status"] == "skipped" for r in results),
            "errors": sum(r["status"] == "error" for r in results),
            "cost_usd": round(total_cost, 4), "per_skill": per_skill}


def regressions(report: dict, baseline: dict, tolerance: float = 0.0) -> list:
    """Per-skill pass-rate drops versus the baseline report (skills absent from either are ignored)."""
    now, before = report["summary"]["per_skill"], baseline.get("summary", {}).get("per_skill", {})
    out = []
    for skill, stats in now.items():
        if skill in before and stats["pass_rate"] < before[skill]["pass_rate"] - tolerance:
            out.append(f"{skill}: pass rate {before[skill]['pass_rate']:.0%} -> {stats['pass_rate']:.0%}")
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv: Optional[list] = None, runner: Optional[Runner] = None) -> int:
    home = os.environ.get("HERMES_HOME", "/data/.hermes")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", default=str(Path(home) / "evals"), help="directory of <skill>.json files")
    parser.add_argument("--skill", action="append", help="only this skill (repeatable)")
    parser.add_argument("--profile", help="Hermes profile to run as")
    parser.add_argument("--runner", help="custom command template using {prompt} {skill} {usage_file}")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--report", help="write the JSON report here")
    parser.add_argument("--baseline", help="compare with an earlier report; exit 1 on regression")
    parser.add_argument("--tolerance", type=float, default=0.0, help="allowed pass-rate drop (0.05 = 5 points)")
    parser.add_argument("--min-pass-rate", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true", help="validate case files only; no model calls")
    args = parser.parse_args(argv)

    try:
        cases = load_cases(Path(args.cases), args.skill)
    except CaseError as exc:
        print(f"[evals] invalid cases: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        live = sum(c.get("enabled") is not False for c in cases)
        print(f"[evals] {len(cases)} case(s) valid ({live} enabled, {len(cases) - live} disabled)")
        return 0
    if not cases:
        print("[evals] no cases found", file=sys.stderr)
        return 2

    template = shlex.split(args.runner) if args.runner else DEFAULT_RUNNER
    report = run_cases(cases, runner or make_runner(template, args.profile, args.timeout))
    summary = report["summary"]
    for skill, stats in sorted(summary["per_skill"].items()):
        print(f"[evals] {skill}: {stats['passed']}/{stats['total']} ({stats['pass_rate']:.0%})")
    for r in report["cases"]:
        if r["status"] in ("fail", "error"):
            print(f"[evals]   {r['status'].upper()} {r['skill']}/{r['id']}: "
                  f"{'; '.join(r.get('failures') or [r.get('error', '')])}")
    print(f"[evals] total {summary['passed']}/{summary['total']} ({summary['pass_rate']:.0%}), "
          f"{summary['errors']} error(s), cost ${summary['cost_usd']:.4f}")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    status = 0
    if args.baseline:
        try:
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[evals] cannot read baseline: {exc}", file=sys.stderr)
            return 2
        drops = regressions(report, baseline, args.tolerance)
        for line in drops:
            print(f"[evals] REGRESSION {line}")
        status = 1 if drops else status
    if args.min_pass_rate is not None and summary["pass_rate"] < args.min_pass_rate:
        print(f"[evals] pass rate {summary['pass_rate']:.0%} below minimum {args.min_pass_rate:.0%}")
        status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
