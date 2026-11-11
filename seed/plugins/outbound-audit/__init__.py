"""outbound-audit plugin: log outbound tool calls. Observer only, never blocks."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from . import audit as _audit

logger = logging.getLogger(__name__)


def _log_path() -> Path:
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - fall back when imported outside Hermes
        home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return home / "logs" / "outbound.jsonl"


def _on_post_tool_call(
    tool_name: str = "", args: Any = None, status: Any = None,
    session_id: Any = None, duration_ms: Any = 0, **_: Any,
) -> None:
    try:
        record = _audit.build_tool_record(tool_name, args, status, session_id, duration_ms)
        if record is not None:
            _audit.append_record(_log_path(), record)
    except Exception as exc:  # noqa: BLE001 - an audit failure must never affect the agent
        logger.debug("outbound-audit: could not record %s: %s", tool_name, exc)
    return None


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
