"""Outbound audit log: classification, redaction, plugin + gateway hook, Setup reader."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import hermes_yaml as yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_v2026_9_24_config_safety import ConfigSafetyFixture, ROOT  # noqa: E402

PLUGIN_DIR = ROOT / "seed" / "plugins" / "outbound-audit"
HOOK_DIR = ROOT / "seed" / "hooks" / "outbound-audit"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _audit():
    return _load("audit_under_test", PLUGIN_DIR / "audit.py")


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.audit = _audit()

    def reason(self, tool, args=None):
        return self.audit.outbound_reason(tool, args)

    def test_terminal_outbound_commands_are_flagged(self):
        for command in (
            "git push origin agent/fix",
            "cd repo && git push --force-with-lease",
            "gh pr merge 12 --squash",
            "gh issue comment 3 -b hi",
            "curl -X POST https://api.example.com/x",
            "curl --request DELETE https://api.example.com/x",
            "curl -d 'a=1' https://api.example.com/x",
            "echo hi | mail -s subject a@b.co",
            "npm publish",
        ):
            self.assertIsNotNone(self.reason("terminal", {"command": command}), command)

    def test_terminal_read_only_commands_are_ignored(self):
        for command in ("ls -la", "git status", "git log --oneline", "curl https://example.com", "cat README.md"):
            self.assertIsNone(self.reason("terminal", {"command": command}), command)

    def test_native_send_like_tools_are_flagged(self):
        for tool in ("send_message", "reply_comment", "post_message", "forward_email"):
            self.assertIsNotNone(self.reason(tool, {}), tool)

    def test_native_file_tools_are_not_flagged(self):
        for tool in ("read_file", "write_file", "patch", "search_files", "web_search", "memory"):
            self.assertIsNone(self.reason(tool, {}), tool)

    def test_mcp_state_changing_tools_are_flagged(self):
        for tool in ("mcp_gmail_send_email", "mcp_clickup_create_task", "mcp_clickup_update_task",
                     "mcp_github_merge_pull_request", "mcp__gmail__reply_to_thread"):
            self.assertIsNotNone(self.reason(tool, {}), tool)

    def test_mcp_read_tools_are_not_flagged_even_with_outbound_words(self):
        for tool in ("mcp_gmail_search_messages", "mcp_clickup_get_comment", "mcp_github_list_comments",
                     "mcp_clickup_get_task", "mcp_gmail_read_email"):
            self.assertIsNone(self.reason(tool, {}), tool)

    def test_native_create_update_are_not_flagged_only_mcp(self):
        self.assertIsNone(self.reason("create_file", {}))
        self.assertIsNotNone(self.reason("mcp_x_create_item", {}))

    def test_empty_names_are_ignored(self):
        self.assertIsNone(self.reason("", {}))
        self.assertIsNone(self.reason(None, None))


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.audit = _audit()

    def test_known_token_shapes_are_removed(self):
        secrets = [
            "ghp_" + "a" * 36,
            "github_pat_" + "B" * 30,
            "sk-" + "c" * 30,
            "xoxb-1234567890-abcdef",
            "AKIA" + "D" * 16,
            "Bearer " + "e" * 30,
        ]
        for secret in secrets:
            out = self.audit.redact(f"x {secret} y")
            self.assertNotIn(secret, out)
            self.assertIn("[REDACTED]", out)

    def test_keyed_values_are_redacted_but_keys_kept(self):
        out = self.audit.redact('curl -H "Authorization: abc123secret" --data password=hunter2 api_key: "zzz"')
        for leaked in ("abc123secret", "hunter2", "zzz"):
            self.assertNotIn(leaked, out)
        self.assertIn("password=", out)

    def test_summary_is_redacted_and_truncated(self):
        args = {"command": "git push https://x:ghp_" + "a" * 36 + "@github.com/o/r " + "z" * 500}
        summary = self.audit.summarize_args("terminal", args)
        self.assertLessEqual(len(summary), self.audit.TEXT_LIMIT)
        self.assertNotIn("ghp_", summary)

    def test_tool_record_never_contains_the_result(self):
        record = self.audit.build_tool_record("send_message", {"to": "a"}, "ok", "s1", 12)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["ms"], 12)
        self.assertNotIn("result", record)
        self.assertIsNone(self.audit.build_tool_record("read_file", {}))


class ReplyRecordTests(unittest.TestCase):
    def setUp(self):
        self.audit = _audit()
        self.owners = {"111"}

    def ctx(self, **overrides):
        base = {"platform": "telegram", "user_id": "111", "chat_id": "111",
                "chat_type": "dm", "session_id": "s", "response": "hello"}
        base.update(overrides)
        return base

    def test_owner_dm_reply_is_skipped(self):
        self.assertIsNone(self.audit.build_reply_record(self.ctx(), self.owners))

    def test_group_reply_is_logged(self):
        rec = self.audit.build_reply_record(self.ctx(chat_type="group", chat_id="-100"), self.owners)
        self.assertEqual(rec["kind"], "reply")
        self.assertEqual(rec["chat_id"], "-100")

    def test_dm_with_someone_else_is_logged(self):
        rec = self.audit.build_reply_record(self.ctx(user_id="999"), self.owners)
        self.assertIsNotNone(rec)

    def test_no_owner_ids_means_nothing_is_skipped(self):
        self.assertIsNotNone(self.audit.build_reply_record(self.ctx(), set()))

    def test_empty_response_is_skipped_and_secrets_redacted(self):
        self.assertIsNone(self.audit.build_reply_record(self.ctx(chat_type="group", response="  "), self.owners))
        rec = self.audit.build_reply_record(
            self.ctx(chat_type="group", response="key sk-" + "q" * 30), self.owners)
        self.assertNotIn("sk-q", rec["text"])

    def test_owner_ids_parse_all_allowlists_and_ignore_wildcard(self):
        env = {"TELEGRAM_ALLOWED_USERS": "1, 2", "WHATSAPP_ALLOWED_USERS": "3,*", "GATEWAY_ALLOWED_USERS": ""}
        self.assertEqual(self.audit.owner_ids(env), {"1", "2", "3"})


class AppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-audit-")
        self.path = Path(self.tmp.name) / "logs" / "outbound.jsonl"
        self.audit = _audit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_appends_json_lines_with_private_mode(self):
        self.audit.append_record(self.path, {"a": 1})
        self.audit.append_record(self.path, {"b": "é"})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x) for x in lines], [{"a": 1}, {"b": "é"}])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_rotates_when_the_file_grows_too_large(self):
        with patch.object(self.audit, "MAX_BYTES", 50):
            for i in range(10):
                self.audit.append_record(self.path, {"n": i, "pad": "x" * 20})
        self.assertTrue(Path(str(self.path) + ".1").exists())
        self.assertTrue(self.path.exists())
        self.assertLess(self.path.stat().st_size, 200)


class PluginAndHookIntegrationTests(unittest.TestCase):
    """Seed into a temp home with the real seeder, then run plugin and hook from there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hermes-audit-home-")
        self.home = Path(self.tmp.name) / ".hermes"
        self.home.mkdir()
        seeder = _load("seed_for_audit", ROOT / "template_helpers" / "seed_defaults.py")
        seeder.seed(ROOT / "seed", self.home)
        self.log = self.home / "logs" / "outbound.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _plugin(self):
        name = f"outbound_audit_plugin_{id(self)}"
        spec = importlib.util.spec_from_file_location(
            name, self.home / "plugins" / "outbound-audit" / "__init__.py",
            submodule_search_locations=[str(self.home / "plugins" / "outbound-audit")],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def test_plugin_registers_post_tool_call_and_logs_outbound_calls_only(self):
        registered = {}

        class Ctx:
            def register_hook(self, hook, callback):
                registered[hook] = callback

        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            self._plugin().register(Ctx())
            self.assertEqual(list(registered), ["post_tool_call"])
            cb = registered["post_tool_call"]
            cb(tool_name="read_file", args={"path": "x"}, status="ok", session_id="s", duration_ms=1)
            self.assertFalse(self.log.exists())
            cb(tool_name="terminal", args={"command": "git push origin x"}, status="ok",
               session_id="s", duration_ms=5, extra_future_kwarg=True)
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "terminal")
        self.assertEqual(rows[0]["why"], "terminal:git")

    def test_plugin_callback_never_raises_when_logging_fails(self):
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            module = self._plugin()
            with patch.object(module._audit, "append_record", side_effect=OSError("disk full")):
                self.assertIsNone(module._on_post_tool_call(tool_name="send_message", args={}))

    def test_gateway_hook_logs_group_replies_and_skips_owner_dm(self):
        handler = _load("audit_hook_under_test", self.home / "hooks" / "outbound-audit" / "handler.py")
        env = {"TELEGRAM_ALLOWED_USERS": "111"}
        with patch.dict(os.environ, env):
            handler.handle("agent:end", {"platform": "telegram", "user_id": "111", "chat_id": "111",
                                         "chat_type": "dm", "response": "private"})
            self.assertFalse(self.log.exists())
            handler.handle("agent:end", {"platform": "whatsapp", "user_id": "5730", "chat_id": "grp",
                                         "chat_type": "group", "response": "Estado: listo. sk-" + "k" * 30})
        row = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((row["kind"], row["platform"], row["chat_id"]), ("reply", "whatsapp", "grp"))
        self.assertNotIn("sk-k", row["text"])

    def test_gateway_hook_swallows_errors(self):
        handler = _load("audit_hook_err", self.home / "hooks" / "outbound-audit" / "handler.py")
        self.assertIsNone(handler.handle("agent:end", None))

    def test_manifests_declare_the_expected_hooks(self):
        plugin = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "outbound-audit")
        self.assertEqual(plugin["provides_hooks"], ["post_tool_call"])
        hook = yaml.safe_load((HOOK_DIR / "HOOK.yaml").read_text(encoding="utf-8"))
        self.assertEqual(hook["events"], ["agent:end"])


