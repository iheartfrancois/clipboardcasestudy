"""Normalise, match and classify scraped communities against CRM accounts.

Classification per scraped community:

    good          - a CRM account matches by name and every compared field agrees
    needs update  - a CRM account matches by name but one or more fields differ
    missing       - no CRM account has that name

Compared fields: name, address, city, state, zip, care offerings.

This module is pure (no network). ``pipeline.run_comparison`` feeds it the
scraped rows and the CRM accounts (from ``crm_api``).
"""
from __future__ import annotations

import difflib
import html
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .crm_api import BELLHAVEN_PARENT_ID

# --- taxonomy -------------------------------------------------------------

# Scraped care taxonomy -> CRM "Care Type" taxonomy.
CARE_MAP = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "short term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}

# Fuzzy name-match threshold. At/above this a CRM account is treated as the same
# entity (with "name" flagged if the spellings differ); below it -> missing.
NAME_MATCH_THRESHOLD = 0.86

# The queue must be worked in this order - a tier unlocks only once the tier
# above it has no pending items left.
QUEUE_ORDER = ["needs update", "chow", "missing", "stale", "duplicate"]
TIER_LABELS = {
    "needs update": "needs update",
    "chow": "change of ownership",
    "missing": "missing",
    "stale": "no website match",
    "duplicate": "possible duplicates",
}

# Fields prefilled onto the "create a new account" form.
COMPARE_FIELDS = ["name", "address", "city", "state", "zip", "care offerings"]

# Compared field -> CRM PATCH field name. `parent_id` is compared too but only
# ever set to Bellhaven Senior Living (see target_crm_value).
PATCH_FIELD_MAP = {
    "name": "name",
    "address": "billing_street",
    "city": "billing_city",
    "state": "billing_state",
    "zip": "billing_zip",
    "care offerings": "care_type",
    "parent_id": "parent_id",
    "status": "status",
}

BELLHAVEN_PARENT_LABEL = "Bellhaven Senior Living (Parent Account)"

# --- normalisation helpers --------------------------------------------------

_STREET_WORDS = {
    "st": "street", "st.": "street",
    "ave": "avenue", "ave.": "avenue", "av": "avenue",
    "blvd": "boulevard", "blvd.": "boulevard",
    "rd": "road", "rd.": "road",
    "dr": "drive", "dr.": "drive",
    "ln": "lane", "ln.": "lane",
    "ct": "court", "ct.": "court",
    "cir": "circle", "cir.": "circle",
    "pkwy": "parkway", "pkwy.": "parkway",
    "pike": "pike", "pk": "pike", "pk.": "pike",
    "hwy": "highway", "hwy.": "highway",
    "sq": "square", "sq.": "square",
    "ter": "terrace", "ter.": "terrace", "terr": "terrace",
    "pl": "place", "pl.": "place",
    "pt": "point", "pt.": "point",
    "trl": "trail", "trl.": "trail",
    "cv": "cove", "cv.": "cove",
    "byp": "bypass", "byp.": "bypass",
    "n": "north", "s": "south", "e": "east", "w": "west",
    "nw": "northwest", "ne": "northeast", "sw": "southwest", "se": "southeast",
    "ste": "suite", "ste.": "suite", "#": "suite",
}

