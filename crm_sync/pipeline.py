"""Run the daily comparison: scrape the community site, pull CRM accounts,
classify each community.

CLI (used by developers and by the GitHub Action):

    python -m crm_sync.pipeline                       # print summary
    python -m crm_sync.pipeline --csv crm_comparison.csv
    python -m crm_sync.pipeline --write-snapshot data/crm_snapshot.json

The snapshot is committed by CI so the review app has a fresh queue even if
it was not running. Nothing here ever writes to the CRM.
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

import scrape_communities as scraper

from . import compare
from .crm_api import CrmClient

CSV_FIELDNAMES = [
    "community_name",
    "classification",
    "mismatched_fields",
    "crm_account_name",
    "crm_account_id",
    "name_match_score",
    "scraped_address", "crm_address",
    "scraped_city", "crm_city",
    "scraped_state", "crm_state",
    "scraped_zip", "crm_zip",
    "scraped_care_offerings", "crm_care_type",
    "crm_parent_name", "crm_parent_ok",
    "crm_lifetime_revenue", "crm_outstanding_ar", "crm_chow_current_account",
    "possible_crm_match",
    "possible_match_details",
    "details",
]


def scrape_community_rows(progress=lambda msg: None) -> List[Dict]:
    session = scraper.make_session()
    progress("scraping community site...")
    paths = scraper.collect_community_paths(session)
    rows = []
    for i, path in enumerate(paths, 1):
        soup = scraper.get_soup(session, f"{scraper.BASE_URL}{path}")
        rows.append(scraper.parse_detail(soup))
        progress(f"  community {i}/{len(paths)}")
    return rows


def run_comparison(token: Optional[str] = None, progress=lambda msg: None) -> List[compare.Result]:
    scraped = scrape_community_rows(progress)
    client = CrmClient(token)
    progress("fetching CRM accounts...")
    accounts = client.list_accounts()
    progress(f"  {len(accounts)} CRM accounts")
    return compare.classify(scraped, accounts)


# --- serialisation -----------------------------------------------------

def result_to_csv_row(r: compare.Result) -> Dict[str, str]:
    crm = r.crm_view or {}
    return {
        "community_name": r.community_name,
        "classification": r.classification,
        "mismatched_fields": ", ".join(r.mismatched_fields) if r.classification == "needs update" else "",
        "crm_account_name": r.crm_account_name,
        "crm_account_id": r.crm_account_id,
        "name_match_score": f"{r.name_match_score:.2f}" if r.name_match_score else "",
        "scraped_address": r.scraped.get("address", ""),
        "crm_address": crm.get("address", ""),
        "scraped_city": r.scraped.get("city", ""),
        "crm_city": crm.get("city", ""),
        "scraped_state": r.scraped.get("state", ""),
        "crm_state": crm.get("state", ""),
        "scraped_zip": r.scraped.get("zip", ""),
        "crm_zip": crm.get("zip", ""),
        "scraped_care_offerings": r.scraped.get("care_offerings", ""),
        "crm_care_type": crm.get("care_type", ""),
        "crm_parent_name": crm.get("parent_name", ""),
        "crm_parent_ok": "" if not crm else (
            "yes" if crm.get("parent_id") == compare.BELLHAVEN_PARENT_ID else "NO"),
        "crm_lifetime_revenue": f"{crm['lifetime_revenue']:.0f}" if crm else "",
        "crm_outstanding_ar": f"{crm['outstanding_ar']:.0f}" if crm else "",
        "crm_chow_current_account": crm.get("chow_current_account", ""),
        "possible_crm_match": r.possible_crm_match,
        "possible_match_details": _format_possible_matches(r.possible_matches),
        "details": r.details,
    }


def _format_possible_matches(matches: List[Dict]) -> str:
    """One human-readable line per candidate account for the CSV."""
    out = []
    for m in matches:
        extra = ""
        if "match_count" in m:
            extra = f", {m['match_count']}/6 fields match, status {m.get('status', '?')}"
        out.append(
            f"{m['name']} | {m['address']}, {m['city']} {m['state']} {m['zip']} "
            f"| {m['care_type'] or 'no care type'} "
            f"| parent: {m['parent_name'] or m['parent_id'] or '—'} "
            f"| id {m['account_id']} ({', '.join(m['reasons'])}{extra})"
        )
    return " ;; ".join(out)


def write_csv(results: List[compare.Result], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for r in results:
            writer.writerow(result_to_csv_row(r))


def result_to_dict(r: compare.Result) -> Dict:
    return {
        "community_name": r.community_name,
        "classification": r.classification,
        "scraped": r.scraped,
        "crm_account_id": r.crm_account_id,
        "crm_account_name": r.crm_account_name,
        "crm_view": r.crm_view,
        "name_match_score": round(r.name_match_score, 4),
        "possible_crm_match": r.possible_crm_match,
        "possible_matches": r.possible_matches,
        "details": r.details,
        "name_collision": r.name_collision,
        "field_diffs": [dataclasses.asdict(d) for d in r.field_diffs],
    }


def _slim(d: Dict) -> Dict:
    """Good rows carry no actionable content - keep only what sync needs."""
    if d["classification"] == "good":
        return {"community_name": d["community_name"], "classification": "good"}
    return d


def write_snapshot(results: List[compare.Result], path: str) -> None:
    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "counts": summary_counts(results),
        "results": [_slim(result_to_dict(r)) for r in results],
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def result_from_dict(d: Dict) -> compare.Result:
    diffs = [compare.FieldDiff(**fd) for fd in d.get("field_diffs", [])]
    return compare.Result(
        community_name=d["community_name"],
        classification=d["classification"],
        scraped=d.get("scraped", {}) or {},
        crm_account_id=d.get("crm_account_id", "") or "",
        crm_account_name=d.get("crm_account_name", "") or "",
        crm_view=d.get("crm_view", {}) or {},
        name_match_score=d.get("name_match_score", 0.0) or 0.0,
        possible_crm_match=d.get("possible_crm_match", "") or "",
        possible_matches=d.get("possible_matches", []) or [],
        details=d.get("details", "") or "",
        name_collision=bool(d.get("name_collision")),
        field_diffs=diffs,
    )


def load_snapshot(path: str) -> Dict:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    payload["result_objects"] = [result_from_dict(d) for d in payload.get("results", [])]
    return payload


def summary_counts(results: List[compare.Result]) -> Dict[str, int]:
    counts = {"good": 0, "needs update": 0, "chow": 0, "missing": 0,
              "stale": 0, "duplicate": 0}
    for r in results:
        counts[r.classification] = counts.get(r.classification, 0) + 1
    return counts


def print_summary(results: List[compare.Result]) -> None:
    counts = summary_counts(results)
    print("\n=== Summary ===", file=sys.stderr)
    for cls in ("good", "needs update", "chow", "missing", "stale", "duplicate"):
        print(f"  {cls:13} {counts.get(cls, 0)}", file=sys.stderr)
    for r in results:
        if r.classification == "good":
            continue
        line = f"  [{r.classification}] {r.community_name}"
        if r.classification == "needs update":
            line += f"  -> {', '.join(r.mismatched_fields)}  (CRM: {r.crm_account_name})"
        elif r.classification == "chow":
            line += (f"  (CRM: {r.crm_account_name}, parent "
                     f"{r.crm_view.get('parent_name', '?')}, "
                     f"rev {r.crm_view.get('lifetime_revenue', 0):.0f})")
        elif r.classification == "stale":
            line += (f"  (CRM account {r.crm_account_id}, status "
                     f"{r.crm_view.get('status', '?')} -> Inactive)")
        elif r.classification == "duplicate":
            line += f"  ({len(r.possible_matches)} accounts)"
        elif r.possible_crm_match:
            line += f"  ({r.possible_crm_match.split(' | ')[0]})"
        print(line, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", nargs="?", const="crm_comparison.csv", default=None,
                        help="write the full comparison to this CSV (default name: crm_comparison.csv)")
    parser.add_argument("--write-snapshot", default=None,
                        help="write a JSON snapshot of non-matching results (for CI)")
    parser.add_argument("--token", default=None, help="CRM API token (else CRM_API_TOKEN)")
    args = parser.parse_args(argv)

    results = run_comparison(args.token, progress=lambda m: print(m, file=sys.stderr))
    print_summary(results)

    if args.csv:
        write_csv(results, args.csv)
        print(f"\nWrote {args.csv}", file=sys.stderr)
    if args.write_snapshot:
        write_snapshot(results, args.write_snapshot)
        print(f"Wrote snapshot {args.write_snapshot}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
