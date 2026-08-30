# Clipboard Case Study

Bellhaven Senior Living — community-site ↔ CRM reconciliation.

Scrapes every community from the Bellhaven public website, compares it against
the Meridian CRM sandbox, and drives a local review app for pushing the
approved corrections back through the CRM API.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Put the CRM token (the `bh_...` segment from the CRM URL) in `.env`:

```
CRM_API_TOKEN=bh_xxxxxxxxxxxxxxxxxxxxxx
```

Then:

```bash
python scrape_communities.py -o communities.csv   # scrape the website
python crm_compare.py -o crm_comparison.csv        # one-shot comparison -> CSV
python -m crm_sync.app                             # review app at http://127.0.0.1:5000
python test_crm_sync.py                            # offline test suite
```

Rehearse every CRM write without sending it:

```bash
CRM_SYNC_DRY_RUN=1 python -m crm_sync.app
```

Full documentation: **[SCRAPER.md](SCRAPER.md)**.

## Layout

```
scrape_communities.py   scrape the community site -> communities.csv
crm_compare.py          one-shot: scrape + compare to the CRM -> crm_comparison.csv
crm_sync/               the review app + shared pipeline
  crm_api.py    Bearer-token REST client for the CRM
  compare.py    normalise / match / classify (pure, no network)
  pipeline.py   run_comparison(): scrape + fetch CRM + classify
  store.py      SQLite review queue + per-field proposals + audit log
  app.py        Flask review UI + APScheduler daily refresh
  templates/, static/
data/
  crm_snapshot.json   committed, diffable comparison snapshot (CI writes this)
  crm_review.db       local review decisions + audit log (gitignored)
.github/workflows/crm-sync.yml   daily snapshot job (needs secret CRM_API_TOKEN)
```
