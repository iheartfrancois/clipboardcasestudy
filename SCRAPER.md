# Bellhaven community scraper, CRM comparison & review app

Scrapes the Bellhaven public community site, compares it to the Meridian CRM
sandbox, and provides a Flask review app that walks the resulting changes tier
by tier (needs update → change of ownership → missing → no website match →
possible duplicates), applying each approved change through the CRM API.

```
scrape_communities.py   scrape the public community site -> communities.csv
crm_compare.py          one-shot: scrape + compare to the CRM -> crm_comparison.csv
crm_sync/               daily comparison + web app to approve/reject CRM changes
  crm_api.py    Bearer-token REST client for the CRM
  compare.py    normalise / match / classify (pure, no network)
  pipeline.py   run_comparison(): scrape + fetch CRM + classify
  store.py      SQLite review queue + per-field proposals + audit log
  app.py        Flask review UI + APScheduler daily refresh
```

## Setup

```bash
pip install -r requirements.txt
```

Put the CRM token (the `bh_...` segment from the CRM URL) in `.env` (gitignored):

```
CRM_API_TOKEN=bh_xxxxxxxxxxxxxxxxxxxxxx
```

## 1. `scrape_communities.py`

Scrapes every community from
`https://analyst-assessment-production.up.railway.app/communities` — walks the
paginated listing, follows each community link, and parses **name, address,
city, state, zip, care offerings** into `communities.csv`.

```bash
python scrape_communities.py -o communities.csv
```

## 2. `crm_compare.py` — one-shot comparison

Scrapes the site, pulls all CRM accounts **via the CRM API**, and classifies
each community:

| classification | meaning |
|----------------|---------|
| `good`         | a CRM account matches by name, every compared field agrees, and it sits under the Bellhaven Senior Living parent |
| `needs update` | a CRM account matches by name but one or more fields differ (listed in `mismatched_fields`). `parent_id` is one of the checked fields — if the account is under the wrong parent **and** has no money (lifetime revenue and outstanding AR both 0), fixing the parent is offered here like any other field |
| `chow`         | the matched account is under the wrong parent **and** carries financial history (lifetime revenue or outstanding AR > 0). Its parent is left alone; instead the reviewer creates a fresh account and the old one is CHOW-linked to it (see below) |
| `missing`      | no CRM account has that name — **or** a same-named account exists but its address, city, state, zip **and** parent are *all* different (a coincidental name collision, not our facility) |
| `stale`        | the reverse check — a CRM account **under the Bellhaven parent** whose name has no correspondent among the scraped website communities (likely closed/defunct). The reviewer sets its `status` (default → Inactive). Accounts already `Inactive`, and the parent account itself, are ignored. If the account is also a possible-match candidate for a `missing` community, that's noted |
| `duplicate`    | the final tier — **two or more CRM accounts sharing the same address, city, state and zip** (any parent). The reviewer sees every member account (parent, status, revenue, AR, `duplicate_of_account`, *View in CRM*) and either **picks one as active** — the rest are PATCHed `duplicate_of_account = <chosen id>` + `status = Inactive` — or **sends the whole group to `status = Needs Review`**. Accounts already set to `Inactive` **or** `Needs Review` are omitted entirely (a group only forms from 2+ that are neither), so a resolved group stops appearing |

Every scraped location is expected to be assigned to `parent_id` **Bellhaven
Senior Living** (`0015QAPLGS3FVYEEEM`). The CSV columns `crm_parent_name`,
`crm_parent_ok`, `crm_lifetime_revenue`, `crm_outstanding_ar` and
`crm_chow_current_account` show the raw values behind that check.

```bash
python crm_compare.py -o crm_comparison.csv          # fresh scrape
python crm_compare.py -i communities.csv -o out.csv  # reuse an existing scrape
```

For `missing` rows the CSV column `possible_match_details` lists candidate CRM
accounts (closest name + same city/state) with full **name, address, city,
state, zip, care type, parent and account id**, so a stale/renamed account can
be spotted without opening the app.

### How matching works

