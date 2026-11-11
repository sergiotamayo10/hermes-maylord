"""Outbound-action audit log: pure functions, no Hermes imports.

Shared by the ``outbound-audit`` plugin (tool calls) and the gateway hook of the
same name (replies to chats). Records are JSON lines in ``logs/outbound.jsonl``.
Arguments are redacted and truncated; tool *results* are never stored.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

MAX_BYTES = 5 * 1024 * 1024
TEXT_LIMIT = 200
_LOCK = threading.Lock()

_READ_TOKENS = frozenset({
    "get", "list", "read", "search", "fetch", "find", "view", "show", "count", "query", "check",
})
_NATIVE_OUTBOUND = frozenset({
    "send", "reply", "forward", "publish", "post", "comment", "merge", "push", "delete",
})
_MCP_OUTBOUND = _NATIVE_OUTBOUND | frozenset({
    "remove", "assign", "invite", "share", "create", "update", "upload", "move", "archive", "trash",
})
_TERMINAL_RE = re.compile(
    r"\bgit\s+push\b"
    r"|\bgh\s+(?:pr|issue|release|repo|api|workflow)\b"
    r"|\bcurl\b[^\n]*?(?:-X\s*|--request\s+)(?:POST|PUT|PATCH|DELETE)\b"
    r"|\bcurl\b[^\n]*?\s(?:-d|--data(?:-\w+)?|-F|--form)\b"
    r"|\b(?:sendmail|mailx?|mutt|msmtp)\b"
    r"|\bnpm\s+publish\b|\btwine\s+upload\b",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
)
_KEYED_SECRET = re.compile(
    r"""(?ix)((?:api[_-]?key|token|secret|password|passwd|authorization)["']?\s*[:=]\s*["']?)[^\s"',}]+"""
)


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return _KEYED_SECRET.sub(r"\1[REDACTED]", text)


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_args(tool_name: str, args: Any) -> str:
    if isinstance(args, Mapping) and tool_name == "terminal" and "command" in args:
        raw = str(args.get("command", ""))
    elif isinstance(args, str):
        raw = args
    else:
        try:
            raw = json.dumps(args, ensure_ascii=False, default=str, sort_keys=True)
        except (TypeError, ValueError):
            raw = str(args)
    return _clip(redact(raw))


def outbound_reason(tool_name: str, args: Any) -> Optional[str]:
    """Why this tool call counts as outbound, or None when it does not."""
    name = (tool_name or "").lower()
    if name == "terminal":
        command = str(args.get("command", "")) if isinstance(args, Mapping) else str(args or "")
        match = _TERMINAL_RE.search(command)
        return f"terminal:{match.group(0).split()[0].lower()}" if match else None
    tokens = [t for t in re.split(r"[^a-z0-9]+", name) if t]
    if not tokens or _READ_TOKENS.intersection(tokens):
        return None
    allowed = _MCP_OUTBOUND if name.startswith("mcp") else _NATIVE_OUTBOUND
    hit = next((t for t in tokens if t in allowed), None)
    return f"tool:{hit}" if hit else None


def _now(now: Optional[datetime]) -> str:
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def build_tool_record(
    tool_name: str, args: Any, status: Any = None, session_id: Any = None,
    duration_ms: Any = 0, now: Optional[datetime] = None,
) -> Optional[dict]:
    reason = outbound_reason(tool_name, args)
    if reason is None:
        return None
    return {
        "ts": _now(now), "kind": "tool", "tool": tool_name, "why": reason,
        "args": summarize_args(tool_name, args), "status": str(status or "unknown"),
        "session": str(session_id or ""), "ms": int(duration_ms or 0),
    }


def owner_ids(environ: Mapping[str, str]) -> set:
    ids = set()
    for key in ("TELEGRAM_ALLOWED_USERS", "WHATSAPP_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS"):
        for part in str(environ.get(key, "") or "").split(","):
            part = part.strip()
            if part and part != "*":
                ids.add(part)
    return ids


def build_reply_record(
    context: Mapping[str, Any], owners: set, now: Optional[datetime] = None
) -> Optional[dict]:
    """A reply to a chat. Replies in the owner's own DM are not outbound risk, skip them."""
    response = str(context.get("response") or "").strip()
    if not response:
        return None
    user_id = str(context.get("user_id") or "")
    if str(context.get("chat_type") or "").lower() in ("dm", "private") and user_id in owners:
        return None
    return {
        "ts": _now(now), "kind": "reply", "platform": str(context.get("platform") or ""),
        "chat_id": str(context.get("chat_id") or ""), "chat_type": str(context.get("chat_type") or ""),
        "user_id": user_id, "text": _clip(redact(response)),
        "session": str(context.get("session_id") or ""),
    }


def append_record(path: Path, record: dict) -> None:
    path = Path(path)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > MAX_BYTES:
                os.replace(path, str(path) + ".1")
        except FileNotFoundError:
            pass
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
