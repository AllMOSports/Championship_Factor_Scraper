"""
scrape_mshsaa_history.py
 
Scrapes MSHSAA "Activity History" pages (ActivityHistory.aspx) for a list of
school/sport combinations and extracts, for every season:
  - Year, Record, Winning %, Home/Away/Neutral splits, PPG, Opponent PPG
  - Every postseason placement attached to that season:
      District Champion, State Champion, State Runner-Up,
      State 3rd Place, State 4th Place
    These icons are NOT all marked up the same way on MSHSAA's pages --
    District Champion carries a `title` attribute directly; the four
    State-level placements have no title at all and are identified purely
    by CSS class (color + icon variant), and one of them (State 4th Place)
    even uses a <span> instead of an <i> tag. The parser checks title first,
    then falls back to a known-class lookup, and as a last resort surfaces
    any *unrecognized* icon's raw class string instead of silently dropping
    it -- so a 6th placement type we haven't seen yet would show up in the
    output as "Unrecognized icon (...)" rather than vanishing.
 
INPUT
-----
A CSV with columns: school_id,school_name,sport,alg_id
  - school_id   -> MSHSAA "s" URL parameter (numeric)
  - school_name -> any label you want in the output (not used in the request)
  - sport       -> any label you want in the output (e.g. "Football")
  - alg_id      -> MSHSAA "alg" URL parameter for that sport (numeric)
 
One row per (school, sport) you want scraped. For 50-75 schools across 9
sports that's up to ~675 rows.
 
RE-RUNNING / RESUMING
----------------------
By default, this script skips (school_id, sport) combos already present in
--output, so a partial run can be safely continued. IMPORTANT: if you're
re-running this after a code change that affects parsing (like the
placement-detection logic), pass --overwrite (or delete the existing output
file) so every row gets re-scraped with the updated logic -- otherwise
already-cached rows keep whatever was parsed under the OLD code and never
get refreshed.
 
OUTPUT
------
A JSON file: a list of records, one per (school, sport, year), e.g.:
  {
    "school_id": "538",
    "school_name": "Principia",
    "sport": "Girls Basketball",
    "year": 2026,
    "record": "30-2",
    "win_pct": "93.8%",
    "home": "4-1",
    "away": "4-0",
    "neutral": "22-1",
    "ppg": "65.8",
    "opp_ppg": "44",
    "placements": ["State Champion"]
  }
 
A separate JSON file of any failed (school, sport) combos is written too, so
you can inspect/retry just those instead of re-running everything.
 
USAGE
-----
    pip install requests beautifulsoup4
 
    # Fresh full re-scrape (use this after the placement-detection fix):
    python scrape_mshsaa_history.py --input teams_to_scrape.csv --output history_results.json --overwrite
 
    # Normal resumable run (skips combos already in --output):
    python scrape_mshsaa_history.py --input teams_to_scrape.csv --output history_results.json
 
Designed to drop straight into a GitHub Actions job: no interactive prompts,
predictable logging, retry/backoff for flaky responses, resumable output.
"""
 
import argparse
import csv
import json
import logging
import time
from pathlib import Path
 
import requests
from bs4 import BeautifulSoup
 
BASE_URL = "https://www.mshsaa.org/MySchool/ActivityHistory.aspx"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
 
# Be polite -- this is a small public ASP.NET site, not an API meant for bulk hits.
REQUEST_DELAY_SECONDS = 1.5
REQUEST_TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 4
 
# Only keep seasons within this range (inclusive). Override with --min-year/--max-year.
DEFAULT_MIN_YEAR = 2020
DEFAULT_MAX_YEAR = 2026
 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
 
 
