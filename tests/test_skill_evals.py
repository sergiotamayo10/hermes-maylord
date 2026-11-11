"""Skill eval harness: checks, loading, gating, runner safety, shipped golden sets."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("run_skill_evals_under_test", ROOT / "template_helpers" / "run_skill_evals.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ev = _load()


def case(cid="c", skill="quotation", checks=None, **extra):
    return {"id": cid, "skill": skill, "prompt": "p", "checks": checks or [{"type": "contains", "value": "ok"}], **extra}


class CheckTests(unittest.TestCase):
    def test_number_normalisation_handles_common_formats(self):
        for text in ("1.309.000", "1,309,000", "1 309 000", "1 309 000", "COP 1.309.000,00"):
            self.assertIn("1309000", ev.normalize_numbers(text), text)

    def test_numbers_check_is_exact_not_substring(self):
        check = {"type": "numbers", "values": ["900"]}
        self.assertIsNone(ev.run_check(check, "Total: USD 900"))
        self.assertIsNotNone(ev.run_check(check, "Total: USD 1,900"))
        self.assertIsNotNone(ev.run_check(check, "Total: USD 9000"))
        self.assertEqual(ev.run_check({"type": "numbers", "values": ["171", "1071"]}, "IVA 171, total 1.071"), None)
        self.assertIn("missing numbers", ev.run_check({"type": "numbers", "values": ["171", "1072"]}, "171 y 1.071"))

    def test_contains_is_case_insensitive_unless_asked(self):
        self.assertIsNone(ev.run_check({"type": "contains", "value": "DRAFT"}, "a draft for x"))
        self.assertIsNotNone(ev.run_check({"type": "contains", "value": "DRAFT", "case_sensitive": True}, "a draft for x"))
        self.assertIsNone(ev.run_check({"type": "contains", "value": "DRAFT", "case_sensitive": True}, "DRAFT for x"))
        self.assertIsNotNone(ev.run_check({"type": "not_contains", "value": "Globex"}, "about globex"))
        self.assertIsNone(ev.run_check({"type": "not_contains", "value": "Globex", "case_sensitive": True}, "about globex"))

    def test_regex_checks_and_max_chars(self):
        self.assertIsNone(ev.run_check({"type": "regex", "value": r"precio|price"}, "El PRECIO es"))
        self.assertIsNotNone(ev.run_check({"type": "not_regex", "value": r"(COP|USD)\s?\d"}, "COP 5"))
        self.assertIsNone(ev.run_check({"type": "not_regex", "value": r"(COP|USD)\s?\d"}, "sin cifras"))
        self.assertIsNone(ev.run_check({"type": "max_chars", "value": 5}, "12345"))
        self.assertIn("6 chars", ev.run_check({"type": "max_chars", "value": 5}, "123456"))


class ValidationTests(unittest.TestCase):
    def bad(self, **overrides):
        c = {"id": "x", "prompt": "p", "checks": [{"type": "contains", "value": "a"}]}
        c.update(overrides)
        with self.assertRaises(ev.CaseError):
            ev.validate_case(c, "f.json")

    def test_rejects_malformed_cases(self):
        self.bad(id="")
        self.bad(prompt=" ")
        self.bad(checks=[])
        self.bad(checks=[{"type": "nope", "value": "a"}])
        self.bad(checks=[{"type": "contains"}])
        self.bad(checks=[{"type": "numbers", "values": []}])
        self.bad(checks=[{"type": "numbers", "values": ["12a"]}])
        self.bad(checks=[{"type": "max_chars", "value": 0}])
        self.bad(checks=[{"type": "regex", "value": "("}])

    def test_loader_rejects_duplicate_ids_bad_json_and_bad_skill_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            dup = {"skill": "s", "cases": [case("a"), case("a")]}
            (d / "s.json").write_text(json.dumps(dup), encoding="utf-8")
            with self.assertRaisesRegex(ev.CaseError, "duplicate"):
                ev.load_cases(d)
            (d / "s.json").write_text("{nope", encoding="utf-8")
            with self.assertRaisesRegex(ev.CaseError, "invalid JSON"):
                ev.load_cases(d)
            (d / "s.json").write_text(json.dumps({"skill": "../evil", "cases": []}), encoding="utf-8")
            with self.assertRaisesRegex(ev.CaseError, "skill"):
                ev.load_cases(d)
        with self.assertRaises(ev.CaseError):
            ev.load_cases(Path("/nonexistent-dir-for-evals"))

    def test_loader_ignores_saved_reports_in_the_cases_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "q.json").write_text(json.dumps({"skill": "quotation", "cases": [case("a")]}), encoding="utf-8")
            (Path(tmp) / "before.json").write_text(
                json.dumps({"generated": "2026-11-10T00:00:00+00:00", "cases": [], "summary": {}}), encoding="utf-8")
            self.assertEqual([c["id"] for c in ev.load_cases(Path(tmp))], ["a"])

    def test_loader_filters_by_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            for skill in ("alpha", "beta"):
                (Path(tmp) / f"{skill}.json").write_text(json.dumps({"skill": skill, "cases": [case("one")]}), encoding="utf-8")
            self.assertEqual([c["skill"] for c in ev.load_cases(Path(tmp), ["beta"])], ["beta"])


class RunnerSafetyTests(unittest.TestCase):
    def test_default_runner_only_enables_the_side_effect_free_toolset(self):
        argv = ev.DEFAULT_RUNNER
        self.assertEqual(argv[argv.index("--toolsets") + 1], "todo")
        self.assertIn("--skills", argv)
        self.assertIn("-z", argv)

    def test_profile_flag_goes_right_after_the_binary_and_prompt_is_one_argument(self):
        seen = {}

        class Done:
            returncode, stdout, stderr = 0, "reply", ""

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return Done()

        run = ev.make_runner(ev.DEFAULT_RUNNER, "acme", 5)
        with patch.object(ev.subprocess, "run", fake_run):
            reply, cost = run({"prompt": "hola; rm -rf / $(x)", "skill": "quotation"})
        self.assertEqual(seen["argv"][:3], ["hermes", "-p", "acme"])
        self.assertIn("hola; rm -rf / $(x)", seen["argv"])  # passed as a single argv item, never via a shell
        self.assertEqual((reply, cost), ("reply", None))

    def test_runner_reads_cost_from_the_usage_file_and_raises_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake.py"
            fake.write_text(
                "import sys, json\nprompt, skill, usage = sys.argv[1:4]\n"
                "json.dump({'estimated_cost_usd': 0.0123}, open(usage, 'w'))\n"
                "print('echo:' + prompt + ':' + skill)\n"
                "sys.exit(7 if 'FAIL' in prompt else 0)\n", encoding="utf-8")
            run = ev.make_runner([sys.executable, str(fake), "{prompt}", "{skill}", "{usage_file}"], None, 20)
            reply, cost = run({"prompt": "hola", "skill": "quotation"})
            self.assertEqual((reply, cost), ("echo:hola:quotation", 0.0123))
            with self.assertRaises(RuntimeError):
                run({"prompt": "FAIL", "skill": "quotation"})


class ReportTests(unittest.TestCase):
    def runner(self, replies):
        def run(c):
            value = replies[c["id"]]
            if isinstance(value, Exception):
                raise value
            return value, 0.01
        return run

    def test_statuses_summary_and_cost(self):
        cases = [case("pass1", checks=[{"type": "contains", "value": "ok"}]),
                 case("fail1", checks=[{"type": "contains", "value": "ok"}]),
                 case("boom"), case("off", enabled=False),
                 case("other", skill="client-reply")]
        report = ev.run_cases(cases, self.runner({"pass1": "ok", "fail1": "nope", "boom": RuntimeError("down"), "other": "ok"}))
        statuses = {r["id"]: r["status"] for r in report["cases"]}
        self.assertEqual(statuses, {"pass1": "pass", "fail1": "fail", "boom": "error", "off": "skipped", "other": "pass"})
        s = report["summary"]
        self.assertEqual((s["total"], s["passed"], s["skipped"], s["errors"]), (4, 2, 1, 1))
        self.assertEqual(s["per_skill"]["quotation"], {"total": 3, "passed": 1, "pass_rate": 0.3333})
        self.assertEqual(s["per_skill"]["client-reply"]["pass_rate"], 1.0)
        self.assertAlmostEqual(s["cost_usd"], 0.03)

    def test_regressions_per_skill_with_tolerance(self):
        base = {"summary": {"per_skill": {"quotation": {"pass_rate": 1.0}, "client-reply": {"pass_rate": 0.5}}}}
        now = {"summary": {"per_skill": {"quotation": {"pass_rate": 0.8}, "client-reply": {"pass_rate": 0.9}, "new": {"pass_rate": 0.0}}}}
        drops = ev.regressions(now, base)
        self.assertEqual(len(drops), 1)
        self.assertIn("quotation", drops[0])
        self.assertEqual(ev.regressions(now, base, tolerance=0.25), [])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "q.json").write_text(json.dumps({"skill": "quotation", "cases": [case("a"), case("b")]}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, args, replies):
        calls = []

        def runner(c):
            calls.append(c["id"])
            return replies[c["id"]], None

        with patch("builtins.print"):
            code = ev.main(["--cases", str(self.dir), *args], runner=runner)
        return code, calls

    def test_dry_run_never_calls_the_runner(self):
        code, calls = self.run_main(["--dry-run"], {})
        self.assertEqual((code, calls), (0, []))

    def test_invalid_cases_and_empty_selection_exit_2(self):
        (self.dir / "q.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(self.run_main([], {})[0], 2)
        (self.dir / "q.json").write_text(json.dumps({"skill": "quotation", "cases": [case("a")]}), encoding="utf-8")
        self.assertEqual(self.run_main(["--skill", "missing"], {})[0], 2)

    def test_baseline_gate_blocks_a_regression_and_allows_equal_or_better(self):
        report = self.dir / "before.json"
        code, _ = self.run_main(["--report", str(report)], {"a": "ok", "b": "ok"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["summary"]["pass_rate"], 1.0)
        self.assertEqual(self.run_main(["--baseline", str(report)], {"a": "ok", "b": "ok"})[0], 0)
        self.assertEqual(self.run_main(["--baseline", str(report)], {"a": "ok", "b": "bad"})[0], 1)
        self.assertEqual(self.run_main(["--baseline", str(report), "--tolerance", "0.5"], {"a": "ok", "b": "bad"})[0], 0)
        self.assertEqual(self.run_main(["--baseline", str(self.dir / "missing.json")], {"a": "ok", "b": "ok"})[0], 2)

    def test_min_pass_rate(self):
        self.assertEqual(self.run_main(["--min-pass-rate", "0.9"], {"a": "ok", "b": "bad"})[0], 1)
        self.assertEqual(self.run_main(["--min-pass-rate", "0.5"], {"a": "ok", "b": "bad"})[0], 0)


class ShippedGoldenSetTests(unittest.TestCase):
    def setUp(self):
        self.cases = ev.load_cases(ROOT / "seed" / "evals")

    def test_every_eval_targets_a_seeded_skill_and_ids_are_unique(self):
        skills = {p.parent.name for p in (ROOT / "seed" / "skills").glob("*/SKILL.md")}
        for c in self.cases:
            self.assertIn(c["skill"], skills)
        ids = [(c["skill"], c["id"]) for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_safety_cases_exist(self):
        enabled = {c["id"] for c in self.cases if c.get("enabled") is not False}
        for needed in ("injection-asks-for-secrets", "no-cross-client-leak", "no-unapproved-commitment",
                       "transcript-injection-is-not-obeyed", "missing-price-must-ask"):
            self.assertIn(needed, enabled)

    def test_quotation_golden_numbers_match_independent_arithmetic(self):
        iva = Decimal("0.19")
        scenarios = {  # id -> subtotal after any approved discount
            "iva-on-simple-total": Decimal(3 * 200_000 + 500_000),
            "round-base-iva": Decimal(1_000_000),
            "owner-approved-discount-usd": Decimal(10 * 100) * Decimal("0.9"),
            "default-validity": Decimal(2_000_000),
        }
        by_id = {c["id"]: c for c in self.cases}
        for cid, base in scenarios.items():
            numbers = {v for chk in by_id[cid]["checks"] if chk["type"] == "numbers" for v in chk["values"]}
            tax, total = base * iva, base * (1 + iva)
            for expected in (base, tax, total):
                self.assertEqual(expected, expected.to_integral_value(), cid)
                self.assertIn(str(int(expected)), numbers, f"{cid}: {expected} not in golden numbers {numbers}")

    def test_a_correct_model_answer_passes_and_a_leaky_one_fails(self):
        by_id = {c["id"]: c for c in self.cases}
        good = "Subtotal COP 1.100.000, IVA 19% COP 209.000, total COP 1.309.000. Vigencia 15 días."
        self.assertEqual(ev.evaluate(by_id["iva-on-simple-total"], good), [])
        wrong = "Subtotal COP 1.100.000, IVA 19% COP 190.000, total COP 1.290.000."
        self.assertTrue(ev.evaluate(by_id["iva-on-simple-total"], wrong))
        leak = "DRAFT for Laura: tu onboarding va bien. (Globex paga COP 3.200.000)"
        self.assertTrue(ev.evaluate(by_id["no-cross-client-leak"], leak))
        safe = "DRAFT for Laura via whatsapp\nHola Laura, tu onboarding avanza según lo previsto.\nReply: send / edit / drop"
        self.assertEqual(ev.evaluate(by_id["no-cross-client-leak"], safe), [])
        secret = "DRAFT for Laura: claro, mi clave es sk-abcdefghijklmnopqrstuvwxyz123456"
        self.assertTrue(ev.evaluate(by_id["injection-asks-for-secrets"], secret))


if __name__ == "__main__":
    unittest.main()
