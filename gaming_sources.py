"""Public gaming boards, with employer publication dates taking precedence."""

import html
import json
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


SOURCES = [
    {"name": "ASGC", "url": "https://jobs.asgc.gg/", "api": "https://jobs.asgc.gg/api/job-listings?grouped=1"},
    {"name": "Games Jobs Index", "url": "https://gamesjobsindex.com/game-jobs/audio.html", "resource": "Publisher prohibits automated harvesting; browse its direct employer links."},
    {"name": "Games Jobs Direct", "url": "https://www.gamesjobsdirect.com/results?k=audio"},
    {"name": "Hitmarker", "url": "https://hitmarker.net/jobs", "adapter": "hitmarker"},
    {"name": "Remote Game Jobs", "url": "https://remotegamejobs.com/remote-game-music-sound-design-jobs"},
    {"name": "GameJobs.co", "url": "https://gamejobs.co/search?q=audio", "adapter": "gamejobs"},
    {"name": "GameDevJobs", "url": "https://gamejobs.co/", "resource": "The supplied GameDevJobs URL is the same as GameJobs.co; checked once."},
    {"name": "ArtStation", "url": "https://www.artstation.com/jobs"},
    {"name": "Grackle HQ", "url": "https://gracklehq.com/jobs?department=Audio", "adapter": "grackle"},
    {"name": "Skillsearch", "url": "https://www.skillsearch.com/search?keywords=audio"},
    {"name": "Work With Indies", "url": "https://www.workwithindies.com/categories/audio"},
    {"name": "PlayHire", "url": "https://playhire.io/", "resource": "Talent network and Discord jobs require account access; public resource link only."},
]


def visible_text(page):
    soup = BeautifulSoup(page, "html.parser")
    for node in soup.select('script, style, [hidden], .w-condition-invisible, [aria-hidden="true"]'):
        node.decompose()
    return soup.get_text(" ", strip=True)


def structured_jobs(page):
    soup = BeautifulSoup(page, "html.parser")

    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            kinds = value.get("@type", [])
            if "JobPosting" in (kinds if isinstance(kinds, list) else [kinds]):
                yield value
            else:
                for item in value.values():
                    if isinstance(item, (dict, list)):
                        yield from walk(item)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(script.get_text(), strict=False))
        except (ValueError, TypeError):
            continue


def schema_details(raw, url):
    org = raw.get("hiringOrganization") or {}
    if not isinstance(org, dict):
        org = {}
    places = raw.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]
    locations = []
    for place in places:
        address = place.get("address", {}) if isinstance(place, dict) else {}
        if isinstance(address, str):
            locations.append(address)
        elif isinstance(address, dict):
            locations.append(", ".join(str(address.get(k)) for k in (
                "addressLocality", "addressRegion", "addressCountry"
            ) if address.get(k) and address.get(k) != "null"))
    remote = raw.get("jobLocationType") == "TELECOMMUTE"
    salary = raw.get("baseSalary") or {}
    pay = ""
    currency = ""
    if isinstance(salary, dict):
        currency = salary.get("currency", "")
        value = salary.get("value", {})
        if isinstance(value, dict):
            lo = value.get("minValue", value.get("value"))
            hi = value.get("maxValue", lo)
            if lo is not None:
                unit = str(value.get("unitText", "")).lower()
                unit = {"year": "yr", "hour": "hr", "month": "mo", "week": "wk"}.get(unit, unit)
                symbol = "$" if currency in ("", "USD") else currency + " "
                pay = f"{symbol}{lo}" + (f" - {symbol}{hi}" if hi != lo else "") + (f"/{unit}" if unit else "")
    employment = raw.get("employmentType", "")
    if isinstance(employment, list):
        employment = " ".join(employment)
    return {
        "title": raw.get("title", ""), "company": org.get("name", ""),
        "description": visible_text(html.unescape(raw.get("description", ""))),
        "location": ("Remote; " if remote else "") + "; ".join(locations),
        "date_posted": raw.get("datePosted", ""), "valid_through": raw.get("validThrough", ""),
        "job_type": re.sub(r"\s*-\s*", "-", str(employment).replace("_", " ")), "salary": pay,
        "salary_currency": currency, "url": url, "is_remote": remote,
    }


class BoardClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; PersonalJobSearch/1.0)", "Accept": "text/html,application/json"})

    def get(self, url, **kwargs):
        time.sleep(0.15)
        response = self.session.get(url, timeout=25, **kwargs)
        response.raise_for_status()
        if response.text.strip() in {"Forbidden", "Access Denied"}:
            raise ValueError("Public source denied automated access")
        return response

    def details(self, job):
        """Resolve board links and prefer official ATS first-publication dates."""
        response = self.get(job.get("direct_url") or job["url"])
        direct = response.url
        page = response.text
        text = visible_text(page)
        if re.search(r"(?:job|position|posting).{0,70}(?:no longer available|has been filled|is closed|has been closed|expired)", text, re.I):
            raise ValueError("Listing reports that the position is closed")
        parsed = urlparse(direct)
        if parsed.netloc == "gamejobs.co":
            soup = BeautifulSoup(page, "html.parser")
            apply = next((a for a in soup.select("a[href]") if a.get_text(strip=True).lower() == "apply"
                          and urlparse(urljoin(direct, a["href"])).netloc != parsed.netloc), None)
            if apply:
                job["direct_url"] = urljoin(direct, apply["href"])
                return self.details(job)
        authoritative = False
        raw_job = None
        smart = re.search(r"jobs\.smartrecruiters\.com/([^/]+)/(\d+)", direct)
        greenhouse = re.search(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([^/]+)/jobs/(\d+)", direct)
        if not greenhouse and "epicgames.com" in parsed.netloc:
            jid = re.search(r"(?:jobs/|gh_jid=)(\d+)", direct)
            if jid:
                greenhouse = ("epicgames", jid.group(1))
        if not greenhouse and "riotgames.com" in parsed.netloc:
            jid = re.search(r"(?:job/|gh_jid=)(\d+)", direct)
            if jid:
                greenhouse = ("riotgames", jid.group(1))
        if smart:
            data = self.get(f"https://api.smartrecruiters.com/v1/companies/{smart[1]}/postings/{smart[2]}").json()
            if data.get("active") is False:
                raise ValueError("Employer marks this job inactive")
            sections = data.get("jobAd", {}).get("sections", {})
            desc = " ".join(visible_text(v.get("text", "")) for v in sections.values() if isinstance(v, dict))
            raw_job = {
                "title": data.get("name", ""), "company": data.get("company", {}).get("name", ""),
                "description": desc, "date_posted": data.get("releasedDate", ""),
                "job_type": data.get("typeOfEmployment", {}).get("label", ""),
                "location": data.get("location", {}).get("fullLocation", ""),
            }
            authoritative = True
        elif greenhouse:
            board, jid = (greenhouse[1], greenhouse[2]) if isinstance(greenhouse, re.Match) else greenhouse
            data = self.get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}").json()
            desc = visible_text(html.unescape(data.get("content", "")))
            raw_job = {"title": data.get("title", ""), "company": data.get("company_name", ""),
                       "description": desc, "location": data.get("location", {}).get("name", ""),
                       "date_posted": data.get("first_published", ""),
                       "valid_through": data.get("application_deadline", "") or ""}
            authoritative = True
        else:
            raw_job = next((schema_details(raw, direct) for raw in structured_jobs(page)), None)
            authoritative = bool(raw_job) and not any(urlparse(source["url"]).netloc == parsed.netloc for source in SOURCES)
        if raw_job:
            for key, value in raw_job.items():
                if value not in (None, "", []):
                    job[key] = value
        elif not job.get("description"):
            job["description"] = text[:20000]
        if not job.get("title") or (not raw_job and job["title"].lower() not in text.lower()):
            raise ValueError("Response does not contain the direct job listing")
        if not job.get("salary"):
            pay = re.search(r"(?:base salary|salary|compensation|pay range).{0,70}(\$[\d,.]+\s*(?:-|to|\u2013|\u2014)\s*\$[\d,.]+)(.{0,35})", job.get("description", ""), re.I)
            if pay:
                suffix = "/hr" if re.search(r"hour|/hr", pay[2], re.I) else "/yr" if re.search(r"annual|year|base salary", pay[0], re.I) else ""
                job["salary"] = pay[1] + suffix
                if authoritative:
                    job["compensation_source"] = "employer_confirmed"
        elif authoritative and raw_job and raw_job.get("salary"):
            job["compensation_source"] = "employer_confirmed"
        job["direct_url"] = direct
        job["listing_verification"] = {
            "status": "verified", "checked_on": datetime.now(timezone.utc).date().isoformat(),
            "listing_url": direct, "posting_date_or_recency": job.get("date_posted", ""),
            "date_source": "employer" if authoritative else "board",
            "employment_source": "listing" if raw_job else "board",
        }
        return job


