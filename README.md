# 🧬 Bay Area MLE / DS Job Scraper

Three GitHub Actions workflows that scrape **software engineering, ML/AI, data science, data engineering, platform/infra/security, and biotech informatics roles** in the SF Bay Area and email the results as an HTML digest.

## What It Does

### 1. Biotech LinkedIn digest — daily at 8pm PT, last 24h
Hits LinkedIn's public guest endpoint for SF Bay Area MLE/DS roles posted in the last 24 hours, then post-filters results to a **biotech company allowlist** derived from `CURATED_BIOTECHS` in `scrape_jobs.py` (10x Genomics, Twist, Maze, Freenome, Cytokinetics, Natera, Inceptive, Atomwise, Profluent, Eikon, Altos Labs, Arc Institute, Caribou, Octant, Genentech, Gilead). Add to that list to expand coverage.

Output goes to `jobs.json`, `jobs.md`, and `jobs.html`. Each run dedupes against the previously-committed `jobs.json` so the email surfaces only postings new since the last run.

> Why allowlist instead of LinkedIn's industry filter? The `f_I` industry parameter is silently ignored on the public guest endpoint (verified by probing IDs 12, 14, 16, 1763, 1862 — all returned identical non-biotech results).

### 2. LinkedIn MLE/DS watcher — every 2 hours, last 2h
Hits LinkedIn's public guest endpoint for SF Bay Area roles posted in **the last 2 hours** across multiple search terms, dedupes by job ID, and sorts by recency. Output goes to `linkedin_jobs.json`, `linkedin_jobs.md`, and `linkedin_jobs.html`.

Runs every 2 hours from **8am to 10pm Pacific time**. Cron is fixed in UTC; in PST (UTC-8) the schedule shifts to 7am–9pm PT — acceptable seasonal drift. Each run dedupes against the previous run so empty windows don't trigger an email.

> ⚠️ Uses the unauthenticated public guest endpoint only — **never** signs in with a user account and does not use LinkedIn cookies, tokens, or credentials.

