# Current job refresh

`python scrape_jobs.py --daily-refresh` runs the gaming boards, a 21-day Indeed
search, CSU Careers, ScholarshipDB, and HigherEdJobs. GitHub Actions runs this
command daily at 13:11 UTC and publishes the generated results to this fork.
It explicitly requests a GitHub Pages rebuild after publishing the data.
Existing watchers also contribute candidates between daily runs.

The dashboard reads the authoritative `output/all_jobs.json`. A role needs a
posting date within 21 calendar days and a successful listing check within the
last two calendar days. Undated, expired, future-dated, unrelated, and duplicate
jobs cannot satisfy the minimum. Employer first-publication dates take priority
over a board's newly-added date or an employer's last-updated date.

Fresh candidates are retained in `output/candidates.json` before the pay filter.
Starting at the configured pay floors, the selector reduces hourly floors in
$5 steps (and the outside-radius annual comparison proportionally) until at
least five unique roles pass. If published pay cannot supply five roles at any
floor, the final fallback includes undisclosed or non-comparable pay when
`filters.adaptive_pay.allow_undisclosed_when_short` is true. Such roles are
labeled; pay is never invented. Role relevance, stated employment schedule, and
the 100-mile part-time limit remain mandatory. Part-time hourly pay is never
annualized. Target music-technology postdocs retain their pay exception.

If fewer than five compatible live roles actually exist, the output reports
`selection.target_met=false`; it does not pad the list with old or unrelated
roles. `output/source_health.json` records source errors, access blocks, parsing
gaps, and resource-only sources. Today's listing cache is reused between runs
and checked again the following day.

## Gaming sources

ASGC uses its public job feed. Hitmarker uses its public search configuration.
Games Jobs Direct, Remote Game Jobs, GameJobs.co, ArtStation, Grackle HQ,
Skillsearch, and Work With Indies are checked through their public pages.
Unavailable pages or feeds are reported as such, rather than described as
successful scrapes. Each discovered relevant listing is checked at its direct
destination, including public employer ATS details where available.

Alexander Rehm's Video Game Jobs now lives at Games Jobs Index. Its published
[reuse policy](https://gamesjobsindex.com/scraping.html) prohibits automated
harvesting of its compiled database, so the dashboard provides a resource link.
PlayHire is also linked as a resource because its Discord jobs require account
access. The supplied GameDevJobs URL duplicates GameJobs.co and is not scraped
twice.