# Word-level synonyms applied only when *matching* names (not when diffing them).
_NAME_SYNONYMS = {
    "rehab": "rehabilitation",
    "rehabilitation": "rehabilitation",
    "healthcare": "health care",
    "centre": "center",
    "&": "and",
}


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def norm_generic(text: str) -> str:
    """Case/punctuation/whitespace-insensitive form for equality checks."""
    text = html.unescape(text or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[.,/#]", " ", text)
    text = text.replace("-", " ")
    return _collapse(text)


def norm_address(text: str) -> str:
    return " ".join(_STREET_WORDS.get(tok, tok) for tok in norm_generic(text).split())


def norm_name_for_compare(name: str) -> str:
    """Formatting-only normalisation (case, punctuation, 'the', '&'/'and').

    Used to decide whether the *name field* matches. Abbreviation differences
    ("Rehab" vs "Rehabilitation", "at" vs "of") are preserved and so flagged.
    """
    text = norm_generic(name)
    if text.startswith("the "):
        text = text[4:]
    return _collapse(text.replace(" (parent account)", ""))


def norm_name_for_match(name: str) -> str:
    """Aggressive normalisation (adds word synonyms), used only to locate the
    CRM record that corresponds to a scraped community."""
    text = norm_name_for_compare(name)
    return _collapse(" ".join(_NAME_SYNONYMS.get(tok, tok) for tok in text.split()))


def map_care(scraped_care: str) -> List[str]:
    """CRM care types implied by a scraped care-offerings cell (order-preserving)."""
    out: List[str] = []
    for chunk in re.split(r"[;,/]| and ", scraped_care or ""):
        key = _collapse(chunk).lower()
        if not key:
            continue
        mapped = CARE_MAP.get(key, chunk.strip())
        if mapped not in out:
            out.append(mapped)
    return out


# --- CRM account adapter --------------------------------------------------

def _num(v) -> float:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def crm_view(account: Dict) -> Dict:
    """Normalise a CRM API account dict to the field names used for comparison."""
    return {
        "account_id": account.get("account_id", ""),
        "name": account.get("name", "") or "",
        "address": account.get("billing_street", "") or "",
        "city": account.get("billing_city", "") or "",
        "state": account.get("billing_state", "") or "",
        "zip": account.get("billing_zip", "") or "",
        "care_type": account.get("care_type", "") or "",
        "status": account.get("status", "") or "",
        "parent_id": account.get("parent_id", "") or "",
        "parent_name": account.get("parent_name", "") or "",
        "lifetime_revenue": _num(account.get("lifetime_revenue")),
        "outstanding_ar": _num(account.get("outstanding_ar")),
        "chow_current_account": account.get("chow_current_account", "") or "",
        "duplicate_of_account": account.get("duplicate_of_account", "") or "",
        "updated_at": account.get("updated_at", "") or "",
    }


def parent_is_bellhaven(v: Dict) -> bool:
    return v.get("parent_id") == BELLHAVEN_PARENT_ID


def has_financials(v: Dict) -> bool:
    return v.get("lifetime_revenue", 0) > 0 or v.get("outstanding_ar", 0) > 0


# --- data model ---------------------------------------------------------

@dataclass
class FieldDiff:
    field: str
    scraped_value: str
    crm_value: str
    care_candidates: Optional[List[str]] = None  # only set for "care offerings"


@dataclass
class Result:
    community_name: str
    classification: str  # good | needs update | missing
    scraped: Dict = field(default_factory=dict)
    crm_account_id: str = ""
    crm_account_name: str = ""
    crm_view: Dict = field(default_factory=dict)
    name_match_score: float = 0.0
    possible_crm_match: str = ""          # short one-line summary
    possible_matches: List[Dict] = field(default_factory=list)  # full candidate accounts
    details: str = ""
    field_diffs: List[FieldDiff] = field(default_factory=list)
    name_collision: bool = False   # same name, but a completely different place

    @property
    def mismatched_fields(self) -> List[str]:
        return [d.field for d in self.field_diffs]


# --- matching ----------------------------------------------------------

def _find_match(scraped_name, crm_views, crm_index) -> Tuple[Optional[Dict], float, bool, Optional[Dict]]:
    """Return (crm_view_or_None, score, exact_bool, best_candidate_view)."""
    key = norm_name_for_match(scraped_name)
    if key in crm_index:
        return crm_index[key], 1.0, True, crm_index[key]

    best, best_score = None, 0.0
    for row in crm_views:
        score = difflib.SequenceMatcher(None, key, norm_name_for_match(row["name"])).ratio()
        if score > best_score:
            best, best_score = row, score
    if best is not None and best_score >= NAME_MATCH_THRESHOLD:
        return best, best_score, False, best
    return None, best_score, False, best


def _same_location_accounts(scraped, crm_views):
    want = (norm_generic(scraped["city"]), norm_generic(scraped["state"]))
    if not want[0]:
        return []
    return [
        r for r in crm_views
        if (norm_generic(r["city"]), norm_generic(r["state"])) == want
    ]


def _hint(candidate, score, same_loc):
    bits = []
    if candidate is not None and score >= 0.60:
        bits.append(
            f"closest name: {candidate['name']} "
            f"({candidate['city']}, {candidate['state']}) ~{score:.2f}"
        )
    others = [
        f"{r['name']} ({r['care_type'] or 'no care type'})"
        for r in same_loc
        if candidate is None or r["account_id"] != candidate["account_id"]
    ]
    if others:
        bits.append("same city/state in CRM: " + "; ".join(others))
    return " | ".join(bits)


def _possible_matches(candidate, score, same_loc) -> List[Dict]:
    """Full CRM account records that might be the same place as a `missing`
    community: the closest name match plus any account in the same city/state."""
    out: Dict[str, Dict] = {}

    def add(v: Dict, reason: str):
        entry = out.setdefault(
            v["account_id"],
            {
                "account_id": v["account_id"],
                "name": v["name"],
                "address": v["address"],
                "city": v["city"],
                "state": v["state"],
                "zip": v["zip"],
                "care_type": v["care_type"],
                "parent_id": v["parent_id"],
                "parent_name": v["parent_name"],
                "reasons": [],
            },
        )
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    if candidate is not None and score >= 0.60:
        add(candidate, f"closest name (~{score:.2f})")
    for r in same_loc:
        add(r, "same city & state")
    return list(out.values())


# fields lined up target-vs-candidate when comparing two CRM accounts
_CRM_COMPARE_FIELDS = [
    ("name", lambda v: v["name"], norm_name_for_compare),
    ("address", lambda v: v["address"], norm_address),
    ("city", lambda v: v["city"], norm_generic),
    ("state", lambda v: v["state"], norm_generic),
    ("zip", lambda v: v["zip"], lambda z: (z or "").split("-")[0]),
    ("care type", lambda v: v["care_type"], norm_generic),
]


def _crm_field_comparison(target: Dict, cand: Dict) -> Tuple[List[Dict], int]:
    rows, matches = [], 0
    for field, get, norm in _CRM_COMPARE_FIELDS:
        tv, cv = get(target), get(cand)
        ok = norm(tv) == norm(cv) and norm(tv) != ""
        matches += ok
        rows.append({"field": field, "target": tv or "", "candidate": cv or "", "match": ok})
    return rows, matches


def crm_match_candidates(target: Dict, crm_views: List[Dict]) -> List[Dict]:
    """Other CRM accounts that might be the same community as `target`
    (a Bellhaven-parented account with no website match), with a field-by-field
    comparison so the reviewer can judge before changing the status.

    Signals: same normalised address, same city + state, or a close name."""
    t_addr, t_city, t_state = (
        norm_address(target["address"]),
        norm_generic(target["city"]),
        norm_generic(target["state"]),
    )
    t_key = norm_name_for_match(target["name"])

    out = []
    for v in crm_views:
        if v["account_id"] == target["account_id"]:
            continue
        if v["name"].strip().lower().endswith("(parent account)"):
            continue
        reasons = []
        if t_addr and norm_address(v["address"]) == t_addr:
            reasons.append("same address")
        if t_city and (norm_generic(v["city"]), norm_generic(v["state"])) == (t_city, t_state):
            reasons.append("same city & state")
        score = difflib.SequenceMatcher(None, t_key, norm_name_for_match(v["name"])).ratio()
        if score >= NAME_MATCH_THRESHOLD:
            reasons.append(f"similar name (~{score:.2f})")
        if not reasons:
            continue
        rows, match_count = _crm_field_comparison(target, v)
        out.append({
            "account_id": v["account_id"],
            "name": v["name"],
            "address": v["address"],
            "city": v["city"],
            "state": v["state"],
            "zip": v["zip"],
            "care_type": v["care_type"],
            "parent_id": v["parent_id"],
            "parent_name": v["parent_name"],
            "status": v["status"],
            "lifetime_revenue": v["lifetime_revenue"],
            "outstanding_ar": v["outstanding_ar"],
            "reasons": reasons,
            "field_comparison": rows,
            "match_count": match_count,
        })
    return sorted(out, key=lambda c: (-c["match_count"], c["name"]))


# --- field comparison ------------------------------------------------

def diff_fields(scraped: Dict, crm: Dict) -> Tuple[List[FieldDiff], List[str]]:
    """Return (field_diffs, notes) for a matched scraped/CRM pair."""
    diffs: List[FieldDiff] = []
    notes: List[str] = []

    if norm_name_for_compare(scraped["community_name"]) != norm_name_for_compare(crm["name"]):
        diffs.append(FieldDiff("name", scraped["community_name"], crm["name"]))

    if norm_address(scraped["address"]) != norm_address(crm["address"]):
        diffs.append(FieldDiff("address", scraped["address"], crm["address"]))

    if norm_generic(scraped["city"]) != norm_generic(crm["city"]):
        diffs.append(FieldDiff("city", scraped["city"], crm["city"]))

    if norm_generic(scraped["state"]) != norm_generic(crm["state"]):
        diffs.append(FieldDiff("state", scraped["state"], crm["state"]))

    scraped_zip = (scraped["zip"] or "").split("-")[0]
    crm_zip = (crm["zip"] or "").split("-")[0]
    if scraped_zip != crm_zip:
        diffs.append(FieldDiff("zip", scraped["zip"], crm["zip"]))

    mapped = map_care(scraped["care_offerings"])
    crm_care = crm["care_type"]
    if set(mapped) != ({crm_care} if crm_care else set()):
        diffs.append(
            FieldDiff("care offerings", scraped["care_offerings"], crm_care, care_candidates=mapped)
        )
        if crm_care and crm_care in mapped and len(mapped) > 1:
            extras = [c for c in mapped if c != crm_care]
            notes.append(f"CRM Care Type is single-valued; scrape also lists {extras}")
        else:
            notes.append(f"care: scraped {mapped} vs CRM '{crm_care}'")

    return diffs, notes


def _all_fields_for_create(scraped: Dict) -> List[FieldDiff]:
    mapped = map_care(scraped["care_offerings"])
    return [
        FieldDiff("name", scraped["community_name"], ""),
        FieldDiff("address", scraped["address"], ""),
        FieldDiff("city", scraped["city"], ""),
        FieldDiff("state", scraped["state"], ""),
        FieldDiff("zip", scraped["zip"], ""),
        FieldDiff("care offerings", scraped["care_offerings"], "", care_candidates=mapped),
    ]


# --- top-level classify ------------------------------------------------

def _index_priority(v: Dict) -> Tuple[int, int]:
    """Lower sorts first: prefer the still-active account (no CHOW pointer) and,
    among those, the one already under the Bellhaven parent."""
    return (
        1 if v["chow_current_account"] else 0,
        0 if parent_is_bellhaven(v) else 1,
    )


def classify(scraped_rows: List[Dict], crm_accounts: List[Dict]) -> List[Result]:
    crm_views = [crm_view(a) for a in crm_accounts]
    crm_index: Dict[str, Dict] = {}
    for v in sorted(crm_views, key=_index_priority):
        crm_index.setdefault(norm_name_for_match(v["name"]), v)

    results: List[Result] = []
    for scraped in scraped_rows:
        crm, score, exact, candidate = _find_match(
            scraped["community_name"], crm_views, crm_index
        )
        same_loc = _same_location_accounts(scraped, crm_views)
        hint = _hint(candidate, score, same_loc)
        matches = _possible_matches(candidate, score, same_loc)

        if crm is None:
            results.append(
                Result(
                    community_name=scraped["community_name"],
                    classification="missing",
                    scraped=scraped,
                    name_match_score=score,
                    possible_crm_match=hint,
                    possible_matches=matches,
                    details="no CRM account with a matching name",
                    field_diffs=_all_fields_for_create(scraped),
                )
            )
            continue

        diffs, notes = diff_fields(scraped, crm)
        fields = {d.field for d in diffs}

        # A fuzzy name match whose city AND state both disagree is almost
        # certainly a different community, not a stale record -> missing.
        if not exact and {"city", "state"} <= fields:
            results.append(
                Result(
                    community_name=scraped["community_name"],
                    classification="missing",
                    scraped=scraped,
                    name_match_score=score,
                    possible_crm_match=hint,
                    possible_matches=matches,
                    details=(
                        f"no exact name match; closest CRM account '{crm['name']}' "
                        f"is a different community ({crm['city']}, {crm['state']})"
                    ),
                    field_diffs=_all_fields_for_create(scraped),
                )
            )
            continue

        # A same-named CRM account whose address, city, state, zip AND parent
        # are *all* different from the scraped data is a coincidental name
        # collision, not our facility -> treat the community as missing (create
        # a new account) rather than trying to update the other one.
        if {"address", "city", "state", "zip"} <= fields and not parent_is_bellhaven(crm):
            results.append(
                Result(
                    community_name=scraped["community_name"],
                    classification="missing",
                    scraped=scraped,
                    name_match_score=score,
                    possible_crm_match=hint,
                    possible_matches=matches,
                    name_collision=True,
                    details=(
                        f"name matches CRM account '{crm['name']}' "
                        f"({crm['city']}, {crm['state']}, parent "
                        f"{crm['parent_name'] or crm['parent_id'] or '-'}) but its "
                        "address, city, state, zip and parent are all different - "
                        "treated as a new community, not an update"
                    ),
                    field_diffs=_all_fields_for_create(scraped),
                )
            )
            continue

        if exact and {"city", "state"} <= fields:
            notes.append(
                "same account name but a completely different location - "
                "verify this is the same entity (possible name collision)"
            )

        # --- parent_id check -------------------------------------------
        # Every scraped location must sit under the Bellhaven Senior Living
        # parent. If it doesn't and the account carries money (lifetime
        # revenue or outstanding AR), we must NOT touch it - instead the
        # reviewer creates a fresh account and we point the old one at it
        # via chow_current_account.
        if not parent_is_bellhaven(crm) and not crm["chow_current_account"]:
            if has_financials(crm):
                results.append(
                    Result(
                        community_name=scraped["community_name"],
                        classification="chow",
                        scraped=scraped,
                        crm_account_id=crm["account_id"],
                        crm_account_name=crm["name"],
                        crm_view=crm,
                        name_match_score=score,
                        possible_crm_match=hint,
                        details=(
                            f"wrong parent ('{crm['parent_name'] or crm['parent_id']}') "
                            f"and the account has financial history "
                            f"(lifetime revenue {crm['lifetime_revenue']:.0f}, "
                            f"outstanding AR {crm['outstanding_ar']:.0f}). "
                            "Create a new account and CHOW-link the old one."
                        ),
                        field_diffs=_all_fields_for_create(scraped),
                    )
                )
                continue
            # No money on the account -> just fix the parent like any field.
            diffs.append(
                FieldDiff("parent_id", BELLHAVEN_PARENT_LABEL,
                          crm["parent_name"] or crm["parent_id"])
            )

        results.append(
            Result(
                community_name=scraped["community_name"],
                classification="good" if not diffs else "needs update",
                scraped=scraped,
                crm_account_id=crm["account_id"],
                crm_account_name=crm["name"],
                crm_view=crm,
                name_match_score=score,
                possible_crm_match=hint,
                details="; ".join(notes),
                field_diffs=diffs,
            )
        )

    results.extend(_stale_bellhaven_accounts(scraped_rows, crm_views, results))
    results.extend(_crm_duplicate_groups(crm_views))
    return results


# Status the reviewer can pick for a stale account (first = default).
STATUS_CHOICES = ["Inactive", "Needs Review", "Active"]


def _stale_bellhaven_accounts(scraped_rows: List[Dict], crm_views: List[Dict],
                              results: List[Result]) -> List[Result]:
    """CRM accounts under the Bellhaven parent whose name has no correspondent
    among the scraped website communities. Likely closed / defunct -> the
    reviewer sets their `status`."""
    scraped_keys = [norm_name_for_match(s["community_name"]) for s in scraped_rows]

    def on_website(crm_name: str) -> bool:
        key = norm_name_for_match(crm_name)
        if key in scraped_keys:
            return True
        return any(
            difflib.SequenceMatcher(None, key, sk).ratio() >= NAME_MATCH_THRESHOLD
            for sk in scraped_keys
        )

    suggested_for: Dict[str, List[str]] = {}
    for r in results:
        if r.classification == "missing":
            for m in r.possible_matches:
                suggested_for.setdefault(m["account_id"], []).append(r.community_name)

    stale: List[Result] = []
    for v in crm_views:
        if not parent_is_bellhaven(v):
            continue
        if v["name"].strip().lower().endswith("(parent account)"):
            continue
        if on_website(v["name"]):
            continue
        if norm_generic(v["status"]) == "inactive":
            continue  # already handled

        note = "no community on the Bellhaven website matches this account"
        if v["account_id"] in suggested_for:
            note += ("; also suggested as a possible match for missing community(ies): "
                     + ", ".join(suggested_for[v["account_id"]]))
        if has_financials(v):
            note += (f"; account carries financial history (lifetime revenue "
                     f"{v['lifetime_revenue']:.0f}, outstanding AR {v['outstanding_ar']:.0f})")

        candidates = crm_match_candidates(v, crm_views)
        if candidates:
            note += (f"; {len(candidates)} other CRM account(s) may be the same "
                     "community - compare below")

        stale.append(
            Result(
                community_name=v["name"],
                classification="stale",
                crm_account_id=v["account_id"],
                crm_account_name=v["name"],
                crm_view=v,
                possible_crm_match=candidates[0]["name"] if candidates else "",
                possible_matches=candidates,
                details=note,
                field_diffs=[FieldDiff("status", STATUS_CHOICES[0], v["status"])],
            )
        )
    return stale


def _duplicate_key(v: Dict) -> Tuple[str, str, str, str]:
    return (
        norm_address(v["address"]),
        norm_generic(v["city"]),
        norm_generic(v["state"]),
        (v["zip"] or "").split("-")[0],
    )


_DEDUPE_SKIP_STATUS = {"inactive", "needsreview"}


def _crm_duplicate_groups(crm_views: List[Dict]) -> List[Result]:
    """CRM accounts that share the same address, city, state AND zip - possible
    duplicate records. The reviewer picks the active account (the others get
    duplicate_of_account + Inactive) or sends the whole group to Needs Review.

    Accounts already set to `Inactive` or `Needs Review` are omitted entirely -
    they have been dealt with and should not clutter the queue."""
    buckets: Dict[Tuple[str, str, str, str], List[Dict]] = {}
    for v in crm_views:
        if v["name"].strip().lower().endswith("(parent account)"):
            continue
        if norm_generic(v["status"]).replace(" ", "") in _DEDUPE_SKIP_STATUS:
            continue
        key = _duplicate_key(v)
        if not all(key):  # need address, city, state and zip all present
            continue
        buckets.setdefault(key, []).append(v)

    groups: List[Result] = []
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        ids = {m["account_id"] for m in members}
        # already partly deduped? (the rest already point at a sibling) -> skip
        unresolved = [m for m in members if m["duplicate_of_account"] not in ids]
        if len(unresolved) < 2:
            continue

        members = sorted(members, key=lambda m: m["account_id"])
        first = members[0]
        sig = ",".join(m["account_id"] for m in members)
        addr = (f"{first['address']}, {first['city']} {first['state']} "
                f"{first['zip']}")
        groups.append(
            Result(
                community_name=f"duplicate: {addr}",
                classification="duplicate",
                crm_account_name=addr,
                possible_crm_match=sig,
                possible_matches=members,
                details=(
                    f"{len(members)} CRM accounts share this address: "
                    + "; ".join(m["name"] for m in members)
                ),
            )
        )
    return sorted(groups, key=lambda g: g.crm_account_name)


def diff_against_account(scraped: Dict, crm_account: Dict) -> Tuple[List[FieldDiff], List[str]]:
    """Compare a scraped row against a specific CRM account (used when a user
    manually links a 'missing' community to an existing account)."""
    return diff_fields(scraped, crm_view(crm_account))


# --- payload builders (single source of truth for what we send) ---------

def target_crm_value(diff: FieldDiff, care_choice: Optional[str] = None) -> str:
    """The value we would write to the CRM for an approved diff."""
    if diff.field == "parent_id":
        return BELLHAVEN_PARENT_ID
    if diff.field == "care offerings":
        if care_choice:
            return care_choice
        return (diff.care_candidates or [""])[0]
    return diff.scraped_value


def build_patch_body(
    approved: List[FieldDiff], care_choice: Optional[str] = None
) -> Dict[str, str]:
    """Turn approved field diffs into a CRM PATCH payload."""
    body: Dict[str, str] = {}
    for diff in approved:
        body[PATCH_FIELD_MAP[diff.field]] = target_crm_value(diff, care_choice)
    return body


def build_create_body(
    values: Dict[str, str],
    care_choice: str,
    parent_id: str,
    status: str = "Active",
) -> Dict[str, str]:
    """Build a CRM create payload from resolved field values.

    ``values`` keys are the COMPARE_FIELDS ('name', 'address', ...).
    """
    body = {
        "name": values.get("name", "").strip(),
        "billing_street": values.get("address", "").strip(),
        "billing_city": values.get("city", "").strip(),
        "billing_state": values.get("state", "").strip(),
        "billing_zip": values.get("zip", "").strip(),
        "status": status,
    }
    if care_choice:
        body["care_type"] = care_choice
    if parent_id:
        body["parent_id"] = parent_id
    return {k: v for k, v in body.items() if v}
