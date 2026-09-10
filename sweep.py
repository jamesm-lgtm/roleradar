#!/usr/bin/env python3
"""Roleradar: daily sweep of target companies' ATS job boards for marketing roles.

Usage:
    python sweep.py            # fetch, filter, update seen.json, write outputs
    python sweep.py --dry-run  # same, but don't touch seen.json (handy for testing filters)

Reads:   companies.yaml, filters.yaml, seen.json (state; created on first run)
Writes:  seen.json, new_roles.csv (append-only log), all_current.csv, docs/index.html

Only requests + pyyaml + stdlib. Every fetch failure is logged and skipped;
the run never dies because one board is broken.
"""

import argparse
import csv
import html
import json
import logging
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

HERE = Path(__file__).resolve().parent
COMPANIES_FILE = HERE / "companies.yaml"
FILTERS_FILE = HERE / "filters.yaml"
SEEN_FILE = HERE / "seen.json"
NEW_CSV = HERE / "new_roles.csv"
ALL_CSV = HERE / "all_current.csv"
HTML_OUT = HERE / "docs" / "index.html"

LONDON = ZoneInfo("Europe/London")
TIMEOUT = 20
HEADERS = {"User-Agent": "roleradar/1.0 (personal job sweep; contact via GitHub)"}
# Forget a job we haven't seen on any board for this long, so seen.json doesn't grow forever.
FORGET_AFTER_DAYS = 90

CSV_COLUMNS = ["date_found", "company", "lane", "title", "location", "salary", "url"]

log = logging.getLogger("roleradar")


# --------------------------------------------------------------------------- fetchers
# Each fetcher returns a list of plain dicts with the same shape:
#   id, title, location, url, salary, posted (ISO date string or "")
# They only read the fields they need and tolerate missing ones.


def _get_json(url, params=None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _iso_date(value):
    """Best-effort: turn an ATS timestamp (ISO string or epoch ms) into YYYY-MM-DD."""
    if not value:
        return ""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date().isoformat()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError, OSError):
        return ""


def fetch_greenhouse(token):
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": str(j.get("id", "")),
            "title": j.get("title", "") or "",
            "location": (j.get("location") or {}).get("name", "") or "",
            "url": j.get("absolute_url", "") or "",
            "salary": "",  # not in the list endpoint
            "posted": _iso_date(j.get("first_published") or j.get("updated_at")),
        })
    return jobs


def fetch_lever(token):
    data = _get_json(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    if not isinstance(data, list):
        raise ValueError("Lever response was not a list")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        locs = [cats.get("location", "")] + list(j.get("allLocations") or [])
        location = "; ".join(dict.fromkeys(l for l in locs if l))
        if j.get("workplaceType") in ("remote", "hybrid"):
            location = f"{location} ({j['workplaceType']})" if location else j["workplaceType"]
        sal = j.get("salaryRange") or {}
        salary = ""
        if sal.get("min") or sal.get("max"):
            salary = f"{sal.get('currency', '')} {sal.get('min', '')}-{sal.get('max', '')} {sal.get('interval', '')}".strip()
        jobs.append({
            "id": str(j.get("id", "")),
            "title": j.get("text", "") or "",
            "location": location,
            "url": j.get("hostedUrl", "") or "",
            "salary": salary,
            "posted": _iso_date(j.get("createdAt")),
        })
    return jobs


def fetch_ashby(token):
    data = _get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{token}",
        params={"includeCompensation": "true"},
    )
    jobs = []
    for j in data.get("jobs", []):
        locs = [j.get("location", "")] + [
            (s or {}).get("location", "") for s in (j.get("secondaryLocations") or [])
        ]
        location = "; ".join(dict.fromkeys(l for l in locs if l))
        if j.get("isRemote") and "remote" not in location.lower():
            location = f"{location} (remote)" if location else "Remote"
        comp = j.get("compensation") or {}
        salary = comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary") or ""
        jobs.append({
            "id": str(j.get("id", "")),
            "title": j.get("title", "") or "",
            "location": location,
            "url": j.get("jobUrl", "") or j.get("applyUrl", "") or "",
            "salary": salary,
            "posted": _iso_date(j.get("publishedAt")),
        })
    return jobs


def fetch_workable(token):
    data = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{token}")
    jobs = []
    for j in data.get("jobs", []):
        parts = [j.get("city", ""), j.get("state", ""), j.get("country", "")]
        location = ", ".join(p for p in parts if p)
        if j.get("remote") or (j.get("workplace") or "").lower() in ("remote", "hybrid"):
            tag = (j.get("workplace") or "remote").lower()
            location = f"{location} ({tag})" if location else tag
        jobs.append({
            "id": str(j.get("shortcode") or j.get("id", "")),
            "title": j.get("title", "") or "",
            "location": location,
            "url": j.get("url", "") or j.get("application_url", "") or "",
            "salary": "",
            "posted": _iso_date(j.get("published_on") or j.get("created_at")),
        })
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
}


