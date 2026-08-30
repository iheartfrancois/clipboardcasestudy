"""SQLite store for the review queue, per-field proposals and the audit log.

Local workflow state only (approve/reject decisions, what we sent to the
CRM). Lives at ``data/crm_review.db`` which is gitignored - the shareable
artifact is ``data/crm_snapshot.json`` produced by the pipeline.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from . import compare

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "crm_review.db"

ITEM_STATUSES = ("pending", "applied", "rejected", "error", "resolved_upstream", "rehearsed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS review_items (
            community_name    TEXT PRIMARY KEY,
            classification    TEXT NOT NULL,
            crm_account_id    TEXT,
            crm_account_name  TEXT,
            crm_updated_at    TEXT,
            crm_view_json     TEXT,
            name_match_score  REAL,
            possible_crm_match TEXT,
            possible_matches_json TEXT,
            details           TEXT,
            scraped_json      TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            note              TEXT,
            first_seen        TEXT NOT NULL,
            last_seen         TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS field_proposals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            community_name  TEXT NOT NULL REFERENCES review_items(community_name) ON DELETE CASCADE,
            field           TEXT NOT NULL,
            scraped_value   TEXT,
            crm_value       TEXT,
            care_candidates TEXT,
            decision        TEXT NOT NULL DEFAULT 'pending',
            decided_at      TEXT,
            UNIQUE(community_name, field)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT NOT NULL,
            community_name TEXT NOT NULL,
            action         TEXT NOT NULL,
            payload_json   TEXT,
            api_status     INTEGER,
            api_response   TEXT,
            actor          TEXT DEFAULT 'reviewer'
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    conn.commit()
    return conn


# --- sync from a comparison run --------------------------------------

def _replace_proposals(conn, name: str, diffs: List[compare.FieldDiff]) -> None:
    conn.execute("DELETE FROM field_proposals WHERE community_name = ?", (name,))
    for d in diffs:
        conn.execute(
            "INSERT INTO field_proposals "
            "(community_name, field, scraped_value, crm_value, care_candidates) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                name,
                d.field,
                d.scraped_value,
                d.crm_value,
                json.dumps(d.care_candidates) if d.care_candidates is not None else None,
            ),
        )


def sync_results(results: List[compare.Result], db_path: Optional[Path] = None,
                 sweep_absent: bool = True) -> Dict[str, int]:
    """Reconcile a fresh comparison into the queue.

    New non-matching communities become ``pending``. Communities that used to
    be pending and are now fine become ``resolved_upstream``. Items the
    reviewer already decided (applied/rejected/error) are left alone, except
    an ``applied`` item that is still non-matching is re-opened.

    ``sweep_absent`` closes CRM-account-driven items ('stale' / 'duplicate')
    that the comparison no longer produces. Only pass it for a **live**
    comparison - a snapshot import may be partial or from an older code
    version, so it must never remove items.
    """
    conn = get_connection(db_path)
    stats = {"new": 0, "refreshed": 0, "resolved_upstream": 0, "reopened": 0}
    try:
        by_name = {r.community_name: r for r in results}
        existing = {
            row["community_name"]: row
            for row in conn.execute("SELECT * FROM review_items")
        }

        for r in results:
            row = existing.get(r.community_name)
            actionable = r.classification in (
                "needs update", "missing", "chow", "stale", "duplicate")

            if not actionable:
                if (sweep_absent and row is not None
                        and row["status"] in ("pending", "rehearsed")):
                    conn.execute(
                        "UPDATE review_items SET status='resolved_upstream', "
                        "last_seen=?, details=? WHERE community_name=?",
                        (_now(), "no longer flagged by the daily comparison", r.community_name),
                    )
                    conn.execute("DELETE FROM field_proposals WHERE community_name=?", (r.community_name,))
                    stats["resolved_upstream"] += 1
                continue

            common = dict(
                classification=r.classification,
                crm_account_id=r.crm_account_id,
                crm_account_name=r.crm_account_name,
                crm_updated_at=(r.crm_view or {}).get("updated_at", ""),
                crm_view_json=json.dumps(r.crm_view or {}),
                name_match_score=r.name_match_score,
                possible_crm_match=r.possible_crm_match,
                possible_matches_json=json.dumps(r.possible_matches or []),
                details=r.details,
                scraped_json=json.dumps(r.scraped),
            )

            if row is None:
                conn.execute(
                    "INSERT INTO review_items (community_name, classification, "
                    "crm_account_id, crm_account_name, crm_updated_at, crm_view_json, "
                    "name_match_score, possible_crm_match, possible_matches_json, details, "
                    "scraped_json, status, first_seen, last_seen) "
                    "VALUES (:cn, :classification, :crm_account_id, :crm_account_name, "
                    ":crm_updated_at, :crm_view_json, :name_match_score, :possible_crm_match, "
                    ":possible_matches_json, :details, :scraped_json, 'pending', :now, :now)",
                    {"cn": r.community_name, "now": _now(), **common},
                )
                _replace_proposals(conn, r.community_name, r.field_diffs)
                stats["new"] += 1
            elif row["status"] == "pending":
                conn.execute(
                    "UPDATE review_items SET classification=:classification, "
                    "crm_account_id=:crm_account_id, crm_account_name=:crm_account_name, "
                    "crm_updated_at=:crm_updated_at, crm_view_json=:crm_view_json, "
                    "name_match_score=:name_match_score, "
                    "possible_crm_match=:possible_crm_match, "
                    "possible_matches_json=:possible_matches_json, "
                    "details=:details, scraped_json=:scraped_json, last_seen=:now "
                    "WHERE community_name=:cn",
                    {"cn": r.community_name, "now": _now(), **common},
                )
                _replace_proposals(conn, r.community_name, r.field_diffs)
                stats["refreshed"] += 1
            elif (row["status"] in ("applied", "rehearsed", "resolved_upstream")
                  and (sweep_absent or row["status"] == "rehearsed")):
                # A snapshot import (sweep_absent=False) may be stale, so it
                # only un-does dry-run rehearsals - it never re-opens a real
                # apply or an upstream resolution.
                reopen_note = {
                    "rehearsed": "re-opened: only rehearsed in dry run",
                    "applied": "re-opened: still flagged after a previous apply",
                    "resolved_upstream": "re-opened: flagged again by the comparison",
                }[row["status"]]
                conn.execute(
                    "UPDATE review_items SET status='pending', classification=:classification, "
                    "crm_account_id=:crm_account_id, crm_account_name=:crm_account_name, "
                    "crm_updated_at=:crm_updated_at, crm_view_json=:crm_view_json, "
                    "name_match_score=:name_match_score, "
                    "possible_crm_match=:possible_crm_match, "
                    "possible_matches_json=:possible_matches_json, "
                    "details=:details, scraped_json=:scraped_json, last_seen=:now, "
                    "note=:reopen_note "
                    "WHERE community_name=:cn",
                    {"cn": r.community_name, "now": _now(), "reopen_note": reopen_note, **common},
                )
                _replace_proposals(conn, r.community_name, r.field_diffs)
                stats["reopened"] += 1
            # rejected / error / resolved_upstream: leave as-is

        # A CRM-account-driven item ('stale' / 'duplicate', keyed by a synthetic
        # name rather than a scraped community) that the comparison no longer
        # produces -> it was resolved another way. Close it out.
        #
        # Deliberately limited to those: scraped-community items (needs update /
        # missing / chow) always reappear on a healthy run, so their transient
        # absence means a partial scrape, not a resolution - never sweep those.
        # Guard against an empty/failed comparison too.
        _crm_driven = {"stale", "duplicate"}
        if sweep_absent and any(r.classification not in _crm_driven for r in results):
            for name, row in existing.items():
                if (row["classification"] in _crm_driven and name not in by_name
                        and row["status"] in ("pending", "error", "rehearsed")):
                    conn.execute(
                        "UPDATE review_items SET status='resolved_upstream', last_seen=?, "
                        "details='no longer flagged by the daily comparison' "
                        "WHERE community_name=?",
                        (_now(), name),
                    )
                    conn.execute("DELETE FROM field_proposals WHERE community_name=?", (name,))
                    stats["resolved_upstream"] += 1

        conn.execute(
            "INSERT INTO meta(key,value) VALUES('last_sync',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_now(),),
        )
        conn.commit()
        return stats
    finally:
        conn.close()


# --- reads ----------------------------------------------------------

def _row_to_item(row: sqlite3.Row) -> Dict:
    item = dict(row)
    item["scraped"] = json.loads(item.pop("scraped_json"))
    item["possible_matches"] = json.loads(item.pop("possible_matches_json") or "[]")
    item["crm_view"] = json.loads(item.pop("crm_view_json") or "{}")
    return item


_TIER_CASE = "CASE classification " + " ".join(
    f"WHEN '{cls}' THEN {i}" for i, cls in enumerate(compare.QUEUE_ORDER)
) + f" ELSE {len(compare.QUEUE_ORDER)} END"


def queue(db_path: Optional[Path] = None) -> List[Dict]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM review_items WHERE status IN ('pending','error') "
            f"ORDER BY {_TIER_CASE}, community_name"
        ).fetchall()
        items = [_row_to_item(r) for r in rows]
        for it in items:
            it["fields"] = [
                p["field"] for p in conn.execute(
                    "SELECT field FROM field_proposals WHERE community_name=? ORDER BY id",
                    (it["community_name"],),
                )
            ]
        return items
    finally:
        conn.close()


def active_tier(db_path: Optional[Path] = None) -> Optional[str]:
    """The classification the reviewer must work next: the first tier in
    QUEUE_ORDER that still has a pending/error item. None when the queue is
    empty."""
    conn = get_connection(db_path)
    try:
        have = {
            r["classification"] for r in conn.execute(
                "SELECT DISTINCT classification FROM review_items "
                "WHERE status IN ('pending','error')"
            )
        }
        for cls in compare.QUEUE_ORDER:
            if cls in have:
                return cls
        return None
    finally:
        conn.close()


def all_items(db_path: Optional[Path] = None) -> List[Dict]:
    conn = get_connection(db_path)
    try:
        return [_row_to_item(r) for r in conn.execute(
            "SELECT * FROM review_items ORDER BY last_seen DESC")]
    finally:
        conn.close()


def get_item(community_name: str, db_path: Optional[Path] = None) -> Optional[Dict]:
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM review_items WHERE community_name=?", (community_name,)
        ).fetchone()
        if row is None:
            return None
        item = _row_to_item(row)
        props = conn.execute(
            "SELECT * FROM field_proposals WHERE community_name=? ORDER BY id",
            (community_name,),
        ).fetchall()
        item["proposals"] = []
        for p in props:
            p = dict(p)
            p["care_candidates"] = json.loads(p["care_candidates"]) if p["care_candidates"] else None
            item["proposals"].append(p)
        return item
    finally:
        conn.close()


def stats(db_path: Optional[Path] = None) -> Dict:
    conn = get_connection(db_path)
    try:
        counts = {s: 0 for s in ITEM_STATUSES}
        for row in conn.execute("SELECT status, COUNT(*) c FROM review_items GROUP BY status"):
            counts[row["status"]] = row["c"]
        last_sync = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
        counts["last_sync"] = last_sync["value"] if last_sync else None
        return counts
    finally:
        conn.close()


def audit(limit: int = 200, db_path: Optional[Path] = None) -> List[Dict]:
    conn = get_connection(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


# --- writes (from the review UI) ------------------------------------

def set_field_decisions(community_name: str, decisions: Dict[str, str], db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        for field, decision in decisions.items():
            conn.execute(
                "UPDATE field_proposals SET decision=?, decided_at=? "
                "WHERE community_name=? AND field=?",
                (decision, _now(), community_name, field),
            )
        conn.commit()
    finally:
        conn.close()


def log_audit(conn, community_name, action, payload=None, api_status=None, api_response=None):
    conn.execute(
        "INSERT INTO audit_log (ts, community_name, action, payload_json, api_status, api_response) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            _now(), community_name, action,
            json.dumps(payload) if payload is not None else None,
            api_status,
            json.dumps(api_response) if not isinstance(api_response, str) else api_response,
        ),
    )


def record_apply(
    community_name: str,
    action: str,
    payload: Dict,
    approved_fields: List[str],
    api_status: Optional[int],
    api_response,
    new_account_id: Optional[str] = None,
    dry_run: bool = False,
    db_path: Optional[Path] = None,
) -> None:
    conn = get_connection(db_path)
    try:
        if dry_run:
            # A rehearsal: nothing is sent to the CRM. Move the item to
            # 'rehearsed' so it leaves the active queue (no infinite loop) but
            # is clearly *not* 'applied'. Any real /refresh or an app restart
            # resets it to 'pending'.
            log_audit(conn, community_name, f"DRY-RUN {action}", payload, None, api_response)
            conn.execute(
                "UPDATE review_items SET status='rehearsed', last_seen=?, note=? "
                "WHERE community_name=?",
                (_now(), f"[dry run] {action} rehearsed {_now()[:19]}Z - nothing sent to the CRM",
                 community_name),
            )
            conn.commit()
            return

        log_audit(conn, community_name, action, payload, api_status, api_response)
        conn.execute(
            "UPDATE review_items SET status='applied', last_seen=?, "
            "note=? WHERE community_name=?",
            (_now(), f"{action} applied" + (f"; new account {new_account_id}" if new_account_id else ""),
             community_name),
        )
        if new_account_id:
            conn.execute(
                "UPDATE review_items SET crm_account_id=? WHERE community_name=?",
                (new_account_id, community_name),
            )
        for field in approved_fields:
            conn.execute(
                "UPDATE field_proposals SET decision='applied', decided_at=? "
                "WHERE community_name=? AND field=?",
                (_now(), community_name, field),
            )
        conn.commit()
    finally:
        conn.close()


def record_error(community_name: str, action: str, payload: Dict, api_status, api_response,
                 db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        log_audit(conn, community_name, action, payload, api_status, api_response)
        conn.execute(
            "UPDATE review_items SET status='error', last_seen=?, note=? WHERE community_name=?",
            (_now(), f"{action} failed ({api_status})", community_name),
        )
        conn.commit()
    finally:
        conn.close()


def record_reject(community_name: str, note: str = "", dry_run: bool = False,
                  db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        if dry_run:
            # In a rehearsal, a reject is a rehearsal too - it comes back on
            # the next refresh / restart like every other dry-run action.
            log_audit(conn, community_name, "DRY-RUN reject", {"note": note}, None, None)
            conn.execute(
                "UPDATE review_items SET status='rehearsed', last_seen=?, note=? "
                "WHERE community_name=?",
                (_now(), f"[dry run] reject rehearsed {_now()[:19]}Z"
                 + (f" - {note}" if note else ""), community_name),
            )
            conn.commit()
            return

        log_audit(conn, community_name, "reject", {"note": note})
        conn.execute(
            "UPDATE review_items SET status='rejected', last_seen=?, note=? WHERE community_name=?",
            (_now(), note or "rejected by reviewer", community_name),
        )
        conn.execute(
            "UPDATE field_proposals SET decision='rejected', decided_at=? "
            "WHERE community_name=? AND decision='pending'",
            (_now(), community_name),
        )
        conn.commit()
    finally:
        conn.close()


def reopen(community_name: str, db_path: Optional[Path] = None) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE review_items SET status='pending', note='re-opened by reviewer' "
            "WHERE community_name=?",
            (community_name,),
        )
        conn.execute(
            "UPDATE field_proposals SET decision='pending', decided_at=NULL WHERE community_name=?",
            (community_name,),
        )
        conn.commit()
    finally:
        conn.close()


def clear_rehearsed(db_path: Optional[Path] = None) -> int:
    """Reset every dry-run 'rehearsed' item back to 'pending'. Called on app
    startup so a rehearsal never permanently consumes queue items."""
    conn = get_connection(db_path)
    try:
        n = conn.execute(
            "UPDATE review_items SET status='pending', "
            "note='reset after dry-run rehearsal' WHERE status='rehearsed'"
        ).rowcount
        conn.execute(
            "UPDATE field_proposals SET decision='pending', decided_at=NULL "
            "WHERE community_name IN (SELECT community_name FROM review_items WHERE status='pending') "
            "AND decision='applied'"
        )
        conn.commit()
        return n
    finally:
        conn.close()
