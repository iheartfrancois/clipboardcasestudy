"""Offline sanity tests for crm_sync (no network).

Run:  python test_crm_sync.py
"""
import tempfile
from pathlib import Path

from crm_sync import compare, store
from crm_sync.compare import FieldDiff
from crm_sync.crm_api import BELLHAVEN_PARENT_ID as BELL

# --- fixtures -----------------------------------------------------------

SCRAPED = [
    # exact match, everything agrees -> good
    {"community_name": "Bellhaven of Maplewood", "address": "210 Orchard Lane",
     "city": "Maplewood", "state": "OH", "zip": "44280", "care_offerings": "Assisted Living"},
    # exact name, zip differs -> needs update (zip)
    {"community_name": "Bellhaven of Portsmouth", "address": "2222 Gallia St",
     "city": "Portsmouth", "state": "OH", "zip": "45662",
     "care_offerings": "Short-Term Rehabilitation & Nursing"},
    # abbreviation-only name difference -> needs update (name)
    {"community_name": "Bellhaven Rehabilitation & Nursing of Grove City",
     "address": "3985 Broadway", "city": "Grove City", "state": "OH", "zip": "43123",
     "care_offerings": "Short-Term Rehabilitation & Nursing"},
    # two care offerings vs single CRM care_type -> needs update (care offerings)
    {"community_name": "Bellhaven Shores of Erie", "address": "4930 W Lake Rd",
     "city": "Erie", "state": "PA", "zip": "16505",
     "care_offerings": "Assisted Living; Memory Support"},
    # no CRM account by that name -> missing
    {"community_name": "Bellhaven of Kettering", "address": "3313 Wilmington Pike",
     "city": "Kettering", "state": "OH", "zip": "45429",
     "care_offerings": "Short-Term Rehabilitation & Nursing"},
    # fuzzy-close name but different town -> guard sends it to missing, not needs-update
    {"community_name": "Bellhaven of Carlisle", "address": "640 Walnut Bottom Rd",
     "city": "Carlisle", "state": "PA", "zip": "17015",
     "care_offerings": "Short-Term Rehabilitation & Nursing"},
    # wrong parent + has revenue -> chow (create new + link old)
    {"community_name": "Bellhaven of Tiffin", "address": "45 St Lawrence Dr",
     "city": "Tiffin", "state": "OH", "zip": "44883",
     "care_offerings": "Short-Term Rehabilitation & Nursing"},
    # wrong parent, no money -> needs update with a parent_id fix
    {"community_name": "Bellhaven of Marion", "address": "199 Barks Rd W",
     "city": "Marion", "state": "OH", "zip": "43302",
     "care_offerings": "Short-Term Rehabilitation & Nursing"},
    # wrong parent + revenue BUT already CHOW-linked -> treated as good
    {"community_name": "Bellhaven of Owosso", "address": "1120 W Main St",
     "city": "Owosso", "state": "MI", "zip": "48867", "care_offerings": "Assisted Living"},
    # exact name but the CRM account is a totally different place (address, city,
    # state, zip AND parent all differ) -> name collision -> missing, not update
    {"community_name": "Amberly Manor", "address": "4390 Darrow Rd",
     "city": "Hudson", "state": "OH", "zip": "44236", "care_offerings": "Assisted Living"},
]