- **Finding the CRM record** — names are normalised (case, punctuation,
  `&`/"and", a leading "The", and abbreviations like *Rehab→Rehabilitation*,
  *Centre→Center*) and matched exactly; failing that, the closest name above a
  similarity threshold is used. A fuzzy match whose **city and state both
  disagree** is rejected as a different community (→ `missing`), with the near
  miss recorded in `possible_crm_match`.
- **Comparing fields**
  - *name* — formatting differences (case, punctuation, "The", `&`) are ignored;
    abbreviation / wording differences (`Rehab` vs `Rehabilitation`, `at` vs
    `of`) are flagged.
  - *address* → CRM `billing_street`; common abbreviations are expanded before
    comparing (`Blvd`↔`Boulevard`, `NW`↔`Northwest`, `Ln`↔`Lane`, …).
  - *zip* — compared on the 5-digit prefix.
  - *care offerings* → CRM `care_type`; the scraped taxonomy is mapped
    (`Short-Term Rehabilitation & Nursing`→`Skilled Nursing`,
    `Memory Support`→`Memory Care`, `Assisted Living` / `Independent Living`
    unchanged). `care_type` is single-valued, so a community listing two
    offerings is always flagged (pick which one to store in the app).
- An **exact** name match whose location is entirely different (e.g. *Amberly
  Manor* in Hudson OH vs Colorado Springs CO) stays `needs update` with a
  "possible name collision — verify same entity" note.

## 3. `crm_sync` — daily sync & review app

```bash
python -m crm_sync.app        # http://127.0.0.1:5000
```

The home page is a **review queue** of every `needs update` / `chow` /
`missing` community plus every `stale` CRM account.

**The queue is worked strictly in order:** `needs update` → change of ownership
→ `missing` → no website match → possible duplicates. A tier is locked (items visible but no *Review*
button; opening one bounces back to the queue) until the tier above it has no
pending items. Re-opening an already-decided item from a higher tier makes that
tier active again. Auto-advance and prev/next stay within the active tier.

Open an item and:

- **needs update** — a per-field table (CRM value vs scraped value). Each field
  has approve / reject radios (real differences default to *approve*). For
  *care offerings* you pick the single `care_type` to store; approving
  *parent_id* always writes the Bellhaven parent. "Review payload →" shows the
  exact `PATCH /api/v1/accounts/<id>` body; a second click sends it.
- **chow** (change of ownership) — the account has money and the wrong parent,
  so it must not be edited. You get the create-account form pre-filled from the
  scrape (parent forced to Bellhaven). "Review the two operations →" shows both
  the `POST /api/v1/accounts` (new account) and the follow-up
  `PATCH /api/v1/accounts/<old id>` `{chow_current_account: <new id>}`. One
  confirm runs the create and then **automatically** sets `chow_current_account`
  on the old account. The old account's parent, revenue and AR are never
  touched. If the create succeeds but the CHOW link fails, the audit log keeps
  the new account id so you can finish it manually.
- **stale** — the account's details (address, care type, status, revenue, AR)
  plus a `status` dropdown (default *Inactive*). "Review payload →" shows the
  exact `PATCH /api/v1/accounts/<id>` `{status: …}`, then confirm sends it.
  *Keep as-is* dismisses it locally. If the account is later matched (community
  re-added to the website) or set Inactive elsewhere, the item auto-closes.
  Below that, **other CRM accounts that may be the same community** — any account
  with the same normalised address, the same city/state, or a close name — are
  shown with a field-by-field comparison (name / address / city / state / zip /
  care type, plus parent, status, revenue, AR) and a *View in CRM ↗* link, so a
  change-of-ownership or duplicate can be spotted before deactivating. Purely
  informational; the action stays "set status".
- **missing** — two options:
  - *Create a new CRM account* — an editable form pre-filled from the scrape
    (`care_type` mapped, `status=Active`, `parent_id`=Bellhaven Senior Living).
    "Review payload →" then confirm sends `POST /api/v1/accounts`.
  - *Link to an existing account* — the page lists every CRM account that
    might be the same place (closest name match + any account in the same
    city/state) with its **full name, address, city, state, zip, care type
    and parent** so you can compare against the scraped row shown above the
    table. Each row has a **View in CRM ↗** link (opens the account in the
    CRM in a new tab) so you can check it before committing. Click *Link →*
    on one (or use the free-text search below): the community is compared
    against that account and, if anything differs (usually just the name), a
    confirm screen shows the exact `PATCH` — the website is authoritative, so
    **every differing field is applied in this one step**. Confirming resolves
    the item; it never bounces to the `needs update` queue. This is how renamed
    accounts get handled — e.g. *Bellhaven of Chesterton* renamed to
    *Chesterton Senior Commons* in the CRM.