# --------------------------------------------------------------------------- filters


def _word_re(phrases):
    """Compile a list of phrases into one case-insensitive whole-word regex."""
    if not phrases:
        return re.compile(r"(?!x)x")  # never matches
    alts = [r"\s+".join(re.escape(w) for w in p.split()) for p in phrases]
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])", re.I)


class Filters:
    def __init__(self, cfg):
        self.include = _word_re(cfg.get("title_keywords", []))
        self.exclude = _word_re(cfg.get("title_exclude", []))
        self.fixed_term = _word_re(cfg.get("fixed_term_patterns", []))
        self.senior = _word_re(cfg.get("senior_keywords", []))
        self.london = _word_re(cfg.get("location_london", ["london"]))
        self.flexible = _word_re(cfg.get("location_flexible", []))
        self.uk_country = _word_re(cfg.get("location_uk_country", []))
        self.non_uk = _word_re(cfg.get("location_non_uk", []))

    def title_ok(self, title):
        if not self.include.search(title):
            return False
        if self.exclude.search(title):
            return False
        if self.fixed_term.search(title) and not self.senior.search(title):
            return False
        return True

    def location_ok(self, location):
        loc = location.strip()
        if not loc:
            return True  # blank: benefit of the doubt
        if self.london.search(loc):
            return True
        if self.non_uk.search(loc):
            return False
        if self.flexible.search(loc):
            return True
        # Strip country-level UK words and separators; if nothing is left it's
        # "United Kingdom" with no city, which we keep. If a city remains
        # (Manchester, Cardiff...) it's UK but not London, so drop it.
        rest = self.uk_country.sub("", loc)
        rest = re.sub(r"[\s,;/()\-–]+", "", rest)
        return rest == ""

    def matches(self, job):
        return self.title_ok(job["title"]) and self.location_ok(job["location"])


# --------------------------------------------------------------------------- state


def load_seen():
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text())
        except json.JSONDecodeError as e:
            log.error("seen.json is corrupt (%s); starting fresh", e)
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=1, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- outputs


def write_all_csv(rows):
    with ALL_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def append_new_csv(rows):
    """new_roles.csv is an append-only log, newest run at the top."""
    existing = []
    if NEW_CSV.exists():
        with NEW_CSV.open(newline="") as f:
            existing = list(csv.DictReader(f))
    with NEW_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        w.writerows(existing)


def days_live(row, today):
    start = row.get("posted") or row.get("date_found")
    try:
        return (today - date.fromisoformat(start)).days
    except (ValueError, TypeError):
        return ""


def write_html(rows, manual, errors, today, run_stamp):
    e = html.escape

    def tr(r):
        new = ' class="new"' if r["is_new"] else ""
        badge = '<span class="badge">new</span> ' if r["is_new"] else ""
        return (
            f"<tr{new}>"
            f"<td>{e(r['date_found'])}</td>"
            f"<td>{badge}<a href=\"{e(r['url'])}\" target=\"_blank\" rel=\"noopener\">{e(r['title'])}</a>"
            f"<div class=\"sub\">{e(r['company'])} · {e(r['lane'])}</div></td>"
            f"<td>{e(r['location'])}</td>"
            f"<td>{e(r['salary'])}</td>"
            f"<td class=\"num\">{days_live(r, today)}</td>"
            "</tr>"
        )

    def manual_li(c):
        url = c.get("url", "")
        link = f'<a href="{e(url)}" target="_blank" rel="noopener">{e(c["name"])}</a>' if url else e(c["name"])
        note = f' <span class="sub">{e(c.get("note", ""))}</span>' if c.get("note") else ""
        return f"<li>{link} <span class=\"sub\">({e(c['lane'])})</span>{note}</li>"

    errors_html = ""
    if errors:
        errors_html = "<h2>Fetch errors this run</h2><ul>" + "".join(
            f"<li>{e(name)}: {e(msg)}</li>" for name, msg in errors
        ) + "</ul>"

    n_new = sum(1 for r in rows if r["is_new"])
    doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Roleradar</title>
