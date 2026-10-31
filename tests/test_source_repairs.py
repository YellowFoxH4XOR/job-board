import html
import json

import pytest
from bs4 import BeautifulSoup

from job_board.embedded import embedded_links, homerun_vacancies
from job_board.feeds import collect_feed
from job_board.filtering import classify, workplace
from job_board.models import Source, SourceResult
from job_board.web import has_no_openings, job_links, listing_jobs, parse_detail


@pytest.mark.parametrize(("url", "link"), [
    ("https://nodesk.co/remote-jobs/", "/remote-jobs/askable-software-engineer-ai-coding-tasks/"),
    ("https://tryremote.com/", "/remote-jobs/450926-senior-ai-engineer-orbis"),
    ("https://datajobs.com/Data-Science-Jobs", "/Example/Senior-Data-Engineer-Job~641212"),
    ("https://careers.kula.ai/usemultiplier", "/usemultiplier/28550-senior-software-engineer-ai-platform"),
    ("https://jobs.ashbyhq.com/toggl", "/toggl/a4f3e1f5-753b-4af9-b068-86a09a164cd2"),
    ("https://www.hirist.tech/c/backend-development-jobs", "/j/company-backend-developer-1234567"),
])
def test_observed_job_link_formats_are_discovered(url, link):
    from urllib.parse import urljoin
    soup = BeautifulSoup(f'<a href="{link}">Software Engineer</a>', "html.parser")
    assert urljoin(url, link) in job_links(soup, url)[0]


def test_category_navigation_does_not_consume_detail_budget():
    soup = BeautifulSoup('<a href="/jobs/jobs-in-pune">Pune jobs</a><a href="/job/pune/software-engineer-123">Software Engineer</a>', "html.parser")
    details, _ = job_links(soup, "https://apna.co/jobs/software-engineering-jobs")
    assert details == ["https://apna.co/job/pune/software-engineer-123"]


def test_homerun_vue_data_is_not_mistaken_for_empty_placeholder():
    data = {"content": {"vacancies": [{"id": 1, "title": "Staff Product Engineer", "url": "https://careers.ghost.org/staff-product-engineer/en", "location_id": 1, "department_id": 2}]},
            "i18n": {"noVacancies": "There are currently no job openings"}}
    soup = BeautifulSoup(f'<job-list v-bind="{html.escape(json.dumps(data), quote=True)}"></job-list>', "html.parser")
    assert len(homerun_vacancies(soup)) == 1
    assert not has_no_openings(soup)
    assert embedded_links(soup) == ["https://careers.ghost.org/staff-product-engineer/en"]


def test_true_empty_career_page_and_curly_apostrophe_are_recognized():
    assert has_no_openings(BeautifulSoup("<main>Sorry, we don’t have any job openings right now.</main>", "html.parser"))
    assert not has_no_openings(BeautifulSoup("<main>Don't see the right position? Join our mailing list.</main>", "html.parser"))


def test_deel_flight_records_preserve_countries_and_job_identity():
    row = {"ashby_id": "job-123", "title": "Senior Backend Engineer", "is_listed": True,
           "external_link": "https://jobs.deel.com/deel/job-details/job-123/application",
           "all_locations": ["India", "Poland"], "full_job_description": "Build reliable APIs.", "employment_type": "Full-time"}
    payload = "4:" + json.dumps({"jobs": [row]}) + "\n"
    soup = BeautifulSoup(f'<script>self.__next_f.push({json.dumps([1, payload])})</script>', "html.parser")
    source = Source(id="deel", name="Deel", company="Deel", kind="generic", url="https://www.deel.com/careers/")
    jobs = listing_jobs(soup, source.url, source)
    assert len(jobs) == 1
    assert jobs[0].countries == ["India", "Poland"]
    assert jobs[0].url.endswith("/overview")
    assert jobs[0].application_url.endswith("/application")
    assert classify(jobs[0])[0]


def test_multiple_jsonld_jobs_map_to_distinct_detail_urls():
    first = {"@type": "JobPosting", "title": "Backend Engineer", "description": "Build APIs", "jobLocation": {"address": "Bangalore, India"}}
    second = {**first, "title": "Frontend Engineer"}
    content = f'<script type="application/ld+json">{json.dumps([first, second])}</script><a href="/backend-example/123">Backend Engineer</a><a href="/frontend-example/456">Frontend Engineer</a>'
    source = Source(id="web3", name="Web3", kind="generic", url="https://web3.career/web3-jobs-india")
    jobs = listing_jobs(BeautifulSoup(content, "html.parser"), source.url, source)
    assert len(jobs) == 2
    assert jobs[0].id != jobs[1].id
    assert all(j.url != source.url for j in jobs)
    assert all(j.location == "Bangalore, India" for j in jobs)


