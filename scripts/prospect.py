#!/usr/bin/env python3
"""Prospect qualification for scraped Google Maps leads (stdlib only).

Scores every business as a potential client for TWO offers:
  1. Website services  — businesses with no website (the `website` field is empty).
  2. ERP / billing     — businesses whose category matches a target list
                         (food & retail, health & beauty, trades & auto,
                         services & fitness), boosted by activity signals
                         (review volume = size proxy).

Use it two ways:
  • Standalone on any results CSV (raw 34-column or trimmed lead CSV):
        python3 scripts/prospect.py results-abc123.csv
        python3 scripts/prospect.py results-abc123.csv --out prospects.csv
        python3 scripts/prospect.py results.csv --erp-categories "restaurant,pharmacy,gym"
  • Imported by scripts/scrape.py when you pass --prospects.
"""
import argparse, csv, sys

# Strong category matches → these businesses run inventory/booking/quotation-heavy
# operations and almost always need billing/ERP. Substring-matched (lowercase) against
# the `category` field, e.g. "Coffee shop", "Auto repair shop", "Pharmacy".
ERP_STRONG = [
    # food & retail
    "restaurant", "café", "cafe", "coffee", "bakery", "grocery", "supermarket",
    "convenience", "pharmacy", "chemist", "drugstore", "liquor", "wine", "butcher",
    "deli", "caterer", "catering", "pizzeria", "ice cream", "snack", "sweet",
    "food", "beverage", "fruit", "vegetable", "market",
    # health & beauty
    "clinic", "dental", "dentist", "doctor", "hospital", "veterinar", "pet clinic",
    "salon", "spa", "barber", "beauty", "parlour", "parlor", "physiotherap",
    "optician", "chiropract", "nail", "tattoo",
    # trades & auto
    "auto repair", "car repair", "car dealer", "car rental", "tire", "tyre",
    "mechanic", "workshop", "garage", "auto parts", "spare parts", "motorcycle",
    "hardware", "plumber", "plumbing", "electrician", "electrical", "contractor",
    "carpenter", "painter", "ac repair", "hvac", "welding", "fabricat",
    # services & fitness
    "gym", "fitness", "yoga", "pilates", "martial art", "dance", "laundromat",
    "laundry", "dry clean", "hotel", "motel", "lodge", "guest house", "hostel",
    "travel", "tour", "tutor", "coaching", "academy", "school", "kindergarten",
    "rental", "hire", "warehouse", "storage", "courier", "logistic", "printing",
    "packers", "movers",
]
# Weak matches — generic retail/service words; plausible ERP need but less certain.
ERP_WEAK = ["store", "shop", "wholesale", "supplier", "distributor", "trader",
            "dealer", "services", "service center", "studio", "boutique"]

# Column names this script adds to the CSV.
PROSPECT_FIELDS = ["has_website", "needs_website", "erp_match", "needs_erp",
                   "priority_score", "priority"]

_NO_WEBSITE = {"", "-", "n/a", "na", "none", "null", "no"}


def has_website(row):
    w = (row.get("website") or "").strip().lower()
    if w in _NO_WEBSITE:
        return False
    # bare "http://" / "https://" with nothing after it
    return w not in ("http://", "https://")


def erp_category_match(row, strong=None, weak=None):
    """Return ("strong"|"weak"|"") for the row's category."""
    strong = strong if strong is not None else ERP_STRONG
    weak = weak if weak is not None else ERP_WEAK
    cat = (row.get("category") or "").strip().lower()
    if not cat:
        return ""
    if any(term in cat for term in strong):
        return "strong"
    if any(term in cat for term in weak):
        return "weak"
    return ""


def _review_count(row):
    try:
        return int(float(row.get("review_count") or 0))
    except ValueError:
        return 0


def score(row, strong=None, weak=None):
    """Return (score, parts-dict). Higher = better prospect.

    score = erp_base (strong 2 / weak 1) + size (>=100 reviews: 2, >=25: 1)
            + 2 if the business has NO website.
    Max 6. needs_erp fires on any category match; a no-website business is a
    website-services prospect even without a category match.
    """
    match = erp_category_match(row, strong, weak)
    erp_base = 2 if match == "strong" else 1 if match == "weak" else 0
    rc = _review_count(row)
    size = 2 if rc >= 100 else 1 if rc >= 25 else 0
    web = 0 if has_website(row) else 2
    return erp_base + size + web, {
        "has_website": "yes" if web == 0 else "no",
        "needs_website": "no" if web == 0 else "yes",
        "erp_match": match or "-",
        "needs_erp": "yes" if erp_base else "no",
        "priority_score": erp_base + size + web,
    }


def priority_label(s):
    return "hot" if s >= 5 else "warm" if s >= 3 else "cool" if s >= 1 else "low"


def qualify(rows, strong=None, weak=None):
    """Add prospect columns to every row IN PLACE and return rows sorted by
    priority_score (highest first)."""
    for r in rows:
        _, parts = score(r, strong, weak)
        parts["priority"] = priority_label(parts["priority_score"])
        r.update(parts)
    return sorted(rows, key=lambda r: int(r.get("priority_score") or 0), reverse=True)


def main():
    ap = argparse.ArgumentParser(description="Score scraped leads as prospects "
                               "(no-website → website services; ERP category match → ERP/billing).")
    ap.add_argument("csv_file", help="results CSV from scrape.py / scrape.sh (any column set)")
    ap.add_argument("--out", help="output CSV (default: <input>-prospects.csv)")
    ap.add_argument("--json", action="store_true", help="write JSON instead of CSV")
    ap.add_argument("--erp-categories", help="comma-separated terms to REPLACE the default "
                    "strong ERP category list, e.g. \"restaurant,pharmacy,gym\"")
    ap.add_argument("--only-prospects", action="store_true",
                    help="keep only rows that need a website OR match an ERP category")
    a = ap.parse_args()

    strong = [t.strip().lower() for t in a.erp_categories.split(",") if t.strip()] \
        if a.erp_categories else None

    with open(a.csv_file, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("X No rows in that file.")

    qualify(rows, strong=strong)
    if a.only_prospects:
        rows = [r for r in rows if r["needs_website"] == "yes" or r["needs_erp"] == "yes"]

    fields = list(rows[0].keys()) + [c for c in PROSPECT_FIELDS if c not in rows[0]]
    ext = ".json" if a.json else ".csv"
    out = a.out or (a.csv_file.rsplit(".", 1)[0] + "-prospects" + ext)
    if a.json:
        import json
        with open(out, "w") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
    else:
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    hot = sum(1 for r in rows if r["priority"] == "hot")
    no_web = sum(1 for r in rows if r["needs_website"] == "yes")
    erp = sum(1 for r in rows if r["needs_erp"] == "yes")
    print(f"Done - {len(rows)} prospects scored -> {out}")
    print(f"  {no_web} need a website | {erp} match ERP categories | {hot} rated HOT")
    print("\nTop prospects (name | category | website? | reviews | score | priority):")
    for r in rows[:10]:
        print(f"  - {r.get('title','')} | {r.get('category','')} | "
              f"{'no site' if r['needs_website'] == 'yes' else 'has site'} | "
              f"{r.get('review_count','')} rev | {r['priority_score']} | {r['priority']}")


if __name__ == "__main__":
    main()
