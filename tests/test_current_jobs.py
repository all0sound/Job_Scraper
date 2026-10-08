import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import scrape_jobs as scraper
from gaming_sources import BoardClient, schema_details, structured_jobs, visible_text


NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def job(index=1, **changes):
    value = {"title": "Sound Designer", "company": f"Studio {index}",
             "url": f"https://example.com/jobs/{index}", "location": "Los Angeles, CA",
             "date_posted": "2026-10-07", "salary": "$80/hr", "job_type": "Full-time",
             "listing_verification": {"status": "verified", "checked_on": "2026-10-08"}}
    value.update(changes)
    return value


class CurrentJobsTests(unittest.TestCase):
    def test_posting_date_wins_over_recent_discovery(self):
        self.assertFalse(scraper._fresh_current_job(job(date_posted="2026-09-16", first_seen="2026-10-08"), now=NOW))
        self.assertTrue(scraper._fresh_current_job(job(date_posted="2026-09-17"), now=NOW))

    def test_missing_future_expired_and_unverified_dates_excluded(self):
        for candidate in [job(date_posted=""), job(date_posted="2026-10-09"),
                          job(valid_through="2026-10-07"),
                          job(listing_verification={"status": "unavailable"}),
                          job(listing_verification={"status": "verified", "checked_on": "2026-10-05"})]:
            self.assertFalse(scraper._fresh_current_job(candidate, now=NOW))

    def test_relative_dates_are_frozen(self):
        candidate = job(date_posted="2 days ago")
        self.assertTrue(scraper._fresh_current_job(candidate, now=NOW))
        self.assertTrue(candidate["date_posted"].startswith("2026-10-06"))

    def test_no_relaxation_when_five_meet_base_pay(self):
        chosen, metadata = scraper._select_current_jobs([job(i) for i in range(5)], now=NOW)
        self.assertEqual(len(chosen), 5)
        self.assertEqual(metadata["base_thresholds"], metadata["effective_thresholds"])
        self.assertFalse(metadata["undisclosed_pay_fallback"])

    def test_relaxation_stops_at_first_sufficient_threshold(self):
        candidates = [job(i, salary="$80/hr") for i in range(3)] + [job(i + 3, salary="$25/hr") for i in range(2)]
        chosen, metadata = scraper._select_current_jobs(candidates, now=NOW)
        self.assertEqual(len(chosen), 5)
        self.assertEqual(metadata["effective_thresholds"]["nearby_min_hourly"], 25)

    def test_duplicates_do_not_satisfy_five(self):
        candidates = [job(i) for i in range(4)] + [job(0, url="https://other.example/jobs/0")]
        chosen, metadata = scraper._select_current_jobs(candidates, now=NOW)
        self.assertEqual(len(chosen), 4)
        self.assertFalse(metadata["target_met"])

    def test_final_fallback_still_enforces_distance_and_schedule(self):
        candidates = [job(i, salary="") for i in range(5)]
        candidates += [job(10, salary="", job_type="Part-time", location="New York, NY"),
                       job(11, salary="", job_type="", description="")]
        chosen, metadata = scraper._select_current_jobs(candidates, now=NOW)
        self.assertEqual(len(chosen), 5)
        self.assertTrue(metadata["undisclosed_pay_fallback"])
        self.assertTrue(all(j["employment_category"] == "full_time" for j in chosen))

    def test_part_time_hourly_pay_is_not_annualized(self):
        candidate = job(job_type="Part-time", salary="$40/hr")
        self.assertTrue(scraper._passes_compensation_policy(candidate))
        self.assertNotIn("annualized_full_time_max", candidate["compensation"])

    def test_far_remote_part_time_cannot_enter_pay_fallback(self):
        candidate = job(job_type="Part-time", salary="", location="Remote; Tbilisi, Georgia")
        self.assertFalse(scraper._passes_compensation_policy(candidate, thresholds={"full_time_min_hourly": 0, "nearby_min_hourly": 0}, allow_undisclosed=True))

    def test_generic_employee_benefits_do_not_establish_hours(self):
        self.assertEqual(scraper._employment_classification(job(job_type="Freelance", description="Full-time employees receive insurance.")), "unknown")

    def test_tangential_music_business_role_is_not_technical(self):
        self.assertFalse(scraper._matches_current_config(job(title="Senior Content Acquisition Manager, Music Labels", description="You will work with music production teams.")))

    def test_foreign_pay_does_not_become_usd(self):
        candidate = job(salary="$120000/yr", salary_currency="CAD")
        self.assertFalse(scraper._passes_compensation_policy(candidate))
        self.assertNotIn("annual_max", candidate["compensation"])

    def test_salary_text_currency_overrides_board_default(self):
        compensation = scraper._normalize_compensation(job(salary="14200 - 24400 PLN per month", salary_currency="USD"))
        self.assertEqual(compensation["currency"], "PLN")
        self.assertNotIn("annual_max", compensation)

    def test_schema_foreign_salary_is_labeled(self):
        details = schema_details({"baseSalary": {"currency": "JPY", "value": {"value": 4500000, "unitText": "YEAR"}}}, "https://example.com/job")
        self.assertEqual(details["salary"], "JPY 4500000/yr")

    def test_schema_description_can_contain_unescaped_newlines(self):
        page = '<script type="application/ld+json">{"@type":"JobPosting","title":"Sound Designer","description":"First line\nSecond line"}</script>'
        self.assertEqual(next(structured_jobs(page))["title"], "Sound Designer")

    def test_structured_schedule_replaces_aggregator_schedule(self):
        previous = job(job_type="Full-Time")
        current = job(job_type="CONTRACTOR", description="Create sound effects on your own schedule.",
                      listing_verification={"status": "verified", "checked_on": "2026-10-08", "employment_source": "listing"})
        scraper._merge_duplicate_job(previous, current)
        self.assertEqual(scraper._employment_classification(previous), "unknown")

    def test_hidden_closed_banner_is_not_expiration(self):
        page = '<div class="w-condition-invisible">This position has been closed.</div><h1>Sound Designer</h1>'
        self.assertNotIn("closed", visible_text(page))

    def test_non_ascii_url_failure_does_not_abort_refresh(self):
        candidate = job(listing_verification={})
        with patch.object(scraper, "urlopen", side_effect=UnicodeError("URL encoding")), patch.object(scraper.time, "sleep"):
            self.assertFalse(scraper._verify_listing_page(candidate))
        self.assertEqual(candidate["listing_verification"]["status"], "unavailable")

    def test_employer_publication_date_overrides_board_added_date(self):
        client = BoardClient()
        response = unittest.mock.Mock(url="https://job-boards.greenhouse.io/studio/jobs/123", text="<h1>Sound Designer</h1>")
        api = unittest.mock.Mock()
        api.json.return_value = {"title": "Sound Designer", "first_published": "2026-06-01", "updated_at": "2026-10-08", "content": "Full-time music technology"}
        with patch.object(client, "get", side_effect=[response, api]):
            candidate = client.details(job(date_posted="2026-10-08"))
        self.assertEqual(candidate["date_posted"], "2026-06-01")
        self.assertFalse(scraper._fresh_current_job(candidate, now=NOW))


if __name__ == "__main__":
    unittest.main()