- **duplicate** — a table of every account at that address (name, id, address,
  care type, parent, status, lifetime revenue, outstanding AR,
  `duplicate_of_account`, last-updated, *View in CRM ↗*). Then:
  - *Keep selected as active* — pick a radio; the confirm screen shows one
    `PATCH /api/v1/accounts/<id>` `{duplicate_of_account: <chosen>, status: Inactive}`
    per other account; sending runs them all. The chosen account is untouched.
  - *Set all to “Needs Review”* — one `PATCH … {status: "Needs Review"}` per
    member account.
  - *Not duplicates — dismiss* — closes the group locally.
- **Reject** closes the item. After an approve or a reject the app loads the
  **next item in the queue** automatically (wrapping around; when the queue is
  empty it returns to the queue page). The item page also shows your position
  (`3 of 15`) with *← prev* / *skip to next →* links.
- **/audit** lists every write attempt and decision.

**Nothing is written to the CRM without an explicit confirm in the UI.** Every
attempt (payload, HTTP status, response) is recorded in the audit log
(`data/crm_review.db`, gitignored).

### Dry run

```bash
CRM_SYNC_DRY_RUN=1 python -m crm_sync.app
```

A banner appears and every PATCH/POST is logged (with its payload) instead of
being sent. Dry-run entries in `/audit` are prefixed **DRY-RUN** with a blank
"Sent to CRM?" column — nothing is ever marked `applied`.

Any decision in dry run - **apply, create, chow, status, and reject** - moves
the item to status **`rehearsed`**: it drops out of the active queue for that
session (so you can walk the whole list without looping) but nothing is saved.
It comes back automatically on the next "Run comparison now", on an app restart,
or via the *Bring them back →* link on the queue page. (A **live** reject is a
real saved decision - it stays `rejected` until you re-open it.)

To go live, stop the app and restart it without the variable
(`unset CRM_SYNC_DRY_RUN`; check `.env`).

### Scheduling

- **In-app**: APScheduler re-runs the comparison daily at 13:35 UTC while the
  app is running, and "Run comparison now" refreshes on demand.
- **GitHub Action** (`.github/workflows/crm-sync.yml`): runs
  `python -m crm_sync.pipeline --write-snapshot data/crm_snapshot.json` daily
  and commits the JSON snapshot (text, diffable). On startup the app imports
  that snapshot if it is newer than its last local run, so the queue is fresh
  even if the app was off. The Action only reads the CRM — it never writes.

  Add the repo secret `CRM_API_TOKEN` (Settings → Secrets and variables →
  Actions). The CI job needs no other secrets.

### Queue reconciliation

Re-running the comparison:

- adds newly-flagged communities as `pending`,
- refreshes the proposed values on still-`pending` items,
- marks an item `resolved_upstream` if the comparison now classifies that
  community as `good` (someone fixed the CRM another way),
- re-opens an `applied` item that is still flagged (the write didn't fully
  resolve it),
- closes a **`stale`** or **`duplicate`** item that the comparison no longer
  produces (resolved another way),
- re-opens a `resolved_upstream` item that the comparison flags again,
- leaves `rejected` items alone,
- **never** removes a `needs update` / `missing` / `chow` item just because it
  is absent from one run — those are keyed to a scraped community and always
  reappear, so a transient absence means a partial scrape, not a fix.

Only a **live** comparison ("Run comparison now" or the scheduler) can
close / re-open items. A **snapshot import** on startup (`sweep_absent=False`)
only *adds* new items and *refreshes* pending ones — it can never remove
anything, since a committed snapshot may be partial or from an older build.

Before sending a PATCH the app re-reads the account and, if it changed since
the last comparison, refreshes the diff and asks you to re-review.

## Tests

```bash
python test_crm_sync.py     # offline: normalisation, classify, payload builders, store
```