CRM = [
    {"account_id": "A1", "name": "Bellhaven of Maplewood", "billing_street": "210 Orchard Lane",
     "billing_city": "Maplewood", "billing_state": "OH", "billing_zip": "44280", "parent_id": BELL,
     "care_type": "Assisted Living", "status": "Active", "updated_at": "2026-08-28 17:04:39Z"},
    {"account_id": "A2", "name": "Bellhaven of Portsmouth", "billing_street": "2222 Gallia St",
     "billing_city": "Portsmouth", "billing_state": "OH", "billing_zip": "45626", "parent_id": BELL,
     "care_type": "Skilled Nursing", "status": "Active", "updated_at": "2026-08-28 17:04:39Z"},
    {"account_id": "A3", "name": "Bellhaven Rehab and Nursing of Grove City",
     "billing_street": "3985 Broadway", "billing_city": "Grove City", "billing_state": "OH",
     "billing_zip": "43123", "care_type": "Skilled Nursing", "status": "Active", "parent_id": BELL},
    {"account_id": "A4", "name": "Bellhaven Shores of Erie", "billing_street": "4930 W Lake Rd",
     "billing_city": "Erie", "billing_state": "PA", "billing_zip": "16505",
     "care_type": "Assisted Living", "status": "Active", "parent_id": BELL},
    {"account_id": "A5", "name": "Bellhaven of New Carlisle", "billing_street": "875 Elm St",
     "billing_city": "New Carlisle", "billing_state": "OH", "billing_zip": "45344",
     "care_type": "Skilled Nursing", "status": "Active", "parent_id": BELL},
    # a renamed account: same place as scraped "Bellhaven of Kettering", different name
    {"account_id": "A6", "name": "Kettering Care Centre", "billing_street": "3313 Wilmington Pike",
     "billing_city": "Kettering", "billing_state": "OH", "billing_zip": "45429",
     "care_type": "Skilled Nursing", "status": "Active",
     "parent_id": BELL, "parent_name": "Bellhaven Senior Living (Parent Account)"},
    # wrong parent + money -> chow
    {"account_id": "A7", "name": "Bellhaven of Tiffin", "billing_street": "45 St Lawrence Dr",
     "billing_city": "Tiffin", "billing_state": "OH", "billing_zip": "44883",
     "care_type": "Skilled Nursing", "status": "Active",
     "parent_id": "001CEDARTRAIL", "parent_name": "Cedar Trail Communities (Parent Account)",
     "lifetime_revenue": 84000, "outstanding_ar": 12400},
    # wrong parent, no money -> parent_id fix
    {"account_id": "A8", "name": "Bellhaven of Marion", "billing_street": "199 Barks Rd W",
     "billing_city": "Marion", "billing_state": "OH", "billing_zip": "43302",
     "care_type": "Skilled Nursing", "status": "Active",
     "parent_id": "001JUNIPER", "parent_name": "Juniper Point Healthcare (Parent Account)",
     "lifetime_revenue": 0, "outstanding_ar": 0},
    # wrong parent + money BUT already CHOW-linked -> not flagged
    {"account_id": "A9", "name": "Bellhaven of Owosso", "billing_street": "1120 W Main St",
     "billing_city": "Owosso", "billing_state": "MI", "billing_zip": "48867",
     "care_type": "Assisted Living", "status": "Active",
     "parent_id": "001HARBORVIEW", "parent_name": "Harborview Care Group (Parent Account)",
     "lifetime_revenue": 12000, "outstanding_ar": 0, "chow_current_account": "001NEWOWOSSO"},
    # exact name for scraped "Amberly Manor" but a different place entirely
    {"account_id": "A14", "name": "Amberly Manor", "billing_street": "918 S Nevada Ave",
     "billing_city": "Colorado Springs", "billing_state": "CO", "billing_zip": "80903",
     "care_type": "Assisted Living", "status": "Active",
     "parent_id": "001JUNIPER", "parent_name": "Juniper Point Healthcare (Parent Account)",
     "lifetime_revenue": 0, "outstanding_ar": 0},
    # Bellhaven-parented, no website community with this name -> stale
    {"account_id": "A10", "name": "Bellhaven of Elsewhere", "billing_street": "1 Gone Rd",
     "billing_city": "Nowhere", "billing_state": "OH", "billing_zip": "40000",
     "care_type": "Assisted Living", "status": "Active", "parent_id": BELL},
    # Bellhaven-parented, no match, but ALREADY Inactive -> not flagged
    {"account_id": "A11", "name": "Bellhaven of History", "billing_street": "2 Past Ave",
     "billing_city": "Old", "billing_state": "OH", "billing_zip": "40001",
     "care_type": "Assisted Living", "status": "Inactive", "parent_id": BELL},
    # the top parent account itself -> never flagged
    {"account_id": "A12", "name": "Bellhaven Senior Living (Parent Account)",
     "billing_street": "", "billing_city": "Columbus", "billing_state": "OH",
     "billing_zip": "", "care_type": "", "status": "Active", "parent_id": BELL},
    # two accounts sharing an address -> a possible-duplicate group
    {"account_id": "A15", "name": "Twin Oaks A", "billing_street": "5 Twin Rd",
     "billing_city": "Dublin", "billing_state": "OH", "billing_zip": "43017",
     "care_type": "Assisted Living", "status": "Active", "parent_id": "001JUNIPER"},
    {"account_id": "A16", "name": "Twin Oaks B", "billing_street": "5 Twin Road",
     "billing_city": "Dublin", "billing_state": "OH", "billing_zip": "43017",
     "care_type": "Skilled Nursing", "status": "Active", "parent_id": "001CEDARTRAIL",
     "lifetime_revenue": 40000},
    # same address but already Inactive / Needs Review -> omitted from the group
    {"account_id": "A17", "name": "Twin Oaks C", "billing_street": "5 Twin Rd",
     "billing_city": "Dublin", "billing_state": "OH", "billing_zip": "43017",
     "care_type": "Assisted Living", "status": "Inactive", "parent_id": "001JUNIPER"},
    {"account_id": "A18", "name": "Twin Oaks D", "billing_street": "5 Twin Rd",
     "billing_city": "Dublin", "billing_state": "OH", "billing_zip": "43017",
     "care_type": "Assisted Living", "status": "Needs Review", "parent_id": "001JUNIPER"},
    # same building as the stale A10, under a different parent, with money ->
    # should surface as a possible-match for A10
    {"account_id": "A13", "name": "Nowhere Care & Rehab", "billing_street": "1 Gone Road",
     "billing_city": "Nowhere", "billing_state": "OH", "billing_zip": "40009",
     "care_type": "Assisted Living", "status": "Active",
     "parent_id": "001CEDARTRAIL", "parent_name": "Cedar Trail Communities (Parent Account)",
     "lifetime_revenue": 90000, "outstanding_ar": 0},
]