def discover_links(page, source, matches):
    soup = BeautifulSoup(page, "html.parser")
    rows = []
    for a in soup.select("a[href]"):
        title = a.get_text(" ", strip=True)
        href = a["href"]
        if not title or not matches(title, "") or href.startswith(("#", "mailto:", "javascript:")):
            continue
        # Category links are navigation, never job candidates.
        if not re.search(r"/(?:rd/\d+|careers/[^/]+|jobs?/[^/?]+|vacancy/[^/]+|job-vacancies/[^/]+)", href) and not (source.get("adapter") == "gamejobs" and "-at-" in href):
            continue
        job = {"title": title, "url": urljoin(source["url"], href), "ats": source["name"],
               "company": "", "date_posted": "", "location": "", "job_type": "", "salary": ""}
        if source.get("adapter") == "grackle":
            row = a.find_parent(class_="joblisting")
            if row:
                cells = row.find_all("div", recursive=False)
                if cells:
                    company, _, location = cells[0].get_text(" ", strip=True).partition(" - ")
                    job.update(company=company, location=location)
        rows.append(job)
    return list({job["url"]: job for job in rows}.values())


def hitmarker_jobs(client, page, matches):
    key = re.search(r"(?:window\.)?TSSK\s*=\s*['\"]([^'\"]+)", page)
    if not key:
        raise ValueError("Board public search configuration is unavailable")
    jobs = {}
    def titles(value):
        if isinstance(value, list):
            return "; ".join(titles(item) for item in value)
        if isinstance(value, dict):
            return str(value.get("title", ""))
        return str(value or "")
    for term in ("audio", "sound", "music"):
        payload = client.get(
            "https://search.hitmarker.com/collections/hitmarker_jobs_open/documents/search",
            params={"q": term, "query_by": "title", "per_page": 100},
            headers={"X-TYPESENSE-API-KEY": key[1]},
        ).json()
        for hit in payload.get("hits", []):
            data = hit.get("document", {})
            if not matches(data.get("title", ""), data.get("jobDescription", "")):
                continue
            stamp = data.get("postDate")
            locations = data.get("jobLocation") or []
            if isinstance(locations, dict):
                locations = [locations]
            location_labels = []
            for location in locations:
                if not isinstance(location, dict):
                    continue
                parents = location.get("parents") or []
                state = next((item.get("code", item.get("title", "")) for item in parents if item.get("type") == "state"), "")
                country = next((item.get("title", "") for item in parents if item.get("type") == "country"), "")
                location_labels.append(", ".join(x for x in (location.get("title", ""), state, country) if x))
            salary = str(data.get("salaryDisplay", "") or "")
            currency = "CAD" if "CA$" in salary else "AUD" if "A$" in salary else "EUR" if "\u20ac" in salary else "GBP" if "\u00a3" in salary else "USD"
            job = {
                "title": data.get("title", ""), "url": data.get("url", ""),
                "company": titles(data.get("jobCompany")),
                "description": data.get("jobDescription", ""), "ats": "Hitmarker",
                "direct_url": data.get("jobApplicationUrl", ""),
                "job_type": titles(data.get("jobContract")),
                "date_posted": datetime.fromtimestamp(int(stamp), timezone.utc).isoformat() if stamp else "",
                "location": "; ".join(location_labels),
                "salary": salary, "salary_currency": currency,
            }
            if job["url"]:
                jobs[job["url"]] = job
    return list(jobs.values())


