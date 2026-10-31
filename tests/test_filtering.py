from datetime import UTC, datetime

import pytest

from job_board.filtering import classify, expired
from job_board.models import Source, make_job


def vacancy(**kwargs):
    defaults = {"location": "India", "description": "Build reliable software systems."}
    defaults.update(kwargs)
    title = defaults.pop("title", "Senior Software Engineer")
    return make_job(Source(id="test", name="Test", kind="generic", url="https://example.com/jobs"),
                    "123", title, "Example", "https://example.com/jobs/123", **defaults)


@pytest.mark.parametrize("location", ["India", "Bengaluru", "Remote, India", "Pune, Maharashtra"])
def test_india_locations_are_kept(location):
    job = vacancy(location=location)
    assert classify(job)[0]
    assert job.india_eligibility == "india"


def test_indiana_is_not_india():
    assert not classify(vacancy(location="Indianapolis, Indiana, United States"))[0]


def test_remote_does_not_imply_worldwide():
    assert not classify(vacancy(location="Remote", workplace_type="remote"))[0]
    assert not classify(vacancy(location="APAC", workplace_type="remote"))[0]


def test_us_location_overrides_generic_remote_benefits():
    job = vacancy(location="Remote, United States", workplace_type="remote",
                  description="Work from anywhere and build software.")
    assert not classify(job)[0]
    assert job.india_eligibility == "excluded"


def test_explicit_worldwide_role_is_kept():
    job = vacancy(location="Remote, Global", workplace_type="remote")
    assert classify(job)[0]
    assert job.india_eligibility == "worldwide"


def test_residency_timezone_excludes_ist_without_excluding_work_overlap():
    job = vacancy(location="Remote, Worldwide", workplace_type="remote",
                  description="Your main location must be between UTC-4 and UTC+4.")
    assert not classify(job)[0]
    job = vacancy(location="Remote, Worldwide", workplace_type="remote",
                  description="Working hours must overlap with EST for at least 20 hours/week.")
    assert classify(job)[0]
    assert job.requirements


def test_hybrid_not_mislabelled_fully_remote_but_india_roles_are_kept():
    job = vacancy(workplace_type="remote", description="This is a hybrid role, 3 days in the office.")
    assert classify(job)[0]
    assert job.workplace_type == "hybrid"


def test_closed_jobs_and_talent_pools_are_not_active():
    assert not classify(vacancy(description="This job is no longer accepting applications."))[0]
    assert not classify(vacancy(title="Engineering Talent Community"))[0]
    assert not classify(vacancy(title="General Applications: Software Engineering"))[0]


def test_nontech_and_industry_noise():
    assert not classify(vacancy(title="Software Recruiter"))[0]
    assert not classify(vacancy(title="Senior Civil Engineer"))[0]
    assert classify(vacancy(title="Enterprise Sales Engineer"))[0]


def test_deadline_uses_end_of_ist_calendar_day():
    assert not expired("2026-10-31", datetime(2026, 10, 31, 12, 0, tzinfo=UTC))
    assert expired("2026-10-31", datetime(2026, 10, 31, 19, 0, tzinfo=UTC))