### 3. Indeed MLE/DS watcher — every 1h, last 1h
Uses [`python-jobspy`](https://pypi.org/project/python-jobspy/) (Indeed's public RSS and Publisher API were both deprecated in 2026; the site sits behind Cloudflare's top-tier bot product, so stdlib `urllib` is blocked at the edge). JobSpy uses Indeed's mobile-app API internally — no proxies required, no documented rate limit. Output goes to `indeed_jobs.json`, `indeed_jobs.md`, and `indeed_jobs.html`, deduped against the previous run.

Scheduled externally by cron-job.org at :47 PT, offset from the LinkedIn :17 slot to spread Gmail sends and reduce contention on the shared commit-push concurrency group.

## Keywords Matched

A title is included if it contains any of (case-insensitive substring match):

**ML / AI:** `machine learning engineer`, `ml engineer`, `mle`, `machine learning infra`, `ml platform`, `ai platform`, `ai engineer`, `ai/ml engineer`, `mlops`, `research engineer`, `llm engineer`, `generative ai`, `genai engineer`, `prompt engineer`, `deep learning`, `reinforcement learning`, `computer vision`, `nlp engineer`

**Applied / scientist:** `applied scientist`, `ai scientist`, `ml scientist`, `data scientist`, `data science`

**Software engineering:** `software engineer`, `software developer`, `backend engineer`, `back-end engineer`, `backend developer`, `frontend engineer`, `front-end engineer`, `frontend developer`, `full stack engineer`, `full-stack engineer`, `fullstack engineer`, `mobile engineer`, `ios engineer`, `android engineer`

**Platform / infra / ops:** `platform engineer`, `infrastructure engineer`, `infra engineer`, `systems engineer`, `distributed systems`, `cloud engineer`, `devops engineer`, `devops`, `site reliability engineer`, `security engineer`

**Data engineering:** `data engineer`, `data engineering`, `analytics engineer`, `data platform`, `data infrastructure`, `etl engineer`, `etl developer`

**Robotics / perception:** `robotics engineer`, `perception engineer`

**Computational / informatics (biotech):** `computational scientist`, `computational biologist`, `bioinformatics scientist`, `bioinformatics engineer`, `cheminformatics`

## Output Files

| File | Source | Description |
|---|---|---|
| `jobs.json` / `.md` / `.html` | Biotech LinkedIn digest | Allowlisted biotech-company roles in the last 24h, deduped against the previous run |
| `linkedin_jobs.json` / `.md` / `.html` | LinkedIn watcher | Roles posted in the last 2h, deduped against the previous run |
| `indeed_jobs.json` / `.md` / `.html` | Indeed watcher | Indeed-sourced roles posted in the last 1h, deduped against the previous run |
| `checked_companies.json` | (legacy) | Tracking file from earlier Wikipedia-based discovery |

The `.html` files are styled email-ready digests; the `.md` files render nicely on GitHub.

Both workflows keep a GitHub history of generated digests: result files are committed when changed, and each scheduled workflow still runs `git push`.

### Interactive triage dashboard — `triage.html`

A single-file dashboard hosted on GitHub Pages that merges all three latest source JSONs into one filterable cockpit: search, role/seniority/source filters, save/applied/dismiss buttons persisted in localStorage, top-companies + role-mix charts, and an "export saved as Claude prompt" action.

**View it:** [`https://ernestod1998.github.io/Job_Scraper/triage.html`](https://ernestod1998.github.io/Job_Scraper/triage.html)

The dashboard fetches `jobs.json` / `linkedin_jobs.json` / `indeed_jobs.json` from the same repo at view time, so it always reflects the latest committed scrape. Refresh in the browser to see new data after a cron fire (Pages serves with ~1–2 min lag after each push). No bake-on-cron step in the scraper — `triage.html` is committed once and never modified by automation.

To run locally (e.g. to edit the dashboard UI):
```bash
python3 -m http.server 8000
# then visit http://localhost:8000/triage.html
```
Opening from `file://` won't work — the dashboard needs same-origin HTTP to `fetch()` the source JSONs.

## Setup

### Gmail secrets (for email delivery)

In **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `GMAIL_USER` | Gmail address |
| `GMAIL_APP_PASSWORD` | [Gmail App Password](https://myaccount.google.com/apppasswords) |

Both workflows email `GMAIL_USER` from `GMAIL_USER` via `smtp.gmail.com:465`.

### Run manually

From the **Actions** tab:
- *Biotech MLE Job Scraper* → Run workflow (biotech LinkedIn, last 24h)
- *LinkedIn MLE/DS Watcher* → Run workflow (general LinkedIn, last 2h)
- *Indeed MLE/DS Watcher* → Run workflow (Indeed via python-jobspy, last 2h)

Or locally:
```bash
python scrape_jobs.py --biotech-only   # biotech LinkedIn, last 24h, allowlist-filtered
python scrape_jobs.py --linkedin-only  # general MLE/DS LinkedIn, last 2h
python scrape_jobs.py --indeed-only    # general MLE/DS Indeed, last 1h (requires python-jobspy)
python scrape_jobs.py                  # legacy curated Greenhouse/Workday/Phenom sweep
```

Biotech and LinkedIn pipelines use only the standard library. The Indeed pipeline requires `pip install -r requirements.txt` (single dep: `python-jobspy`).

## Repo Structure

```
├── scrape_jobs.py                  # All scraping logic
├── requirements.txt                # python-jobspy (Indeed only; LinkedIn/biotech are stdlib)
├── jobs.{json,md,html}             # Curated biotech sweep output
├── linkedin_jobs.{json,md,html}    # LinkedIn last-2h output
├── indeed_jobs.{json,md,html}      # Indeed last-2h output
├── triage.html                     # Interactive dashboard (fetches the 3 JSONs at view time)
├── checked_companies.json          # Legacy tracking file
├── deep-dive/                      # Notes / analysis
└── .github/workflows/
    ├── scrape_jobs.yml             # Daily 8pm PT — biotech LinkedIn (last 24h, allowlist)
    ├── linkedin_watch.yml          # Hourly :17 PT — general LinkedIn (last 2h, cron-job.org-driven)
    ├── indeed_watch.yml            # Hourly :47 PT — Indeed (last 2h, cron-job.org-driven)
    └── linkedin_watch_backup.yml   # In-GH watchdog at :33 PT — re-dispatches missed runs
```

## ATS Endpoints Used

| ATS | Endpoint |
|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Workday | `https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` (POST) |
| Phenom (Genentech) | `https://careers.gene.com/us/en/search-results` (HTML + JSON-LD) |
| LinkedIn | `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search` (public guest) |
| Indeed | `python-jobspy` library (mobile-app API; no public endpoint since 2026 deprecation) |
