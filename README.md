# MSHSAA Activity History Scraper

Scrapes `ActivityHistory.aspx` pages on mshsaa.org and pulls year-by-year
records plus postseason placements (District Champion, State Champion,
State Runner-Up, etc.) straight from the icon `title` attributes -- no
guessing based on color/class needed.

## Setup

```bash
pip install requests beautifulsoup4
```

## 1. Fill in `teams_to_scrape_template.csv`

One row per (school, sport) combo you want. Columns:

- `school_id` -- the `s=` value from the school's MSHSAA URL
- `school_name` -- whatever label you want in the output (doesn't affect the request)
- `sport` -- whatever label you want in the output (e.g. "Football")
- `alg_id` -- the `alg=` value for that sport

The template has one row already filled in (St. Vincent / Football,
confirmed working). Copy that pattern for the rest of your 50-75 schools.

### Known `alg=` sport codes

Pulled from the nav menu on a real school page, so these are confirmed for
at least these sports (MSHSAA appears to use the same `alg` codes
site-wide, but worth spot-checking one new sport before trusting it
wholesale):

| Sport                      | alg |
|-----------------------------|-----|
| Baseball - Spring Season    | 3   |
| Basketball - Boys           | 5   |
| Basketball - Girls          | 6   |
| Sideline Cheerleading       | 9   |
| Cross Country - Boys        | 11  |
| Cross Country - Girls       | 12  |
| Football - 11 Man           | 19  |
| Golf - Boys                 | 23  |
| Music Activities             | 29  |
| Soccer - Boys                | 33  |
| Soccer - Girls                | 34  |
| Softball - Fall Season        | 38  |
| Track and Field - Boys        | 52  |
| Track and Field - Girls       | 53  |
| Volleyball - Girls             | 57  |

If one of your 9 sports isn't on this list (e.g. wrestling, swimming), open
that sport's Schedule/History page for any school and check the `alg=`
value in the URL bar.

## 2. Find each school's `s=` ID

Visit any page under a school's MySchool section
(`mshsaa.org/MySchool/...?s=###`) and read the `s=` value out of the URL.

## 3. Run it

```bash
python scrape_mshsaa_history.py --input teams_to_scrape.csv --output history_results.json
```

This will:
- Hit each (school, sport) URL once, with a 1.5s delay between requests
  (~675 rows takes roughly 15-20 minutes -- adjust with `--delay` if needed)
- Retry up to 3 times on a flaky/failed request before giving up on that combo
- Write every parsed season as one record to `history_results.json`
- Write any combos that failed all retries to `failed_combos.json` for
  re-checking later

It's safe to re-run: by default it skips any (school, sport) combo already
present in `--output`, so if it gets interrupted partway through (or you add
more rows to the CSV later) you can just run the same command again.

## Output format

```json
{
  "school_id": "554",
  "school_name": "St. Vincent",
  "sport": "Football",
  "year": 2023,
  "record": "9-4",
  "win_pct": "69.2%",
  "home": "3-1",
  "away": "6-2",
  "neutral": "0-1",
  "ppg": "33.4",
  "opp_ppg": "16.5",
  "placements": ["District Champion"]
}
```

`placements` is a list because a single season can carry more than one icon
(e.g. District Champion *and* State Runner-Up both appearing in the same
row) -- the parser doesn't assume only one.

## A heads-up on label text

We've only directly confirmed one exact string so far: `"District
Champion"`. The script doesn't hardcode a list of expected labels -- it
just captures whatever text is in each icon's `title` attribute -- so it
will correctly pick up "State Champion," "State Runner-Up," "State 3rd
Place," "State 4th Place," etc. once it hits a school/season that has them.
Worth eyeballing `history_results.json` after the first run to confirm the
exact phrasing MSHSAA uses for each (in case "Runner-Up" vs "Runner Up" vs
"2nd Place" varies), since that affects how you'd filter/group on the
`placements` field downstream in `schools.json`.

## GitHub Actions

Drop `scrape_mshsaa_history.py` and your filled-in CSV into your existing
pipeline repo and call it from a workflow step the same way you're already
running `build_schools_json.py` / the SOR scripts -- no special permissions
or secrets needed, it's a plain unauthenticated GET against public pages.
