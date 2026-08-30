"""Daily CRM sync + human review workflow for the Bellhaven communities.

Pieces:
  crm_api   - Bearer-token REST client for the Meridian CRM sandbox
  compare   - normalise / match / classify scraped communities vs CRM accounts
  pipeline  - run_comparison(): scrape + fetch CRM + classify (CLI + CI use this)
  store     - SQLite review queue, per-field proposals, audit log
  app       - Flask review UI + APScheduler daily refresh
"""
