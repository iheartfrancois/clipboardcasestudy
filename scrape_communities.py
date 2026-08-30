#!/usr/bin/env python3
"""Scrape Bellhaven Senior Living communities.

Walks the paginated listing at /communities, follows each community link to its
detail page, and pulls: community name, street address, city, state, zip, and
care offerings. Writes the result to a CSV.

Usage:
    python scrape_communities.py [-o communities.csv]
"""

import argparse
import csv
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://analyst-assessment-production.up.railway.app"
LISTING_PATH = "/communities"

# "City, ST 12345" or "City, ST 12345-6789"
CITY_STATE_ZIP_RE = re.compile(r"^(.*),\s*([A-Za-z]{2})\.?\s+(\d{5}(?:-\d{4})?)\s*$")

FIELDNAMES = [
    "community_name",
    "address",
    "city",
    "state",
    "zip",
    "care_offerings",
]


def make_session():
    session = requests.Session()
    session.headers.update(
        {"User-Agent": "community-scraper/1.0 (+https://example.com)"}
    )
    return session


def get_soup(session, url):
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def total_pages(soup):
    """Read 'Page X / Y' out of the pager; default to 1 if not found."""
    pager = soup.select_one(".pager")
    if pager:
        m = re.search(r"Page\s+\d+\s*/\s*(\d+)", pager.get_text(" ", strip=True))
        if m:
            return int(m.group(1))
    return 1


def collect_community_paths(session):
    """Return an ordered, de-duplicated list of /communities/<slug> paths."""
    first = get_soup(session, f"{BASE_URL}{LISTING_PATH}?page=1")
    pages = total_pages(first)

    seen = set()
    paths = []

    def harvest(soup):
        for a in soup.select('.card h3 a[href^="/communities/"]'):
            href = a["href"].split("?")[0].split("#")[0]
            if href not in seen:
                seen.add(href)
                paths.append(href)

    harvest(first)
    for page in range(2, pages + 1):
        soup = get_soup(session, f"{BASE_URL}{LISTING_PATH}?page={page}")
        harvest(soup)
        time.sleep(0.3)

    return paths


def parse_detail(soup):
    """Pull the fields out of a community detail page."""
    name = soup.find("h1").get_text(strip=True) if soup.find("h1") else ""

    record = {
        "community_name": name,
        "address": "",
        "city": "",
        "state": "",
        "zip": "",
        "care_offerings": "",
    }

    dl = soup.select_one("dl.detail")
    if not dl:
        return record

    for dt in dl.find_all("dt"):
        label = dt.get_text(strip=True).lower()
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue

        if label == "address":
            # <dd>210 Orchard Lane<br>Maplewood, OH 44280</dd>
            lines = [
                seg.strip()
                for seg in dd.get_text("\n", strip=True).split("\n")
                if seg.strip()
            ]
            if lines:
                last = lines[-1]
                m = CITY_STATE_ZIP_RE.match(last)
                if m:
                    record["city"] = m.group(1).strip()
                    record["state"] = m.group(2).upper()
                    record["zip"] = m.group(3)
                    record["address"] = ", ".join(lines[:-1])
                else:
                    # Couldn't split the last line; keep the whole thing.
                    record["address"] = ", ".join(lines)
        elif label in ("care offerings", "care offering"):
            badges = [b.get_text(strip=True) for b in dd.find_all("span", class_="badge")]
            if not badges:
                badges = [dd.get_text(" ", strip=True)]
            record["care_offerings"] = "; ".join(b for b in badges if b)

    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o", "--output", default="communities.csv", help="output CSV path"
    )
    args = parser.parse_args(argv)

    session = make_session()

    print("Collecting community links...", file=sys.stderr)
    paths = collect_community_paths(session)
    print(f"Found {len(paths)} communities.", file=sys.stderr)

    records = []
    for i, path in enumerate(paths, 1):
        url = f"{BASE_URL}{path}"
        soup = get_soup(session, url)
        record = parse_detail(soup)
        records.append(record)
        print(f"  [{i}/{len(paths)}] {record['community_name']}", file=sys.stderr)
        time.sleep(0.3)

    with open(args.output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(records)

    print(f"Wrote {len(records)} rows to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