def test_broken_jsonld_can_fall_back_to_document_title():
    source = Source(id="js", name="JavaScript Jobs", kind="generic", url="https://javascript.jobs")
    content = '<title>Frontend Software Engineer</title><script type="application/ld+json">{"description":"broken"quotes"}</script><main><p>Job description</p><p>Build reliable browser applications using JavaScript and work with product teams. Requirements include production software engineering experience.</p><h3>Job Location</h3><p>Pune, India - Remote</p></main>'
    job = parse_detail(content, source.url + "/job/frontend-engineer", source)[0]
    assert job.title == "Frontend Software Engineer"
    assert "Pune" in job.location


def test_hybrid_cloud_is_not_a_hybrid_work_arrangement():
    assert workplace("remote", "India", "Build security software for hybrid cloud environments.") == "remote"
    assert workplace("remote", "India", "This is a hybrid role, 3 days in the office.") == "hybrid"


def test_remote_ats_region_restriction_overrides_global_company_text():
    source = Source(id="remote", name="Remote", company="Remote", kind="generic", url="https://remote.com/openings")
    html = '<main><h1>Software Engineer</h1><p>We hire globally and build reliable software for distributed customers around the world. Requirements: production Python experience.</p><dl><dt>Region</dt><dd>EMEA, NORAM</dd></dl></main>'
    job = parse_detail(html, "https://apply.remote.com/jobs/123", source)[0]
    assert job.location == "EMEA, NORAM"
    assert not classify(job)[0]


def test_nonsemantic_navigation_cannot_hide_remote_role_metadata():
    source = Source(id="cal", name="Cal.com", company="Cal.com", kind="generic", url="https://cal.com/jobs")
    html = '<div>' + ('Navigation links and products ' * 60) + '</div><div><h1>Senior Full-Stack Engineer · Remote</h1><p>We build scheduling software. Responsibilities include owning backend APIs and collaborating with engineers. Fully remote and async, work from anywhere on your own schedule.</p></div>'
    job = parse_detail(html, source.url + "/engineer", source)[0]
    assert job.description.startswith("Senior Full-Stack Engineer")
    assert classify(job)[0]


def test_unicode_residence_timezone_excludes_india_despite_anywhere_benefit():
    source = Source(id="ghost", name="Ghost", company="Ghost", kind="generic", url="https://careers.ghost.org")
    html = '<main><h1>Staff Product Engineer</h1><p>Full-time · Remote · UTC−1 - UTC+3</p><p>This role is open to candidates in time zones between UTC−1 and UTC+3. Responsibilities include building reliable software.</p><p>Work from anywhere</p><p>Everything we do is online.</p></main>'
    job = parse_detail(html, source.url + "/engineer/en", source)[0]
    assert not classify(job)[0]
    assert job.india_eligibility == "excluded"


def test_html_application_deadline_is_preserved():
    source = Source(id="datajobs", name="DataJobs", kind="generic", url="https://datajobs.com")
    html = '<h1>Data Engineer</h1><h2>Example</h2><p>Job description and responsibilities: build reliable data pipelines and scalable analytical systems with the engineering team. Application Deadline: September 11, 2025 at 11:59 PM</p><h3>Job Location</h3><p>India</p>'
    job = parse_detail(html, source.url + "/Example/Data-Engineer-Job~1", source)[0]
    assert job.valid_through == "2025-09-11"
    assert not classify(job)[0]


async def test_himalayas_follows_cursor_even_when_a_page_is_short():
    class Fetch:
        def __init__(self):
            self.urls = []

        async def json(self, url):
            self.urls.append(url)
            index = len(self.urls)
            return {"totalCount": 2, "nextCursor": "next-page" if index == 1 else None,
                    "jobs": [{"guid": f"https://example.com/job/{index}", "title": "Software Engineer",
                              "companyName": "Example", "locationRestrictions": ["India"]}]}

    source = Source(id="himalayas", name="Himalayas", kind="himalayas", url="https://himalayas.app/jobs/api")
    fetch = Fetch()
    result = SourceResult(source=source)
    await collect_feed(source, fetch, result)
    assert len(result.jobs) == 2
    assert "cursor=next-page" in fetch.urls[1]
    assert "offset=" not in fetch.urls[1]


def test_same_title_at_two_companies_keeps_separate_job_urls():
    source = Source(id="board", name="Board", kind="generic", url="https://example.com/jobs")
    data = [{"@type": "JobPosting", "title": "Software Engineer", "hiringOrganization": {"name": company}, "description": "Build APIs."} for company in ["One", "Two"]]
    content = f'<script type="application/ld+json">{json.dumps(data)}</script><table><tr><td>One</td><td><a href="/job/1">Software Engineer</a></td></tr><tr><td>Two</td><td><a href="/job/2">Software Engineer</a></td></tr></table>'
    jobs = listing_jobs(BeautifulSoup(content, "html.parser"), source.url, source)
    assert {job.url for job in jobs} == {"https://example.com/job/1", "https://example.com/job/2"}
