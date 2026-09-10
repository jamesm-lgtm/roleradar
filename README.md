# Roleradar

A small Python tool that checks the careers boards of a defined list of target
companies once a day, finds new marketing roles matching your filters, and
writes them to a CSV plus a phone-friendly HTML page. Runs on GitHub Actions
at 07:30 UK time. Review over coffee.

It does **not** scrape LinkedIn or Indeed. It polls the public JSON APIs of the
ATS platforms most companies use (Greenhouse, Lever, Ashby, Workable).
Companies on anything else (Workday, SmartRecruiters, Teamtailor, custom sites)
are listed on the output page under **Manual check** so you know what's not covered.

## Files

| File | What it is |
|---|---|
| `sweep.py` | The whole tool. `python sweep.py` is the only entry point. |
| `companies.yaml` | Your target list: name, ATS, token, lane. Edit freely. |
| `filters.yaml` | Title keywords, exclusions, location rules. Edit freely. |
| `seen.json` | State: every matching job ever seen, with first/last seen dates. Committed by the Action. |
| `new_roles.csv` | Append-only log of newly found roles, newest run at the top. |
| `all_current.csv` | Every currently-live matching role, newest first. |
| `docs/index.html` | The page. Published via GitHub Pages. |
| `.github/workflows/sweep.yml` | Daily cron + manual trigger. Commits results back. |

## Run it locally

```bash
pip install -r requirements.txt
python sweep.py
```

The first run has no `seen.json`, so every current match is "new". That's the
seed. Subsequent runs only flag jobs whose ID hasn't been recorded before.

Use `python sweep.py --dry-run` to fetch and write `all_current.csv` and the
HTML without touching `seen.json` or `new_roles.csv`, which is handy when
you're tuning filters.

Open `docs/index.html` in a browser to see the page.

## Set up on GitHub

This folder is designed to be its **own repository** (the Action commits CSVs
daily, and Pages serves `docs/`). To split it out:

```bash
cp -r roleradar ~/roleradar && cd ~/roleradar && git init && git add -A && git commit -m "roleradar v1"
```

Then create an empty repo on GitHub, push, and:

1. **Actions**: Settings → Actions → General → Workflow permissions →
   *Read and write permissions* (the workflow needs to push commits). Then run
   the "Daily job sweep" workflow once by hand from the Actions tab to seed
   `seen.json`.
2. **Pages**: Settings → Pages → Source: *Deploy from a branch* →
   Branch `main`, folder `/docs`. Your page will be at
   `https://<user>.github.io/<repo>/`. Bookmark it on your phone.

After that it runs itself. The cron fires at 06:30 and 07:30 UTC and the
workflow skips whichever one isn't 07:xx London time, so it stays at 07:30
across the BST/GMT switch. GitHub may delay scheduled runs by a few minutes
under load; that's normal.

Note: GitHub disables scheduled workflows on repos with no activity for 60
days. The bot's own commits count as activity, so this only bites if the
sweep finds nothing new for two months straight.

## Add a company

Add an entry to `companies.yaml`:

```yaml
  - name: Acme Money
    ats: greenhouse        # greenhouse | lever | ashby | workable | none
    token: acmemoney       # the board token / company slug
    lane: fintech          # any tag you like
```

To find the token, open the company's careers page and look at the job links:

| ATS | Job links look like | Token is |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/**acme**/jobs/123` or `job-boards.eu.greenhouse.io/**acme**/…` | `acme` |
| Lever | `jobs.lever.co/**acme**/…` | `acme` |
| Ashby | `jobs.ashbyhq.com/**acme**/…` | `acme` |
| Workable | `apply.workable.com/**acme**/` | `acme` |

You can sanity-check a token with curl before committing it:

```bash
curl -s "https://boards-api.greenhouse.io/v1/boards/acme/jobs" | head -c 300
```

The other endpoints are `https://api.lever.co/v0/postings/acme?mode=json`,
`https://api.ashbyhq.com/posting-api/job-board/acme` and
`https://apply.workable.com/api/v1/widget/accounts/acme`. Watch out for
namesakes: several tokens in the seed list (e.g. `wise`, `quilter`,
`netwealth`) exist on an ATS but belong to unrelated companies abroad.

If the company isn't on one of the four, use `ats: none` with a `url` and a
`note` so it shows up in the Manual check list.

## Change the filters

Everything is in `filters.yaml`. Matching is case-insensitive and on whole
words, so `crm` matches "CRM Manager" but not "Scrm", and `pr` matches
"PR Manager" but not "Product". (`lead` was dropped from `title_keywords`
because it pulled in every Lead Engineer; it still counts as senior for the
fixed-term rule.)

- `title_keywords`: a title must contain at least one of these.
- `title_exclude`: a title containing any of these is dropped.
- `fixed_term_patterns` / `senior_keywords`: fixed-term contracts are dropped
  unless the title also contains a senior keyword.
- Location: a role is kept if its location is blank, mentions London, says
  remote/hybrid, or is just "United Kingdom" with no city. Anything naming a
  non-UK place (without also saying London) is dropped, and so is a UK city
  other than London. Add to `location_non_uk` if a foreign city slips
  through; add to `location_london` if you want, say, `manchester` treated as
  in-scope.

Filter changes apply to the next run. Already-seen jobs stay in `seen.json`,
so loosening a filter surfaces the newly-matching jobs as "new" once.

## How it works

For each company with a supported ATS, `sweep.py` fetches the board's JSON,
normalises each posting to `id / title / location / url / salary / posted`,
applies the filters, and keys each match as `ats:token:id`. Keys not in
`seen.json` are new. It then writes `all_current.csv`, prepends new rows to
`new_roles.csv`, regenerates `docs/index.html` (newest first, with a "days
live" column from the ATS posting date), and saves `seen.json`. Jobs absent
from every board for 90 days are forgotten so the state file doesn't grow forever.

Any board that fails (network error, changed schema, 404) is logged, listed
under "Fetch errors" on the page, and skipped. One broken board never kills
the run.

## Seed list notes

- Tokens were verified against the live APIs on 2026-09-09.
- Cazoo was dropped (no longer trading). "LendlordInvest-type platforms" was
  a placeholder with no concrete company, so nothing was added for it.
- Some boards are global (HelloFresh, Wolt, Whoop, Strava, TaskRabbit, Hearst).
  They're included because the location filter handles them; expect most of
  their roles to be filtered out.
- Auto Trader is on Greenhouse but Manchester-based, so its roles mostly fail
  the London/remote rule. Add `manchester` to `location_london` if you want them.
- Zoopla's parent (Houseful) links to a Workable board that returned 404 at
  setup. Retry `ats: workable, token: houseful` later.

## v2 (not built)

- Email / Telegram digest of new roles
- Google Sheets output
- Salary parsing and normalisation (right now salary is whatever the ATS
  gives, which is usually nothing)
- Adzuna or similar aggregator API as a supplementary source for the
  "manual check" companies