def fetch_html(url: str) -> str:
    """Fetch a URL with retries + backoff. Raises on final failure."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("Attempt %d/%d failed for %s: %s", attempt, MAX_RETRIES, url, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts") from last_exc
 
 
# Known placement icons that carry NO title attribute -- the placement is encoded
# purely in the CSS class instead (e.g. "fas fa-trophy-alt gold xxl no-padding" for
# State Champion). Add to this dict as more class signatures are confirmed.
KNOWN_ICON_CLASS_LABELS = {
    frozenset(["fa-trophy-alt", "gold"]): "State Champion",
    frozenset(["fa-trophy-alt", "silver"]): "State Runner-Up",
    frozenset(["fa-trophy-alt", "bronze"]): "State 3rd Place",
    frozenset(["fa-trophy-alt", "black"]): "State 4th Place",
}
 
 
def label_icon(icon):
    """Return a human-readable placement label for a single <i> icon element.
 
    Prefers the `title` attribute when present (e.g. "District Champion").
    Falls back to matching known CSS class signatures (e.g. State Champion,
    which has no title at all). If an icon matches neither, its raw class
    string is returned instead of silently dropping it -- so new placement
    types show up in the output for manual confirmation rather than vanishing.
    """
    title = (icon.get("title") or "").strip()
    if title:
        return title
 
    classes = set(icon.get("class", []))
    if not classes:
        return None
 
    for class_signature, label in KNOWN_ICON_CLASS_LABELS.items():
        if class_signature.issubset(classes):
            return label
 
    return f"Unrecognized icon ({' '.join(sorted(classes))})"
 
 
def find_icon_elements(tr):
    """Find every Font-Awesome icon element in a row, regardless of tag name.
 
    MSHSAA uses <i> for some placement icons (e.g. District Champion) and
    <span> for others (e.g. State 4th Place) -- both carry an "fa-..." class,
    so we match on that instead of assuming a specific tag.
    """
    return tr.find_all(
        lambda tag: tag.has_attr("class") and any(c.startswith("fa-") for c in tag["class"])
    )
 
 
def find_history_table(soup: BeautifulSoup):
    """Locate the detailed-history table by matching its header row (Year + Record)."""
    for table in soup.find_all("table"):
        header_cells = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not header_cells:
            first_row = table.find("tr")
            if first_row:
                header_cells = [td.get_text(strip=True).lower() for td in first_row.find_all("td")]
        if "year" in header_cells and "record" in header_cells:
            return table, header_cells
    return None, []
 
 
def parse_history_page(html: str, school_id: str, school_name: str, sport: str,
                        min_year: int = DEFAULT_MIN_YEAR, max_year: int = DEFAULT_MAX_YEAR):
    """Parse a single ActivityHistory.aspx page into a list of season records.
 
    Only seasons where min_year <= year <= max_year are kept -- the page
    itself returns full history (2008-present), so filtering happens here
    rather than by changing the request.
    """
    soup = BeautifulSoup(html, "html.parser")
    table, header_cells = find_history_table(soup)
    if table is None:
        # Surface what we actually got back instead of just "not found" --
        # this distinguishes a genuine no-data page from a block/rate-limit
        # page, a captcha, or a structural change on MSHSAA's end.
        page_title = soup.title.get_text(strip=True) if soup.title else "(no <title>)"
        visible_text = soup.get_text(separator=" ", strip=True)
        snippet = visible_text[:300]
        log.warning("No history table found for %s / %s (s=%s)", school_name, sport, school_id)
        log.warning("  Page title: %s", page_title)
        log.warning("  HTML length: %d chars | Visible text snippet: %s", len(html), snippet)
        return []
 
    # Map header name -> column index (the icon column has a blank header, so it's skipped here
    # and handled separately below via the <i title="..."> search)
    col_index = {name: i for i, name in enumerate(header_cells) if name}
 
    rows_out = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue  # header row, no <td>s
 
        # Placement icons: every Font-Awesome icon element in the row (regardless
        # of whether it's an <i> or <span>), labeled via label_icon() -- which
        # checks title first, then known CSS class signatures, then falls back
        # to surfacing the raw class so nothing is silently dropped.
        placements = [
            label for icon in find_icon_elements(tr)
            for label in [label_icon(icon)]
            if label
        ]
 
        def cell_text(col_name):
            idx = col_index.get(col_name)
            if idx is None or idx >= len(cells):
                return ""
            return cells[idx].get_text(strip=True)
 
        year_text = cell_text("year")
        if not year_text or not year_text[:4].isdigit():
            continue  # not a real data row
 
        year = int(year_text[:4])
        if year < min_year or year > max_year:
            continue  # outside the requested range
 
        rows_out.append({
            "school_id": school_id,
            "school_name": school_name,
            "sport": sport,
            "year": year,
            "record": cell_text("record"),
            "win_pct": cell_text("winning %"),
            "home": cell_text("home"),
            "away": cell_text("away"),
            "neutral": cell_text("neutral"),
            "ppg": cell_text("ppg"),
            "opp_ppg": cell_text("opp ppg"),
            "placements": placements,
        })
    return rows_out
 
 
def load_input_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"school_id", "school_name", "sport", "alg_id"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Input CSV missing columns: {missing}")
        return list(reader)
 
 
def main():
    parser = argparse.ArgumentParser(description="Scrape MSHSAA ActivityHistory pages.")
    parser.add_argument("--input", default="teams_to_scrape.csv", help="Input CSV path")
    parser.add_argument("--output", default="history_results.json", help="Output JSON path")
    parser.add_argument("--errors", default="failed_combos.json", help="Failed-fetch log path")
    parser.add_argument("--overwrite", action="store_true",
                         help="Re-scrape combos that already exist in --output")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY_SECONDS,
                         help="Seconds to wait between requests")
    parser.add_argument("--min-year", type=int, default=DEFAULT_MIN_YEAR,
                         help="Earliest season to keep (inclusive)")
    parser.add_argument("--max-year", type=int, default=DEFAULT_MAX_YEAR,
                         help="Latest season to keep (inclusive)")
    args = parser.parse_args()
 
    input_path = Path(args.input)
    output_path = Path(args.output)
    errors_path = Path(args.errors)
 
    rows = load_input_rows(input_path)
    log.info("Loaded %d (school, sport) combos from %s", len(rows), input_path)
 
    existing_results = []
    skip_keys = set()
    if output_path.exists() and not args.overwrite:
        try:
            existing_results = json.loads(output_path.read_text(encoding="utf-8"))
            skip_keys = {(r["school_id"], r["sport"]) for r in existing_results}
            if skip_keys:
                log.info("Resuming: %d combos already scraped, will skip them", len(skip_keys))
        except (json.JSONDecodeError, OSError):
            existing_results = []
 
    all_results = list(existing_results)
    failed = []
 
    for i, row in enumerate(rows, start=1):
        school_id = row["school_id"].strip()
        school_name = row["school_name"].strip()
        sport = row["sport"].strip()
        alg_id = row["alg_id"].strip()
        key = (school_id, sport)
 
        if key in skip_keys:
            continue
 
        url = f"{BASE_URL}?s={school_id}&alg={alg_id}"
        log.info("[%d/%d] %s / %s (s=%s, alg=%s)", i, len(rows), school_name, sport, school_id, alg_id)
 
        try:
            html = fetch_html(url)
            season_rows = parse_history_page(html, school_id, school_name, sport,
                                              min_year=args.min_year, max_year=args.max_year)
            all_results.extend(season_rows)
            log.info("  -> %d seasons parsed", len(season_rows))
        except Exception as exc:
            log.error("  -> FAILED: %s", exc)
            failed.append({"school_id": school_id, "school_name": school_name,
                            "sport": sport, "alg_id": alg_id, "url": url, "error": str(exc)})
 
        time.sleep(args.delay)
 
    output_path.write_text(json.dumps(all_results, indent=2), encoding="utf-8")
    log.info("Wrote %d season records to %s", len(all_results), output_path)
 
    if failed:
        errors_path.write_text(json.dumps(failed, indent=2), encoding="utf-8")
        log.warning("%d combos failed -- see %s to retry just those", len(failed), errors_path)
    elif errors_path.exists():
        errors_path.unlink()
 
 
if __name__ == "__main__":
    main()
