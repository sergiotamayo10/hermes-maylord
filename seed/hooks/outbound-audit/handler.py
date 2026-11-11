"""Gateway hook: record replies to chats via the shared audit module."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _load_audit():
    # <home>/hooks/outbound-audit/handler.py -> <home>/plugins/outbound-audit/audit.py
    home = Path(__file__).resolve().parents[2]
    path = home / "plugins" / "outbound-audit" / "audit.py"
    spec = importlib.util.spec_from_file_location("outbound_audit_shared", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_AUDIT = None


def handle(event_type: str, context: dict) -> None:
    global _AUDIT
    try:
        if _AUDIT is None:
            _AUDIT = _load_audit()
        if _AUDIT is None:
            return
        record = _AUDIT.build_reply_record(context or {}, _AUDIT.owner_ids(os.environ))
        if record is not None:
            home = Path(__file__).resolve().parents[2]
            _AUDIT.append_record(home / "logs" / "outbound.jsonl", record)
    except Exception:  # noqa: BLE001 - never let auditing disturb a turn
        return None
