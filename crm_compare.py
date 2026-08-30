#!/usr/bin/env python3
"""Compare scraped Bellhaven community data against the Meridian CRM.

Thin CLI wrapper around ``crm_sync.pipeline`` (kept for the original
interface). Classification per community:

    good          - a CRM account matches by name and every compared field agrees
    needs update  - a CRM account matches by name but one or more fields differ
    missing       - no CRM account has that name

Compared fields: name, address, city, state, zip, care offerings.

Usage:
    python crm_compare.py [-o crm_comparison.csv] [-i communities.csv]

Needs a CRM token in CRM_API_TOKEN (or a .env file). With -i, community rows
are read from that CSV instead of re-scraping the site.

For the daily schedule and the approve/reject review UI, see
`python -m crm_sync.app` and SCRAPER.md.
"""
import argparse
import csv
import os
import sys

import scrape_communities as scraper
from crm_sync import compare
from crm_sync.crm_api import CrmClient
from crm_sync.pipeline import print_summary, scrape_community_rows, write_csv


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", default="crm_comparison.csv")
    parser.add_argument("-i", "--input", default=None,
                        help="read community rows from this CSV instead of scraping")
    parser.add_argument("--cache-dir", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.input and os.path.exists(args.input):
        with open(args.input, newline="", encoding="utf-8") as fh:
            scraped = list(csv.DictReader(fh))
    else:
        scraped = scrape_community_rows(progress=lambda m: print(m, file=sys.stderr))

    accounts = CrmClient().list_accounts()
    results = compare.classify(scraped, accounts)

    print_summary(results)
    write_csv(results, args.output)
    print(f"\nWrote {len(results)} rows to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
