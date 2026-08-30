"""Flask review UI for the daily CRM comparison.

    python -m crm_sync.app            # http://127.0.0.1:5000

Workflow: the daily comparison (APScheduler here, or the GitHub Action's
snapshot) fills a queue of `needs update` / `missing` communities. A reviewer
opens each one, approves/rejects the proposed changes per field, and the app
writes the approved changes to the CRM via its API.

No CRM write happens without an explicit confirm in the UI. Set
CRM_SYNC_DRY_RUN=1 to rehearse writes (payloads are logged, nothing is sent).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

from flask import (
    Flask, abort, flash, redirect, render_template, request, url_for,
)

from . import compare, pipeline, store
from .crm_api import (
    BELLHAVEN_PARENT_ID, CrmApiError, CrmClient, account_web_url, dry_run_enabled,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("crm_sync.app")

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "crm_snapshot.json"
CARE_TYPES = ["Skilled Nursing", "Assisted Living", "Memory Care", "Independent Living"]


# --- refresh helpers -------------------------------------------------

def refresh_from_live() -> Dict[str, int]:
    results = pipeline.run_comparison(progress=lambda m: log.info("refresh: %s", m))
    stats = store.sync_results(results)
    log.info("refresh_from_live: %s", stats)
    return stats


def import_snapshot_if_fresh() -> None:
    if not SNAPSHOT_PATH.exists():
        return
    try:
        snap = pipeline.load_snapshot(str(SNAPSHOT_PATH))
    except Exception as e:  # pragma: no cover - defensive
        log.warning("could not read snapshot: %s", e)
        return
    last_sync = store.stats().get("last_sync")
    if last_sync and snap["generated_at"] <= last_sync:
        return
    # A snapshot is a "here is fresh data" hint, not the authoritative list -
    # it may be partial or from an older code version. Only add / refresh
    # pending items; never resolve, reopen or sweep from it.
    store.sync_results(snap["result_objects"], sweep_absent=False)
    log.info("imported snapshot generated at %s", snap["generated_at"])


# --- app factory --------------------------------------------------

def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("CRM_SYNC_SECRET", "dev-only-not-secret")

    @app.context_processor
    def _globals():
        return {
            "dry_run": dry_run_enabled(),
            "stats": store.stats(),
            "crm_url": account_web_url,  # crm_url(account_id) -> CRM web page
            "active_tier": store.active_tier(),
            "queue_order": compare.QUEUE_ORDER,
            "tier_labels": compare.TIER_LABELS,
        }

    @app.route("/")
    def index():
        items = store.queue()
        groups = {c: [] for c in compare.QUEUE_ORDER}
        for it in items:
            groups.setdefault(it["classification"], []).append(it)
        return render_template("queue.html", groups=groups, done=_recent_done())

    @app.route("/refresh", methods=["POST"])
    def refresh():
        try:
            stats = refresh_from_live()
            flash(f"Refreshed: {stats['new']} new, {stats['refreshed']} updated, "
                  f"{stats['resolved_upstream']} resolved upstream.", "ok")
        except CrmApiError as e:
            flash(f"CRM API error during refresh: {e}", "err")
        except Exception as e:
            flash(f"Refresh failed: {e}", "err")
        return redirect(url_for("index"))

    @app.route("/reset-rehearsed")
    def reset_rehearsed():
        n = store.clear_rehearsed()
        flash(f"Brought {n} rehearsed item(s) back into the queue.", "ok")
        return redirect(url_for("index"))

    @app.route("/item/<path:community_name>")
    def item(community_name):
        it = store.get_item(community_name)
        if it is None:
            abort(404)
        locked = _tier_lock_redirect(it)
        if locked is not None:
            return locked
        it["crm_url"] = account_web_url(it["crm_account_id"]) if it["crm_account_id"] else ""
        match_results = []
        q = request.args.get("match_q", "").strip()
        if q:
            try:
                match_results = CrmClient().search_accounts(q)
            except CrmApiError as e:
                flash(f"Search failed: {e}", "err")

        # position + prev/next within the current tier (the only unlocked one)
        tier = store.active_tier()
        tier_names = [i["community_name"] for i in store.queue()
                      if i["classification"] == tier]
        nav = {}
        if community_name in tier_names:
            pos = tier_names.index(community_name)
            nav = {"pos": pos + 1, "total": len(tier_names),
                   "tier": compare.TIER_LABELS.get(tier, tier)}
            if pos > 0:
                nav["prev"] = tier_names[pos - 1]
            if pos + 1 < len(tier_names):
                nav["next"] = tier_names[pos + 1]

        return render_template(
            "item.html", it=it, match_q=q, match_results=match_results, nav=nav,
            care_types=CARE_TYPES, bellhaven_parent=BELLHAVEN_PARENT_ID,
            status_choices=compare.STATUS_CHOICES,
        )

    @app.route("/item/<path:community_name>/reject", methods=["POST"])
    def reject(community_name):
        it = store.get_item(community_name)
        if it is None:
            abort(404)
        if (locked := _tier_lock_redirect(it)) is not None:
            return locked
        dry = dry_run_enabled()
        store.record_reject(community_name, request.form.get("note", "").strip(), dry_run=dry)
        flash(("[DRY RUN - not saved] Rehearsed reject of " if dry else "Rejected ")
              + community_name + ".", "ok")
        return redirect(_next_url(it))

    @app.route("/item/<path:community_name>/reopen", methods=["POST"])
    def reopen(community_name):
        store.reopen(community_name)
        flash(f"Re-opened {community_name}.", "ok")
        return redirect(url_for("item", community_name=community_name))

    @app.route("/item/<path:community_name>/link", methods=["POST"])
    def link(community_name):
        it = store.get_item(community_name)
        if it is None:
            abort(404)
        if (locked := _tier_lock_redirect(it)) is not None:
            return locked
        account_id = request.form["account_id"]
        confirmed = request.form.get("confirmed") == "1"
        try:
            account = CrmClient().get_account(account_id)
        except CrmApiError as e:
            flash(f"Could not load account {account_id}: {e}", "err")
            return redirect(url_for("item", community_name=community_name))

        scraped = {**it["scraped"], "community_name": community_name}
        diffs, _ = compare.diff_against_account(scraped, account)

        if not diffs:
            store.record_apply(community_name, "link", {"account_id": account_id},
                               [], 200, {"already_matches": True},
                               new_account_id=account_id, dry_run=dry_run_enabled())
            flash(("[DRY RUN - nothing sent] " if dry_run_enabled() else "") +
                  f"Linked to {account.get('name')} - every field already matches.", "ok")
            return redirect(_next_url(it))

        body = compare.build_patch_body(diffs)

        if not confirmed:
            # Linking means "this account IS this community" -> apply every diff
            # (the website is authoritative) right here, not via the needs-update
            # queue. Nothing is persisted until the reviewer confirms.
            return render_template(
                "confirm_link.html", it=it, account=account, account_id=account_id,
                diffs=diffs, body=body,
            )

        client = CrmClient()
        try:
            resp = client.patch_account(account_id, body)
        except CrmApiError as e:
            store.record_error(community_name, "link", body, e.status, e.body)
            flash(f"CRM rejected the update ({e.status}): {e.body}", "err")
            return redirect(url_for("item", community_name=community_name))

        store.record_apply(community_name, "link",
                           {"account_id": account_id, "patch": body},
                           [d.field for d in diffs], 200, resp,
                           new_account_id=account_id, dry_run=dry_run_enabled())
        flash(("[DRY RUN - nothing sent] " if dry_run_enabled() else "") +
              f"Linked to {account.get('name')} and updated "
              f"{', '.join(d.field for d in diffs)}.", "ok")
        return redirect(_next_url(it))

    @app.route("/item/<path:community_name>/apply", methods=["POST"])
    def apply(community_name):
        it = store.get_item(community_name)
        if it is None:
            abort(404)
        if (locked := _tier_lock_redirect(it)) is not None:
            return locked
        mode = request.form.get("mode", "patch")
        confirmed = request.form.get("confirmed") == "1"

        if mode == "create":
            return _handle_create(it, community_name, confirmed)
        if mode == "status":
            return _handle_status(it, community_name, confirmed)
        return _handle_patch(it, community_name, confirmed)

    @app.route("/item/<path:community_name>/chow", methods=["POST"])
    def chow(community_name):
        it = store.get_item(community_name)
        if it is None or it["classification"] != "chow":
            abort(404)
        if (locked := _tier_lock_redirect(it)) is not None:
            return locked
        return _handle_chow(it, community_name, request.form.get("confirmed") == "1")

    @app.route("/item/<path:community_name>/dedupe", methods=["POST"])
    def dedupe(community_name):
        it = store.get_item(community_name)
        if it is None or it["classification"] != "duplicate":
            abort(404)
        if (locked := _tier_lock_redirect(it)) is not None:
            return locked
        return _handle_dedupe(it, community_name)

    @app.route("/audit")
    def audit():
        return render_template("audit.html", rows=store.audit(), items=store.all_items())

    return app


# --- apply handlers ---------------------------------------------------

def _diffs_from_item(it: Dict) -> Dict[str, compare.FieldDiff]:
    out = {}
    for p in it["proposals"]:
        out[p["field"]] = compare.FieldDiff(
            p["field"], p["scraped_value"], p["crm_value"], p["care_candidates"]
        )
    return out


def _handle_patch(it: Dict, community_name: str, confirmed: bool):
    diffs = _diffs_from_item(it)
    decisions = {
        f: ("approved" if request.form.get(f"decision__{f}") == "approve" else "rejected")
        for f in diffs
    }
    care_choice = request.form.get("care_choice") or None
    approved = [diffs[f] for f, d in decisions.items() if d == "approved"]

    if not approved:
        flash("Nothing approved - use Reject if none of the changes are right.", "err")
        return redirect(url_for("item", community_name=community_name))

    body = compare.build_patch_body(approved, care_choice)

    if not confirmed:
        return render_template(
            "confirm.html", it=it, mode="patch",
            method="PATCH", target=f"/api/v1/accounts/{it['crm_account_id']}",
            payload=body, decisions=decisions, care_choice=care_choice,
        )

    store.set_field_decisions(community_name, decisions)
    client = CrmClient()
    # Guard against a concurrent CRM edit since the last comparison.
    try:
        fresh = client.get_account(it["crm_account_id"])
        stored_updated = it.get("crm_updated_at") or ""
        if stored_updated and fresh.get("updated_at") and fresh["updated_at"] != stored_updated:
            fresh_diffs, _ = compare.diff_against_account(
                {**it["scraped"], "community_name": community_name}, fresh
            )
            store.sync_results([compare.Result(
                community_name=community_name, classification="needs update" if fresh_diffs else "good",
                scraped={**it["scraped"], "community_name": community_name},
                crm_account_id=it["crm_account_id"], crm_account_name=fresh.get("name", ""),
                crm_view=compare.crm_view(fresh), field_diffs=fresh_diffs,
                details="CRM changed since last comparison - re-review",
            )], sweep_absent=False)
            flash("The CRM account changed since the last comparison. Re-review the refreshed diff.", "err")
            return redirect(url_for("item", community_name=community_name))
    except CrmApiError as e:
        flash(f"Could not re-check the account before writing: {e}", "err")
        return redirect(url_for("item", community_name=community_name))

    try:
        resp = client.patch_account(it["crm_account_id"], body)
        store.record_apply(community_name, "patch", body, list(
            f for f, d in decisions.items() if d == "approved"), 200, resp,
            dry_run=dry_run_enabled())
        flash(("[DRY RUN - nothing sent] " if dry_run_enabled() else "") +
              f"Applied {len(approved)} field change(s) to {it['crm_account_name']}.", "ok")
        return redirect(_next_url(it))
    except CrmApiError as e:
        store.record_error(community_name, "patch", body, e.status, e.body)
        flash(f"CRM rejected the update ({e.status}): {e.body}", "err")
        return redirect(url_for("item", community_name=community_name))


def _handle_create(it: Dict, community_name: str, confirmed: bool):
    values = {
        "name": request.form.get("name", "").strip(),
        "address": request.form.get("address", "").strip(),
        "city": request.form.get("city", "").strip(),
        "state": request.form.get("state", "").strip(),
        "zip": request.form.get("zip", "").strip(),
    }
    care_choice = request.form.get("care_choice", "").strip()
    status = request.form.get("status", "Active").strip() or "Active"
    parent_id = request.form.get("parent_id", "").strip()

    body = compare.build_create_body(values, care_choice, parent_id, status)

    if not body.get("name"):
        flash("A name is required to create an account.", "err")
        return redirect(url_for("item", community_name=community_name))

    if not confirmed:
        return render_template(
            "confirm.html", it=it, mode="create",
            method="POST", target="/api/v1/accounts", payload=body,
            decisions={}, care_choice=care_choice,
        )

    try:
        resp = CrmClient().create_account(body)
        new_id = resp.get("account_id") if isinstance(resp, dict) else None
        store.record_apply(community_name, "create", body, list(values), 201, resp,
                           new_account_id=None if dry_run_enabled() else new_id,
                           dry_run=dry_run_enabled())
        flash(("[DRY RUN - nothing sent] " if dry_run_enabled() else "") +
              f"Created CRM account for {values['name']}"
              + (f" ({new_id})" if new_id and not dry_run_enabled() else "") + ".", "ok")
        return redirect(_next_url(it))
    except CrmApiError as e:
        store.record_error(community_name, "create", body, e.status, e.body)
        flash(f"CRM rejected the create ({e.status}): {e.body}", "err")
        return redirect(url_for("item", community_name=community_name))


def _handle_status(it: Dict, community_name: str, confirmed: bool):
    """Set the `status` field on a Bellhaven-parented CRM account that has no
    matching community on the website."""
    account_id = it["crm_account_id"]
    current = (it["crm_view"] or {}).get("status", "")
    new_status = request.form.get("status", "").strip() or compare.STATUS_CHOICES[0]
    if compare.norm_generic(new_status) == compare.norm_generic(current):
        flash(f"Status is already '{current}'.", "err")
        return redirect(url_for("item", community_name=community_name))

    body = {"status": new_status}
    if not confirmed:
        return render_template(
            "confirm.html", it=it, mode="status",
            method="PATCH", target=f"/api/v1/accounts/{account_id}",
            payload=body, decisions={}, care_choice=None,
        )

    try:
        resp = CrmClient().patch_account(account_id, body)
        store.record_apply(community_name, "status", body, ["status"], 200, resp,
                           dry_run=dry_run_enabled())
        flash(("[DRY RUN - nothing sent] " if dry_run_enabled() else "") +
              f"Set status of {it['crm_account_name']} ({account_id}) to '{new_status}'.", "ok")
        return redirect(_next_url(it))
    except CrmApiError as e:
        store.record_error(community_name, "status", body, e.status, e.body)
        flash(f"CRM rejected the status change ({e.status}): {e.body}", "err")
        return redirect(url_for("item", community_name=community_name))


def _handle_chow(it: Dict, community_name: str, confirmed: bool):
    """Create a fresh, correctly-parented account from the scraped data, then
    automatically point the money-carrying old account at it via
    chow_current_account. The old account is never otherwise modified."""
    old_id = it["crm_account_id"]
    crm = it["crm_view"] or {}
    values = {
        "name": request.form.get("name", "").strip() or community_name,
        "address": request.form.get("address", "").strip(),
        "city": request.form.get("city", "").strip(),
        "state": request.form.get("state", "").strip(),
        "zip": request.form.get("zip", "").strip(),
    }
    care_choice = request.form.get("care_choice", "").strip()
    create_body = compare.build_create_body(
        values, care_choice, BELLHAVEN_PARENT_ID, status="Active"
    )

    if not confirmed:
        return render_template(
            "confirm_chow.html", it=it, old_id=old_id, crm=crm,
            create_body=create_body, care_choice=care_choice,
        )

    client = CrmClient()
    # Step 1: create the new account.
    try:
        resp = client.create_account(create_body)
    except CrmApiError as e:
        store.record_error(community_name, "chow-create", create_body, e.status, e.body)
        flash(f"CRM rejected the new account ({e.status}): {e.body}", "err")
        return redirect(url_for("item", community_name=community_name))

    new_id = resp.get("account_id") if isinstance(resp, dict) else None
    if dry_run_enabled() and not new_id:
        new_id = "<new-account-id>"

    # Step 2 (automatic): CHOW-link the old account to the new one.
    chow_body = {"chow_current_account": new_id}
    try:
        chow_resp = client.patch_account(old_id, chow_body)
    except CrmApiError as e:
        store.record_error(
            community_name, "chow-link",
            {"created_account": new_id, "create_body": create_body,
             "chow_target": old_id, "chow_body": chow_body},
            e.status, e.body,
        )
        flash(f"New account was created ({new_id}) but the CHOW link failed "
              f"({e.status}): {e.body}. Set chow_current_account on {old_id} manually.", "err")
        return redirect(url_for("item", community_name=community_name))

    store.record_apply(
        community_name, "chow",
        {"create": create_body, "chow": {"account_id": old_id, **chow_body}},
        [p["field"] for p in it["proposals"]], 200,
        {"created": resp, "chow": chow_resp},
        new_account_id=None if dry_run_enabled() else new_id,
        dry_run=dry_run_enabled(),
    )
    if dry_run_enabled():
        flash(f"[DRY RUN - nothing sent] Would create {values['name']} and set "
              f"chow_current_account on the old account ({old_id}).", "ok")
    else:
        flash(f"Created {values['name']}" + (f" ({new_id})" if new_id else "") +
              f" and set chow_current_account on the old account ({old_id}).", "ok")
    return redirect(_next_url(it))


def _handle_dedupe(it: Dict, community_name: str):
    """Resolve a possible-duplicate group. Either 'active' (pick one account;
    the rest get duplicate_of_account + Inactive) or 'needs_review' (every
    account in the group -> status 'Needs Review')."""
    group = {m["account_id"]: m for m in it["possible_matches"]}
    mode = request.form.get("mode", "")
    confirmed = request.form.get("confirmed") == "1"
    primary_id = request.form.get("primary_id", "")

    if mode == "active":
        if primary_id not in group:
            flash("Pick which account to keep as the active one.", "err")
            return redirect(url_for("item", community_name=community_name))
        ops = [
            (aid, {"duplicate_of_account": primary_id, "status": "Inactive"})
            for aid in group if aid != primary_id
        ]
        summary = (f"keep {group[primary_id]['name']} ({primary_id}) active; "
                   f"mark {len(ops)} other(s) duplicate_of {primary_id} + Inactive")
    elif mode == "needs_review":
        ops = [(aid, {"status": "Needs Review"}) for aid in group]
        summary = f"set all {len(ops)} account(s) to status 'Needs Review'"
    else:
        abort(400)

    if not confirmed:
        return render_template("confirm_dedupe.html", it=it, mode=mode,
                               primary_id=primary_id, ops=ops, summary=summary)

    client = CrmClient()
    ok, failed = [], []
    for aid, body in ops:
        try:
            ok.append({aid: client.patch_account(aid, body)})
        except CrmApiError as e:
            failed.append({aid: {"status": e.status, "body": e.body}})

    payload = {"mode": mode, "primary_id": primary_id, "ops": [dict(a=a, body=b) for a, b in ops]}
    if failed:
        store.record_error(community_name, f"dedupe-{mode}", payload, None,
                           {"ok": ok, "failed": failed})
        flash(f"Some updates failed; the group stays open: {failed}", "err")
        return redirect(url_for("item", community_name=community_name))

    store.record_apply(community_name, f"dedupe-{mode}", payload, [], 200,
                       {"ok": ok}, dry_run=dry_run_enabled())
    flash(("[DRY RUN - nothing sent] " if dry_run_enabled() else "") + summary + ".", "ok")
    return redirect(_next_url(it))


def _recent_done() -> List[Dict]:
    return [i for i in store.all_items()
            if i["status"] in ("applied", "rejected", "error", "rehearsed")][:15]


_CLASS_ORDER = {cls: i for i, cls in enumerate(compare.QUEUE_ORDER)}


def _queue_sort_key(entry: Dict):
    return (_CLASS_ORDER.get(entry["classification"], len(compare.QUEUE_ORDER)),
            entry["community_name"])


def _tier_lock_redirect(it: Dict):
    """The queue must be worked in QUEUE_ORDER. If `it` is a pending item in a
    tier that isn't the active one, bounce back to the queue."""
    if it["status"] not in ("pending", "error"):
        return None  # already-decided items can always be viewed / re-opened
    tier = store.active_tier()
    if tier is None or it["classification"] == tier:
        return None
    flash(f"Locked - finish the '{compare.TIER_LABELS.get(tier, tier)}' items "
          "first; this tier unlocks after that.", "err")
    return redirect(url_for("index"))


