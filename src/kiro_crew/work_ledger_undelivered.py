"""The work-ledger store's record of a worker-tool call the harness never delivered.

A worker reports only through ``work_report`` (and reads its item through
``work_brief``). When the harness cannot resolve that call -- KAS answers a bare
tool name with ``Tool "work_report" is not available.`` without ever reaching the
server -- nothing lands on the item, and the board shows a worker that went quiet.
The gateway sees the failed call in the worker's own turn
(:mod:`kiro_crew.undelivered_call_watch`) and records it here, and the ledger reads
derive ``report_undelivered`` from it: the conductor and the board say "couldn't
send its report" instead of guessing at a stall.

NOT AN ITEM WRITE. The items are a projection of the crew log, and this is not
something a worker or a conductor said: it is the gateway's observation of the
worker's transport, like the ``stale`` liveness flag. So it is kept beside the
items, one small file per worker under ``work-ledger/undelivered/``
(:func:`~kiro_crew.work_ledger_store.undelivered_path`), never under a conductor's
directory, which a rebuild from the crew log prunes. The whole ``work-ledger`` tree
is withheld from agent file tools, so a model cannot write the record itself.

CLEARED BY THE CALL GETTING THROUGH. Every ``work_brief`` or ``work_report`` that
reaches the ledger deletes its worker's record (:func:`clear_undelivered`, called by
the two route handlers), so the flag lasts exactly as long as the worker has not
got through since. Ordering is by when each operation ran, never by comparing
stamps, so a failure a moment after a success still shows.

STRUCTURAL ONLY. The record holds the item it was bound to, which worker tool
failed, and when. Never the harness's error text or the call's arguments: the flag
reaches the conductor's wake and the board, and a worker's words reach those only
through the identity-gated report path.

:mod:`kiro_crew.work_ledger` binds every name defined here, so each one reads,
imports and patches as ``kiro_crew.work_ledger.<name>``.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from kiro_crew.session_ledger import ledger_key
from kiro_crew.work_ledger_records import SCHEMA_VERSION, logger
from kiro_crew.work_ledger_store import (
    _ITEM_ID_RE,
    _read_json_record,
    _write_record,
    binding_lock,
    read_work_item,
    undelivered_path,
)
from kiro_crew.work_ledger_writes import read_binding

#: The tools a dispatched worker uses to read its item and report on it. A failed
#: conductor tool (``work_ledger_*``) is not recorded: the conductor is the one who
#: would read the flag, and it already saw its own call fail.
WORKER_TOOLS: frozenset[str] = frozenset({"work_brief", "work_report"})


def _undelivered_stamp() -> str:
    """Microsecond precision, so two failures in one second are two wakes, not one."""
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def note_undelivered(
    worker_slot_key: str, tool: str, *, observed_ns: int | None = None
) -> dict[str, Any] | None:
    """Record that *worker_slot_key*'s *tool* call never reached its item.

    Only for a session bound to an OPEN item that names it back, which is what makes
    it a dispatched worker; any other session's failed call records nothing.

    *observed_ns* is when the failed result was SEEN (``time.time_ns()``, taken by
    the watch before this runs off the event loop). The write is fenced against
    :func:`clear_undelivered` under the worker's binding lock: when a worker-tool
    call got through after the failure was seen, the clear's stamp is newer and this
    note is dropped, so a slow note cannot bring back a failure the worker already
    recovered from.

    Returns ``{"conductor_slot_key", "item_id", "tool", "at"}`` for what was
    written, ``None`` when nothing was. Never raises (cancellation aside): this runs
    inside a worker's turn, and a bookkeeping failure must not cost that turn.
    """
    if tool not in WORKER_TOOLS:
        return None
    seen = time.time_ns() if observed_ns is None else observed_ns
    try:
        key = ledger_key(worker_slot_key)
        with binding_lock(key):
            binding = read_binding(key)
            if binding is None:
                return None
            conductor_slot_key, item_id = binding
            item = read_work_item(conductor_slot_key, item_id)
            if item is None or item.is_terminal or item.worker_session_key != key:
                return None
            path = undelivered_path(key)
            prior = _read_json_record(path)
            cleared = prior.get("cleared_ns") if isinstance(prior, dict) else None
            if isinstance(cleared, int) and cleared >= seen:
                return None
            record = {
                "schema": SCHEMA_VERSION,
                "conductor_slot_key": conductor_slot_key,
                "item_id": item_id,
                "tool": tool,
                "at": _undelivered_stamp(),
                "observed_ns": seen,
            }
            _write_record(path, record)
    except Exception:  # noqa: BLE001 - best effort inside a worker's turn
        logger.warning("could not record an undelivered %s call", tool, exc_info=True)
        return None
    return {k: record[k] for k in ("conductor_slot_key", "item_id", "tool", "at")}


def clear_undelivered(worker_slot_key: str) -> None:
    """Forget *worker_slot_key*'s record: one of its worker-tool calls got through.

    Called by the ``work_brief`` and ``work_report`` route handlers once the call has
    reached the ledger, which is the one place that knows it did, whichever surface
    the worker's turn ran on. Leaves a ``cleared_ns`` stamp rather than nothing, so a
    note of an EARLIER failure still in flight is fenced off (see
    :func:`note_undelivered`). Never raises (cancellation aside).
    """
    try:
        key = ledger_key(worker_slot_key)
        with binding_lock(key):
            # Written even when no record exists: a note of an earlier failure may
            # still be on its way, and the stamp is what fences it off.
            _write_record(
                undelivered_path(key), {"schema": SCHEMA_VERSION, "cleared_ns": time.time_ns()}
            )
    except Exception:  # noqa: BLE001 - best effort beside a delivered call
        logger.debug("could not clear an undelivered-call record", exc_info=True)


def read_undelivered(conductor_slot_key: str, item: Any) -> dict[str, str] | None:
    """``{"tool", "at"}`` while *item*'s worker has an undelivered call, else ``None``.

    A record for another item (the worker was bound again since), another conductor,
    a closed item, or a record that does not read back whole is not this item's.
    Never raises.
    """
    worker = getattr(item, "worker_session_key", None) or ""
    if not worker or getattr(item, "is_terminal", False):
        return None
    try:
        raw = _read_json_record(undelivered_path(worker))
    except Exception:  # noqa: BLE001 - an unreadable record is no record
        return None
    if not isinstance(raw, dict):
        return None
    tool, at, item_id = raw.get("tool"), raw.get("at"), raw.get("item_id")
    if (
        not isinstance(tool, str)
        or tool not in WORKER_TOOLS
        or not isinstance(at, str)
        or not isinstance(item_id, str)
        or not _ITEM_ID_RE.match(item_id)
        or item_id != getattr(item, "item_id", None)
        or raw.get("conductor_slot_key") != conductor_slot_key
    ):
        return None
    return {"tool": tool, "at": at}