def check(label, got, want):
    status = "ok " if got == want else "FAIL"
    print(f"  [{status}] {label}: got {got!r} want {want!r}")
    assert got == want, f"{label}: {got!r} != {want!r}"


def main():
    print("=== normalisation ===")
    check("blvd == boulevard",
          compare.norm_address("1125 Logan Blvd"), compare.norm_address("1125 Logan Boulevard"))
    check("NW == Northwest",
          compare.norm_address("4850 NW Sylvania Ave"),
          compare.norm_address("4850 Northwest Sylvania Avenue"))
    check("Pk == Pike (so 'Wilmington Pk' groups with 'Wilmington Pike')",
          compare.norm_address("3313 Wilmington Pk"),
          compare.norm_address("3313 Wilmington Pike"))
    check("name compare ignores '&'/'the'/case",
          compare.norm_name_for_compare("The Arbors at Bellhaven - Dayton"),
          compare.norm_name_for_compare("Arbors at Bellhaven Dayton"))
    check("name compare keeps Rehab vs Rehabilitation distinct",
          compare.norm_name_for_compare("Bellhaven Rehab of X")
          != compare.norm_name_for_compare("Bellhaven Rehabilitation of X"), True)

    print("=== map_care ===")
    check("short-term rehab -> Skilled Nursing",
          compare.map_care("Short-Term Rehabilitation & Nursing"), ["Skilled Nursing"])
    check("memory support -> Memory Care",
          compare.map_care("Assisted Living; Memory Support"),
          ["Assisted Living", "Memory Care"])

    print("=== classify ===")
    results = {r.community_name: r for r in compare.classify(SCRAPED, CRM)}
    check("Maplewood -> good", results["Bellhaven of Maplewood"].classification, "good")
    check("Portsmouth -> needs update", results["Bellhaven of Portsmouth"].classification, "needs update")
    check("Portsmouth mismatched fields", results["Bellhaven of Portsmouth"].mismatched_fields, ["zip"])
    check("Grove City -> needs update (name)",
          results["Bellhaven Rehabilitation & Nursing of Grove City"].mismatched_fields, ["name"])
    check("Erie -> needs update (care offerings)",
          results["Bellhaven Shores of Erie"].mismatched_fields, ["care offerings"])
    check("Kettering -> missing", results["Bellhaven of Kettering"].classification, "missing")
    check("Carlisle -> missing (fuzzy guard)",
          results["Bellhaven of Carlisle"].classification, "missing")

    print("=== name collision -> missing ===")
    am = results["Amberly Manor"]
    check("exact name but address+city+state+zip+parent all differ -> missing",
          am.classification, "missing")
    check("...flagged as a name collision", am.name_collision, True)
    check("...field_diffs are the create form, not update diffs",
          [d.field for d in am.field_diffs],
          ["name", "address", "city", "state", "zip", "care offerings"])
    check("...still surfaces the same-named CRM account as a candidate",
          any(m["name"] == "Amberly Manor" for m in am.possible_matches)
          or "Amberly Manor" in am.possible_crm_match, True)

    print("=== parent_id / CHOW ===")
    check("crm_view exposes financials",
          compare.crm_view(CRM[6])["lifetime_revenue"], 84000.0)
    check("Tiffin: wrong parent + revenue -> chow",
          results["Bellhaven of Tiffin"].classification, "chow")
    check("Tiffin chow carries the old account id",
          results["Bellhaven of Tiffin"].crm_account_id, "A7")
    check("Marion: wrong parent, no money -> needs update",
          results["Bellhaven of Marion"].classification, "needs update")
    check("Marion flags parent_id",
          "parent_id" in results["Bellhaven of Marion"].mismatched_fields, True)
    check("parent_id patch always targets Bellhaven",
          compare.build_patch_body(results["Bellhaven of Marion"].field_diffs),
          {"parent_id": BELL})
    check("Owosso: wrong parent but already CHOW-linked -> not flagged",
          results["Bellhaven of Owosso"].classification, "good")

    print("=== stale (Bellhaven account, no website match) ===")
    stale = {r.crm_account_id: r for r in compare.classify(SCRAPED, CRM)
             if r.classification == "stale"}
    check("A10 (no website community) -> stale", "A10" in stale, True)
    check("A6 (Bellhaven-parented, unmatched name) -> stale", "A6" in stale, True)
    check("A11 (already Inactive) -> not flagged", "A11" in stale, False)
    check("A12 (parent account itself) -> not flagged", "A12" in stale, False)
    check("A5 (fuzzy-matched to a scraped community) -> not stale", "A5" in stale, False)
    check("stale proposes a status change from the current value",
          [(d.field, d.crm_value, d.scraped_value) for d in stale["A10"].field_diffs],
          [("status", "Active", "Inactive")])
    check("stale item cross-references a suggested-for missing community",
          "Bellhaven of Kettering" in stale["A6"].details, True)
    check("status patch body",
          compare.build_patch_body(stale["A10"].field_diffs), {"status": "Inactive"})

    a10_cands = {c["account_id"]: c for c in stale["A10"].possible_matches}
    check("stale item surfaces the same-address CRM account", "A13" in a10_cands, True)
    a13 = a10_cands["A13"]
    check("...flagged as same address", "same address" in a13["reasons"], True)
    check("...with a field-by-field comparison (name differs, location matches)",
          {r["field"]: r["match"] for r in a13["field_comparison"]}["name"], False)
    check("...address field marked matching", next(
          r["match"] for r in a13["field_comparison"] if r["field"] == "address"), True)
    check("...carries the candidate's status + revenue for context",
          (a13["status"], a13["lifetime_revenue"]), ("Active", 90000.0))
    check("A6 stale item has no same-place CRM candidate",
          stale["A6"].possible_matches, [])

    print("=== possible duplicate groups (same address+city+state+zip) ===")
    dups = [r for r in compare.classify(SCRAPED, CRM) if r.classification == "duplicate"]
    twin = next((r for r in dups if "5 Twin" in r.crm_account_name), None)
    check("accounts sharing an address form a duplicate group", twin is not None, True)
    check("...only the Active members (Inactive/Needs Review omitted)",
          sorted(m["account_id"] for m in twin.possible_matches), ["A15", "A16"])
    check("...members carry status + parent + financials for the evidence table",
          (twin.possible_matches[1]["status"], twin.possible_matches[1]["lifetime_revenue"]),
          ("Active", 40000.0))

    def _dups(crm):
        return [x for x in compare.classify(SCRAPED, crm) if x.classification == "duplicate"]
    check("an already-deduped group (rest Inactive + duplicate_of) is not flagged",
          any("5 Twin" in r.crm_account_name for r in _dups(
              [dict(a, status="Inactive", duplicate_of_account="A15") if a["account_id"] == "A16"
               else dict(a) for a in CRM])), False)
    check("setting one of the two Active members to Inactive drops the group",
          any("5 Twin" in r.crm_account_name for r in _dups(
              [dict(a, status="Inactive") if a["account_id"] == "A16" else dict(a) for a in CRM])),
          False)
    check("setting one to Needs Review also drops the group",
          any("5 Twin" in r.crm_account_name for r in _dups(
              [dict(a, status="Needs Review") if a["account_id"] == "A15" else dict(a) for a in CRM])),
          False)

    print("=== link to existing account (missing -> confirm + patch, one step) ===")
    ket_scraped = {"community_name": "Bellhaven of Kettering", "address": "3313 Wilmington Pike",
                   "city": "Kettering", "state": "OH", "zip": "45429",
                   "care_offerings": "Short-Term Rehabilitation & Nursing"}
    renamed = {"account_id": "AX1", "name": "Kettering Nursing & Rehab",
               "billing_street": "3313 Wilmington Pike", "billing_city": "Kettering",
               "billing_state": "OH", "billing_zip": "45429", "care_type": "Skilled Nursing",
               "status": "Active", "parent_id": BELL}
    d1, _ = compare.diff_against_account(ket_scraped, renamed)
    check("linking to a same-place, differently-named account -> only 'name' differs",
          [d.field for d in d1], ["name"])
    check("the link PATCH renames the account to the website name",
          compare.build_patch_body(d1), {"name": "Bellhaven of Kettering"})
    exact = dict(renamed, name="Bellhaven of Kettering")
    d2, _ = compare.diff_against_account(ket_scraped, exact)
    check("linking to an already-matching account -> no diffs (item just resolves)", d2, [])
    d3, _ = compare.diff_against_account(
        ket_scraped, dict(renamed, billing_zip="99999", care_type="Assisted Living"))
    check("a link with several diffs patches them all in one body",
          set(compare.build_patch_body(d3)), {"name", "billing_zip", "care_type"})

    print("=== possible_matches (full account details for 'missing') ===")
    ket_matches = {m["name"]: m for m in results["Bellhaven of Kettering"].possible_matches}
    check("Kettering surfaces the same-city account", "Kettering Care Centre" in ket_matches, True)
    kc = ket_matches["Kettering Care Centre"]
    check("...with its address", kc["address"], "3313 Wilmington Pike")
    check("...city/state/zip", (kc["city"], kc["state"], kc["zip"]), ("Kettering", "OH", "45429"))
    check("...care_type", kc["care_type"], "Skilled Nursing")
    check("...parent_id", kc["parent_id"], "0015QAPLGS3FVYEEEM")
    check("...with a reason", "same city & state" in kc["reasons"], True)
    carl = {m["name"]: m for m in results["Bellhaven of Carlisle"].possible_matches}
    check("Carlisle surfaces the near-name account with full detail",
          carl.get("Bellhaven of New Carlisle", {}).get("zip"), "45344")

    print("=== build_patch_body ===")
    ports = results["Bellhaven of Portsmouth"]
    check("only the approved zip field is sent",
          compare.build_patch_body(ports.field_diffs), {"billing_zip": "45662"})
    check("rejecting every field yields an empty body",
          compare.build_patch_body([]), {})
    erie = results["Bellhaven Shores of Erie"]
    check("care offerings uses the chosen care_type",
          compare.build_patch_body(erie.field_diffs, care_choice="Memory Care"),
          {"care_type": "Memory Care"})

    print("=== build_create_body ===")
    ket = results["Bellhaven of Kettering"]
    values = {d.field if d.field != "care offerings" else "care": d.scraped_value for d in ket.field_diffs}
    body = compare.build_create_body(
        {"name": "Bellhaven of Kettering", "address": "3313 Wilmington Pike",
         "city": "Kettering", "state": "OH", "zip": "45429"},
        care_choice="Skilled Nursing", parent_id="0015QAPLGS3FVYEEEM")
    check("create body has mapped care_type", body["care_type"], "Skilled Nursing")
    check("create body has Bellhaven parent", body["parent_id"], "0015QAPLGS3FVYEEEM")
    check("create body defaults status Active", body["status"], "Active")
    check("create body street", body["billing_street"], "3313 Wilmington Pike")

    print("=== store.sync_results ===")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        all_results = compare.classify(SCRAPED, CRM)
        s1 = store.sync_results(all_results, db_path=db)
        check("first sync inserts the 11 actionable items", s1["new"], 11)
        check("queue length", len(store.queue(db_path=db)), 11)
        check("chow item is in the queue with its old-account view",
              store.get_item("Bellhaven of Tiffin", db_path=db)["crm_view"]["lifetime_revenue"], 84000.0)
        check("stale item is in the queue keyed by CRM account name",
              store.get_item("Bellhaven of Elsewhere", db_path=db)["classification"], "stale")
        s2 = store.sync_results(all_results, db_path=db)
        check("second sync is idempotent (0 new)", s2["new"], 0)

        def _patched(accounts, account_id, **changes):
            return [dict(a, **changes) if a["account_id"] == account_id else dict(a)
                    for a in accounts]

        # Portsmouth zip gets fixed upstream -> item resolves.
        fixed = _patched(CRM, "A2", billing_zip="45662")
        s3 = store.sync_results(compare.classify(SCRAPED, fixed), db_path=db)
        check("resolved upstream once CRM matches", s3["resolved_upstream"], 1)
        names = {i["community_name"] for i in store.queue(db_path=db)}
        check("Portsmouth left the queue", "Bellhaven of Portsmouth" in names, False)

        # A10 gets set Inactive directly in the CRM -> the stale item disappears
        # from the comparison and is closed out.
        fixed2 = _patched(fixed, "A10", status="Inactive")
        s4 = store.sync_results(compare.classify(SCRAPED, fixed2), db_path=db)
        check("stale item closed when it drops out of the comparison",
              s4["resolved_upstream"], 1)
        names = {i["community_name"] for i in store.queue(db_path=db)}
        check("A10 left the queue", "Bellhaven of Elsewhere" in names, False)

        # A scraped-community item (needs update / missing / chow) must NOT be
        # swept if it is transiently absent from a comparison run - only 'stale'
        # items auto-close on disappearance.
        full = compare.classify(SCRAPED, CRM)
        store.sync_results(full, db_path=db)
        partial = [r for r in full if r.community_name != "Bellhaven of Kettering"]
        store.sync_results(partial, db_path=db)
        ket = store.get_item("Bellhaven of Kettering", db_path=db)
        check("a needs-update/missing item absent from one run is NOT resolved",
              ket["status"], "pending")

        # sweep_absent=False (a snapshot import) must never remove ANY item -
        # not even stale/duplicate ones missing from a partial/old snapshot.
        store.sync_results(full, db_path=db)
        n_before = len(store.queue(db_path=db))
        no_dupes = [r for r in full if r.classification != "duplicate"]
        s6 = store.sync_results(no_dupes, db_path=db, sweep_absent=False)
        check("snapshot import (sweep_absent=False) sweeps nothing",
              (s6["resolved_upstream"], len(store.queue(db_path=db))), (0, n_before))

        # --- dry run must not loop: 'rehearsed' items leave the queue ---
        store.sync_results(full, db_path=db)
        target = "Bellhaven of Marion"   # a clean pending needs-update item
        assert store.get_item(target, db_path=db)["status"] == "pending"
        q0 = len(store.queue(db_path=db))
        store.record_apply(target, "patch", {"parent_id": BELL}, ["parent_id"],
                           200, {"dry_run": True}, dry_run=True, db_path=db)
        check("dry-run apply -> status 'rehearsed', not 'applied'",
              store.get_item(target, db_path=db)["status"], "rehearsed")
        check("rehearsed item leaves the active queue", len(store.queue(db_path=db)), q0 - 1)
        check("clear_rehearsed brings it back", store.clear_rehearsed(db_path=db), 1)
        check("...and it's pending again",
              store.get_item(target, db_path=db)["status"], "pending")
        # a real /refresh also un-does a rehearsal
        store.record_apply(target, "patch", {}, ["parent_id"], 200,
                           {"dry_run": True}, dry_run=True, db_path=db)
        r = store.sync_results(full, db_path=db)
        check("sync re-opens a rehearsed item", r["reopened"], 1)

        # a reject in dry run is a rehearsal too, not a saved decision
        store.record_reject(target, "testing", dry_run=True, db_path=db)
        check("dry-run reject -> 'rehearsed', not 'rejected'",
              store.get_item(target, db_path=db)["status"], "rehearsed")
        store.clear_rehearsed(db_path=db)
        check("...and clear_rehearsed brings it back to pending",
              store.get_item(target, db_path=db)["status"], "pending")
        # a live reject IS saved
        store.record_reject(target, "for real", dry_run=False, db_path=db)
        check("live reject -> 'rejected'",
              store.get_item(target, db_path=db)["status"], "rejected")

    print("=== queue tier order + locking ===")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        store.sync_results(compare.classify(SCRAPED, CRM), db_path=db)
        check("queue is ordered by QUEUE_ORDER",
              list(dict.fromkeys(i["classification"] for i in store.queue(db_path=db))),
              compare.QUEUE_ORDER)
        check("active tier starts at 'needs update'",
              store.active_tier(db_path=db), "needs update")
        for cls, nxt in [("needs update", "chow"), ("chow", "missing"),
                         ("missing", "stale"), ("stale", "duplicate"),
                         ("duplicate", None)]:
            for it in [i for i in store.queue(db_path=db) if i["classification"] == cls]:
                store.record_reject(it["community_name"], db_path=db)
            check(f"after clearing '{cls}', active tier -> {nxt}",
                  store.active_tier(db_path=db), nxt)
        store.reopen("Bellhaven of Portsmouth", db_path=db)
        check("re-opening a higher-tier item makes it active again",
              store.active_tier(db_path=db), "needs update")

    print("\nAll crm_sync tests passed.")


if __name__ == "__main__":
    main()