<style>
  body {{ font: 15px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; margin: 0; padding: 12px; color: #111; background: #fff; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 28px 0 8px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 12px; }}
  input {{ width: 100%; box-sizing: border-box; padding: 8px; font-size: 15px; border: 1px solid #bbb; border-radius: 6px; margin-bottom: 12px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 8px 6px; border-bottom: 1px solid #e5e5e5; vertical-align: top; }}
  th {{ font-size: 12px; text-transform: uppercase; color: #666; letter-spacing: .03em; }}
  td.num {{ text-align: right; white-space: nowrap; }}
  tr.new {{ background: #fffbe6; }}
  .badge {{ display: inline-block; background: #d97706; color: #fff; font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 4px; vertical-align: middle; }}
  .sub {{ color: #666; font-size: 13px; }}
  a {{ color: #0b5bd3; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  ul {{ padding-left: 20px; }}
  li {{ margin-bottom: 4px; }}
  @media (max-width: 600px) {{ .hide-sm {{ display: none; }} }}
</style>
</head>
<body>
<h1>Roleradar</h1>
<div class="meta">Updated {e(run_stamp)} · {len(rows)} matching roles · {n_new} new this run</div>
<input id="q" type="search" placeholder="Filter by title, company, lane, location…" autocomplete="off">
<table id="roles">
<thead><tr><th>Found</th><th>Role</th><th>Location</th><th class="hide-sm">Salary</th><th>Days live</th></tr></thead>
<tbody>
{''.join(tr(r) for r in rows)}
</tbody>
</table>
{errors_html}
<h2>Manual check (not on a polled ATS)</h2>
<ul>
{''.join(manual_li(c) for c in manual)}
</ul>
<script>
  // Tiny client-side filter; no dependencies.
  var q = document.getElementById('q'), rows = document.querySelectorAll('#roles tbody tr');
  q.addEventListener('input', function () {{
    var t = q.value.toLowerCase();
    rows.forEach(function (r) {{ r.hidden = t && r.textContent.toLowerCase().indexOf(t) === -1; }});
  }});
</script>
</body>
</html>
"""
    HTML_OUT.parent.mkdir(parents=True, exist_ok=True)
    HTML_OUT.write_text(doc)


# --------------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="don't update seen.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    now = datetime.now(LONDON)
    today = now.date()
    today_s = today.isoformat()
    run_stamp = now.strftime("%a %d %b %Y, %H:%M %Z")

    companies = yaml.safe_load(COMPANIES_FILE.read_text())["companies"]
    filters = Filters(yaml.safe_load(FILTERS_FILE.read_text()) or {})
    seen = load_seen()

    current, manual, errors = [], [], []
    for c in companies:
        ats = (c.get("ats") or "none").lower()
        if ats == "none":
            manual.append(c)
            continue
        fetcher = FETCHERS.get(ats)
        if not fetcher or not c.get("token"):
            log.warning("%s: unknown ats %r or missing token; skipping", c["name"], ats)
            errors.append((c["name"], f"bad config: ats={ats!r}, token={c.get('token')!r}"))
            continue
        try:
            jobs = fetcher(c["token"])
        except Exception as ex:  # noqa: BLE001 - never let one board kill the run
            log.error("%s (%s/%s): %s", c["name"], ats, c["token"], ex)
            errors.append((c["name"], f"{type(ex).__name__}: {ex}"))
            continue

        matched = [j for j in jobs if filters.matches(j)]
        log.info("%-24s %-10s %4d jobs, %3d match", c["name"], ats, len(jobs), len(matched))
        for j in matched:
            key = f"{ats}:{c['token']}:{j['id']}"
            current.append({
                "key": key,
                "company": c["name"],
                "lane": c.get("lane", ""),
                **j,
            })

    # Dedupe against state. A job is "new" if its key has never been recorded.
    new_rows = []
    for r in current:
        entry = seen.get(r["key"])
        r["is_new"] = entry is None
        r["date_found"] = entry["first_seen"] if entry else today_s
        if r["is_new"]:
            new_rows.append(r)
        seen[r["key"]] = {
            "first_seen": r["date_found"],
            "last_seen": today_s,
            "title": r["title"],
            "company": r["company"],
        }

    # Forget jobs that have been gone for a long time.
    for key in list(seen):
        try:
            gone_days = (today - date.fromisoformat(seen[key].get("last_seen", today_s))).days
        except ValueError:
            gone_days = 0
        if gone_days > FORGET_AFTER_DAYS:
            del seen[key]

    # Newest first; within a day, alphabetical by company then title.
    current.sort(key=lambda r: (r["date_found"], r["company"], r["title"]))
    current.sort(key=lambda r: r["date_found"], reverse=True)
    new_rows.sort(key=lambda r: (r["company"], r["title"]))

    write_all_csv(current)
    write_html(current, manual, errors, today, run_stamp)

    if args.dry_run:
        # Don't touch the two files that carry state across runs.
        log.info("dry run: seen.json and new_roles.csv not updated")
    else:
        if new_rows or not NEW_CSV.exists():
            append_new_csv(new_rows)
        save_seen(seen)

    log.info(
        "done: %d current matches, %d new, %d companies on manual check, %d fetch errors",
        len(current), len(new_rows), len(manual), len(errors),
    )
    for r in new_rows:
        log.info("NEW  %s — %s (%s)", r["company"], r["title"], r["location"])


if __name__ == "__main__":
    main()
