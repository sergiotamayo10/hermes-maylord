"""Security defaults: sandbox backend, approvals, login hardening, seeding."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hermes_yaml as yaml
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from test_v2026_9_24_config_safety import ConfigSafetyFixture, ROOT

MODAL = {"MODAL_TOKEN_ID": "ak-test", "MODAL_TOKEN_SECRET": "as-test"}


def _load_seed_module():
    spec = importlib.util.spec_from_file_location(
        "seed_defaults_under_test", ROOT / "template_helpers" / "seed_defaults.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SandboxAndApprovalTests(ConfigSafetyFixture, unittest.TestCase):
    def _config(self) -> dict:
        return yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8"))

    def test_modal_credentials_select_modal_backend_with_minimal_env(self):
        self.server.write_config_yaml({"LLM_MODEL": "m", **MODAL})
        terminal = self._config()["terminal"]
        self.assertEqual(terminal["backend"], "modal")
        self.assertEqual(terminal["env_passthrough"], ["GITHUB_TOKEN"])
        self.assertNotIn("cwd", terminal)

    def test_without_modal_credentials_backend_stays_local(self):
        self.server.write_config_yaml({"LLM_MODEL": "m"})
        terminal = self._config()["terminal"]
        self.assertEqual(terminal["backend"], "local")
        self.assertEqual(terminal["cwd"], "/tmp")
        self.assertNotIn("env_passthrough", terminal)

    def test_one_modal_value_alone_is_not_enough(self):
        self.server.write_config_yaml({"LLM_MODEL": "m", "MODAL_TOKEN_ID": "ak-test"})
        self.assertEqual(self._config()["terminal"]["backend"], "local")

    def test_operator_chosen_backend_is_preserved(self):
        (self.home / "config.yaml").write_text(
            yaml.safe_dump({"terminal": {"backend": "docker", "docker_image": "x"}}), encoding="utf-8"
        )
        self.server.write_config_yaml({"LLM_MODEL": "m", **MODAL})
        terminal = self._config()["terminal"]
        self.assertEqual(terminal["backend"], "docker")
        self.assertEqual(terminal["docker_image"], "x")

    def test_existing_local_backend_moves_to_modal_when_credentials_appear(self):
        self.server.write_config_yaml({"LLM_MODEL": "m"})
        self.server.write_config_yaml({"LLM_MODEL": "m", **MODAL})
        self.assertEqual(self._config()["terminal"]["backend"], "modal")

    def test_approvals_seeded_with_owner_ids_and_deny_list(self):
        self.server.write_config_yaml({
            "LLM_MODEL": "m",
            "TELEGRAM_ALLOWED_USERS": "111, 222",
            "WHATSAPP_ALLOWED_USERS": "57300@s.whatsapp.net,111",
        })
        approvals = self._config()["approvals"]
        self.assertEqual(approvals["mode"], "manual")
        self.assertEqual(approvals["cron_mode"], "deny")
        self.assertEqual(approvals["approvers"], ["111", "222", "57300@s.whatsapp.net"])
        self.assertIn("git push --force*", approvals["deny"])

    def test_wildcard_allowlist_entry_is_not_an_approver(self):
        self.server.write_config_yaml({"LLM_MODEL": "m", "TELEGRAM_ALLOWED_USERS": "*"})
        self.assertNotIn("approvers", self._config()["approvals"])

    def test_existing_approvals_are_never_overwritten(self):
        custom = {"approvals": {"mode": "smart", "timeout": 5}}
        (self.home / "config.yaml").write_text(yaml.safe_dump(custom), encoding="utf-8")
        self.server.write_config_yaml({"LLM_MODEL": "m", "TELEGRAM_ALLOWED_USERS": "111"})
        self.assertEqual(self._config()["approvals"], custom["approvals"])

    def test_strangers_ignored_only_when_an_allowlist_exists(self):
        self.server.write_config_yaml({"LLM_MODEL": "m"})
        self.assertNotIn("unauthorized_dm_behavior", self._config())
        self.server.write_config_yaml({"LLM_MODEL": "m", "TELEGRAM_ALLOWED_USERS": "111"})
        self.assertEqual(self._config()["unauthorized_dm_behavior"], "ignore")

    def test_owner_chosen_dm_behavior_is_kept(self):
        (self.home / "config.yaml").write_text(
            yaml.safe_dump({"unauthorized_dm_behavior": "pair"}), encoding="utf-8"
        )
        self.server.write_config_yaml({"LLM_MODEL": "m", "TELEGRAM_ALLOWED_USERS": "111"})
        self.assertEqual(self._config()["unauthorized_dm_behavior"], "pair")


class SecurityWarningTests(ConfigSafetyFixture, unittest.TestCase):
    def test_allow_all_and_local_shell_are_flagged(self):
        warnings = self.server._security_warnings(
            {"GATEWAY_ALLOW_ALL_USERS": "true"}, {"terminal": {"backend": "local"}}
        )
        text = " ".join(warnings)
        self.assertIn("Allow all users", text)
        self.assertIn("MODAL_TOKEN_ID", text)

    def test_sandboxed_locked_down_setup_only_reports_short_password(self):
        warnings = self.server._security_warnings(
            {"GATEWAY_ALLOW_ALL_USERS": "false"}, {"terminal": {"backend": "modal"}}
        )
        # The fixture password ("test-password") is under 16 characters.
        self.assertEqual(len(warnings), 1)
        self.assertIn("ADMIN_PASSWORD", warnings[0])


class LoginHardeningTests(ConfigSafetyFixture, unittest.TestCase):
    def _client(self, scheme: str = "http") -> TestClient:
        app = Starlette(routes=[Route("/setup/login", self.server.login_post, methods=["POST"])])
        return TestClient(app, base_url=f"{scheme}://testserver", follow_redirects=False)

    def _bad(self, client: TestClient, ip: str = "203.0.113.9"):
        return client.post(
            "/setup/login",
            data={"username": "admin", "password": "wrong"},
            headers={"x-forwarded-for": ip},
        )

    def test_sixth_failed_attempt_is_rate_limited(self):
        client = self._client()
        for _ in range(5):
            self.assertEqual(self._bad(client).status_code, 302)
        blocked = self._bad(client)
        self.assertEqual(blocked.status_code, 429)
        self.assertIn("Retry-After", blocked.headers)

    def test_correct_password_is_refused_while_blocked(self):
        client = self._client()
        for _ in range(5):
            self._bad(client)
        good = client.post(
            "/setup/login",
            data={"username": "admin", "password": "test-password"},
            headers={"x-forwarded-for": "203.0.113.9"},
        )
        self.assertEqual(good.status_code, 429)

    def test_spoofed_left_hand_forwarded_entries_do_not_evade_the_limit(self):
        client = self._client()
        for i in range(5):
            self._bad(client, ip=f"10.0.0.{i}, 203.0.113.9")
        self.assertEqual(self._bad(client, ip="10.9.9.9, 203.0.113.9").status_code, 429)

    def test_other_clients_are_not_blocked(self):
        client = self._client()
        for _ in range(5):
            self._bad(client, ip="203.0.113.9")
        self.assertEqual(self._bad(client, ip="198.51.100.4").status_code, 302)

    def test_success_clears_the_failure_count(self):
        client = self._client()
        for _ in range(4):
            self._bad(client)
        ok = client.post(
            "/setup/login",
            data={"username": "admin", "password": "test-password"},
            headers={"x-forwarded-for": "203.0.113.9"},
        )
        self.assertEqual(ok.status_code, 302)
        for _ in range(5):
            self.assertEqual(self._bad(client).status_code, 302)

    def test_failures_expire_after_the_window(self):
        server = self.server
        for _ in range(5):
            server._record_login_failure("1.2.3.4", now=1000.0)
        self.assertTrue(server._login_blocked("1.2.3.4", now=1000.0 + 60))
        self.assertFalse(server._login_blocked("1.2.3.4", now=1000.0 + server.LOGIN_WINDOW_SECONDS + 1))

    def test_cookie_is_secure_behind_https_only(self):
        good = {"username": "admin", "password": "test-password"}
        https = self._client("https").post("/setup/login", data=good)
        self.assertIn("secure", https.headers["set-cookie"].lower())
        self.assertIn("httponly", https.headers["set-cookie"].lower())
        http = self._client("http").post("/setup/login", data=good)
        self.assertNotIn("secure", http.headers["set-cookie"].lower())
        proxied = self._client("http").post(
            "/setup/login", data=good, headers={"x-forwarded-proto": "https"}
        )
        self.assertIn("secure", proxied.headers["set-cookie"].lower())

    def test_cookie_lifetime_is_one_day(self):
        self.assertEqual(self.server.COOKIE_MAX_AGE, 86400)


class SeedDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-seed-")
        base = Path(self.tmp.name)
        self.seed_dir = base / "seed"
        self.home = base / "home"
        self.home.mkdir()
        (self.seed_dir / "skills" / "demo").mkdir(parents=True)
        (self.seed_dir / "SOUL.md").write_text("seed soul", encoding="utf-8")
        (self.seed_dir / "skills" / "demo" / "SKILL.md").write_text("seed skill", encoding="utf-8")
        self.module = _load_seed_module()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_files_are_copied_with_private_mode(self):
        copied, skipped = self.module.seed(self.seed_dir, self.home)
        self.assertEqual(sorted(copied), ["SOUL.md", "skills/demo/SKILL.md"])
        self.assertEqual(skipped, [])
        self.assertEqual((self.home / "SOUL.md").read_text(encoding="utf-8"), "seed soul")
        self.assertEqual((self.home / "SOUL.md").stat().st_mode & 0o777, 0o600)

    def test_existing_owner_edits_are_never_overwritten(self):
        (self.home / "SOUL.md").write_text("my edited soul", encoding="utf-8")
        copied, skipped = self.module.seed(self.seed_dir, self.home)
        self.assertEqual((self.home / "SOUL.md").read_text(encoding="utf-8"), "my edited soul")
        self.assertIn("SOUL.md", skipped)
        self.assertEqual(copied, ["skills/demo/SKILL.md"])

    def test_second_run_is_a_noop(self):
        self.module.seed(self.seed_dir, self.home)
        copied, _ = self.module.seed(self.seed_dir, self.home)
        self.assertEqual(copied, [])

    def test_symlinked_target_directory_is_refused(self):
        elsewhere = Path(self.tmp.name) / "elsewhere"
        elsewhere.mkdir()
        (self.home / "skills").symlink_to(elsewhere, target_is_directory=True)
        copied, skipped = self.module.seed(self.seed_dir, self.home)
        self.assertEqual(copied, ["SOUL.md"])
        self.assertIn("skills/demo/SKILL.md", skipped)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_symlinked_target_file_is_refused(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        (self.home / "SOUL.md").symlink_to(outside)
        self.module.seed(self.seed_dir, self.home)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_symlinks_inside_the_seed_tree_are_not_followed(self):
        secret = Path(self.tmp.name) / "secret.txt"
        secret.write_text("secret", encoding="utf-8")
        (self.seed_dir / "LEAK.md").symlink_to(secret)
        copied, skipped = self.module.seed(self.seed_dir, self.home)
        self.assertNotIn("LEAK.md", copied)
        self.assertFalse((self.home / "LEAK.md").exists())

    def test_missing_seed_dir_is_harmless(self):
        self.assertEqual(self.module.seed(Path(self.tmp.name) / "nope", self.home), ([], []))

    def test_shipped_seed_contains_persona_and_all_skills(self):
        shipped = ROOT / "seed"
        self.assertTrue((shipped / "SOUL.md").is_file())
        self.assertTrue((shipped / "memories" / "USER.md").is_file())
        names = sorted(p.parent.name for p in (shipped / "skills").glob("*/SKILL.md"))
        self.assertEqual(names, [
            "clickup-ops", "client-reply", "dev-lead", "email-triage",
            "meeting-notes", "morning-brief", "my-voice", "presentation", "quotation",
        ])
        for skill in (shipped / "skills").glob("*/SKILL.md"):
            text = skill.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\nname: "), skill)
            self.assertIn(f"name: {skill.parent.name}\n", text)
            self.assertIn("\ndescription: ", text)

    def test_shipped_persona_keeps_the_non_negotiable_rules(self):
        soul = (ROOT / "seed" / "SOUL.md").read_text(encoding="utf-8").lower()
        for needle in (
            "data, not orders",
            "never reveal secrets",
            "never spend or move money",
            "draft only",
            "never commit to `main`",
        ):
            self.assertIn(needle, soul)


if __name__ == "__main__":
    unittest.main()