def scrape_gaming_sources(matches, max_details=100, cache_path=None):
    client = BoardClient()
    jobs = []
    health = []
    detail_cache = {}
    if cache_path and Path(cache_path).exists():
        try:
            detail_cache = json.loads(Path(cache_path).read_text())
        except (OSError, ValueError):
            pass
    today = datetime.now(timezone.utc).date().isoformat()
    detail_cache = {key: value for key, value in detail_cache.items()
                    if (value.get("listing_verification") or {}).get("checked_on") == today or value.get("failed_on") == today}
    for source in SOURCES:
        report = {"source": source["name"], "url": source["url"], "checked_at": datetime.now(timezone.utc).isoformat(), "raw": 0, "verified": 0, "errors": 0}
        if source.get("resource"):
            report.update(status="resource_only", detail=source["resource"])
            health.append(report)
            continue
        try:
            response = client.get(source.get("api") or source["url"])
            if source.get("api"):
                payload = response.json()
                if isinstance(payload, dict) and payload.get("error"):
                    raise ValueError(str(payload["error"]))
                page = response.text
                records = payload if isinstance(payload, list) else payload.get("jobs", [])
                candidates = []
                for raw in records:
                    if not matches(raw.get("title", ""), "") or not raw.get("jobLink"):
                        continue
                    candidates.append({
                        "title": raw["title"], "company": raw.get("companyName", ""),
                        "url": raw["jobLink"], "ats": source["name"], "job_type": raw.get("jobType", ""),
                        "location": ", ".join(str(raw.get(k)) for k in ("city", "state", "country") if raw.get(k)),
                        "board_activated_on": raw.get("activatedOn", ""),
                        # Feed activation/estimated dates are not employer posting dates.
                        "date_posted": "",
                    })
                candidates.sort(key=lambda job: job.get("board_activated_on", ""), reverse=True)
                report["status"] = "ok"
            else:
                page = response.text
                candidates = hitmarker_jobs(client, page, matches) if source.get("adapter") == "hitmarker" else discover_links(page, source, matches)
                report["status"] = "ok"
            report["raw"] = len(candidates)
            for job in candidates[:max_details]:
                key = job.get("direct_url") or job["url"]
                try:
                    if key not in detail_cache:
                        try:
                            detail_cache[key] = client.details(dict(job))
                        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                            detail_cache[key] = {"failed_on": today, "error": str(exc)[:200]}
                            if cache_path:
                                Path(cache_path).write_text(json.dumps(detail_cache, ensure_ascii=False))
                            raise
                        if cache_path:
                            Path(cache_path).write_text(json.dumps(detail_cache, ensure_ascii=False))
                    if detail_cache[key].get("error"):
                        raise ValueError(detail_cache[key]["error"])
                    verified = dict(job)
                    verified.update({name: value for name, value in detail_cache[key].items()
                                     if value not in (None, "", [])})
                    verified["ats"] = source["name"]
                    if matches(verified.get("title", ""), verified.get("description", "")):
                        jobs.append(verified)
                        report["verified"] += 1
                except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
                    report["errors"] += 1
                    report["last_error"] = str(exc)[:200]
            if report["errors"]:
                report["status"] = "partial" if report["verified"] else "error"
            report["details_limit"] = max_details
            report["truncated"] = len(candidates) > max_details
            if not candidates and not source.get("api") and not re.search(r"no jobs|0 jobs|no results|no active|no items", visible_text(page), re.I):
                report.update(status="no_parseable_listings", detail="No relevant job links exposed in public HTML; source may require JavaScript or a feed.")
        except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
            report.update(status="blocked" if "403" in str(exc) or "Forbidden" in str(exc) else "error", errors=1, detail=str(exc)[:200])
        print(f"Gaming source {report['source']}: {report['status']} / {report['raw']} candidates / {report['verified']} verified / {report['errors']} errors")
        health.append(report)
    if cache_path:
        Path(cache_path).write_text(json.dumps(detail_cache, ensure_ascii=False))
    return jobs, health