def _next_url(current_item: Optional[Dict]) -> str:
    """After acting on `current_item`, the URL of the next item to review -
    stays within the active tier and only moves on once it is cleared."""
    remaining = store.queue()
    if not remaining:
        flash("Queue is clear - nothing left to review.", "ok")
        return url_for("index")
    tier = store.active_tier()
    pool = [e for e in remaining if e["classification"] == tier] or remaining
    if current_item:
        key = _queue_sort_key(current_item)
        after = [e for e in pool if _queue_sort_key(e) > key]
        nxt = (after or pool)[0]
    else:
        nxt = pool[0]
    return url_for("item", community_name=nxt["community_name"])


# --- entrypoint ---------------------------------------------------

def _start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(daemon=True)
    # Daily, offset from the job-digest action (13:00 UTC).
    sched.add_job(lambda: _safe_refresh(), "cron", hour=13, minute=35, id="daily-refresh")
    sched.start()
    log.info("scheduler started: daily refresh at 13:35 UTC")
    return sched


def _safe_refresh():
    try:
        refresh_from_live()
    except Exception as e:  # pragma: no cover
        log.error("scheduled refresh failed: %s", e)


def main():
    app = create_app()
    reset = store.clear_rehearsed()
    if reset:
        log.info("reset %d dry-run 'rehearsed' item(s) back to pending", reset)
    try:
        import_snapshot_if_fresh()
    except Exception as e:
        log.warning("snapshot import skipped: %s", e)
    if os.environ.get("CRM_SYNC_NO_SCHEDULER") != "1":
        _start_scheduler()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