class SetupIntegrationTests(ConfigSafetyFixture, unittest.TestCase):
    def _config(self) -> dict:
        return yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8"))

    def test_plugin_is_enabled_alongside_existing_plugins(self):
        (self.home / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["my-plugin"], "hook_callback_timeout": 9}}), encoding="utf-8")
        self.server.write_config_yaml({"LLM_MODEL": "m"})
        self.server.write_config_yaml({"LLM_MODEL": "m"})  # idempotent
        plugins = self._config()["plugins"]
        self.assertEqual(plugins["enabled"], ["my-plugin", "outbound-audit"])
        self.assertEqual(plugins["hook_callback_timeout"], 9)

    def test_plugin_enabled_on_a_fresh_config(self):
        self.server.write_config_yaml({"LLM_MODEL": "m"})
        self.assertEqual(self._config()["plugins"]["enabled"], ["outbound-audit"])

    def test_reader_merges_profiles_sorts_newest_first_and_skips_bad_lines(self):
        (self.home / "logs").mkdir()
        (self.home / "profiles" / "acme" / "logs").mkdir(parents=True)
        (self.home / "logs" / "outbound.jsonl").write_text(
            '{"ts":"2026-11-10T10:00:00+00:00","kind":"tool","tool":"a"}\nnot json\n'
            '{"ts":"2026-11-10T12:00:00+00:00","kind":"tool","tool":"c"}\n', encoding="utf-8")
        (self.home / "profiles" / "acme" / "logs" / "outbound.jsonl").write_text(
            '{"ts":"2026-11-10T11:00:00+00:00","kind":"reply","platform":"whatsapp"}\n', encoding="utf-8")
        rows = self.server._read_audit_tail(self.home, limit=10)
        self.assertEqual([r.get("tool") or r.get("platform") for r in rows], ["c", "whatsapp", "a"])
        self.assertEqual(rows[1]["profile"], "acme")
        self.assertNotIn("profile", rows[0])
        self.assertEqual(len(self.server._read_audit_tail(self.home, limit=2)), 2)

    def test_reader_ignores_symlinked_logs_and_missing_files(self):
        self.assertEqual(self.server._read_audit_tail(self.home), [])
        outside = Path(self.tmp.name) / "outside.jsonl"
        outside.write_text('{"ts":"2026-11-10T10:00:00+00:00","tool":"leak"}\n', encoding="utf-8")
        (self.home / "logs").mkdir()
        (self.home / "logs" / "outbound.jsonl").symlink_to(outside)
        self.assertEqual(self.server._read_audit_tail(self.home), [])

    def test_audit_endpoint_requires_login_and_returns_entries(self):
        from starlette.applications import Starlette
        from starlette.routing import Route
        from starlette.testclient import TestClient

        (self.home / "logs").mkdir()
        (self.home / "logs" / "outbound.jsonl").write_text(
            '{"ts":"2026-11-10T10:00:00+00:00","kind":"tool","tool":"send_message"}\n', encoding="utf-8")
        client = TestClient(Starlette(routes=[Route("/setup/api/audit", self.server.api_audit)]),
                            follow_redirects=False)
        self.assertNotEqual(client.get("/setup/api/audit").status_code, 200)
        client.cookies.set(self.server.COOKIE_NAME, self.server._make_auth_token())
        ok = client.get("/setup/api/audit")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["entries"][0]["tool"], "send_message")


if __name__ == "__main__":
    unittest.main()
