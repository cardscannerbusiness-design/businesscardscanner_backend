"""In-memory + file-backed store for WhatsApp delivery status webhooks.

Meta returns a wamid when the Graph API *accepts* a send. Actual phone delivery
is reported asynchronously via webhook statuses: sent → delivered → read, or failed.

File backing lets CLI diagnostics in a separate process see webhook updates that
arrive in the API worker process.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_STATUSES: dict[str, dict[str, Any]] = {}
_MAX_ENTRIES = 500
_TTL_SECONDS = 6 * 3600
_STORE_PATH = Path(__file__).resolve().parent.parent / "data" / "whatsapp_delivery_status.json"


def _prune_locked(now: float) -> None:
    expired = [
        mid
        for mid, row in _STATUSES.items()
        if now - float(row.get("updated_at") or 0) > _TTL_SECONDS
    ]
    for mid in expired:
        _STATUSES.pop(mid, None)
    if len(_STATUSES) <= _MAX_ENTRIES:
        return
    ordered = sorted(_STATUSES.items(), key=lambda item: float(item[1].get("updated_at") or 0))
    for mid, _ in ordered[: max(0, len(_STATUSES) - _MAX_ENTRIES)]:
        _STATUSES.pop(mid, None)


def _load_disk_locked() -> None:
    try:
        raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for mid, row in raw.items():
                if isinstance(row, dict):
                    _STATUSES[str(mid)] = row
    except Exception:
        pass


def _save_disk_locked() -> None:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STORE_PATH.write_text(json.dumps(_STATUSES), encoding="utf-8")
    except Exception:
        pass


def record_message_status(
    message_id: str,
    status: str,
    *,
    recipient_id: str | None = None,
    errors: list[Any] | None = None,
    timestamp: str | None = None,
) -> None:
    mid = (message_id or "").strip()
    if not mid:
        return
    now = time.time()
    with _LOCK:
        if not _STATUSES:
            _load_disk_locked()
        _prune_locked(now)
        prev = _STATUSES.get(mid) or {}
        _STATUSES[mid] = {
            "message_id": mid,
            "status": (status or "").strip().lower() or "unknown",
            "recipient_id": recipient_id or prev.get("recipient_id"),
            "errors": errors if errors is not None else prev.get("errors") or [],
            "timestamp": timestamp or prev.get("timestamp"),
            "updated_at": now,
            "history": list(prev.get("history") or [])
            + [
                {
                    "status": (status or "").strip().lower() or "unknown",
                    "at": now,
                    "errors": errors or [],
                }
            ],
        }
        _save_disk_locked()


def get_message_status(message_id: str, *, reload: bool = True) -> dict[str, Any] | None:
    mid = (message_id or "").strip()
    if not mid:
        return None
    with _LOCK:
        if reload or mid not in _STATUSES:
            _load_disk_locked()
        row = _STATUSES.get(mid)
        return dict(row) if row else None


def wait_for_terminal_status(
    message_id: str,
    *,
    timeout_seconds: float = 12.0,
    poll_seconds: float = 0.5,
) -> dict[str, Any] | None:
    """Block until delivered/read/failed, or timeout (return latest if any)."""
    deadline = time.time() + max(0.5, timeout_seconds)
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = get_message_status(message_id)
        if last and last.get("status") in {"delivered", "read", "failed"}:
            return last
        time.sleep(max(0.1, poll_seconds))
    return last or get_message_status(message_id)
