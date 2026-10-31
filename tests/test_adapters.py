import json

import pytest
from bs4 import BeautifulSoup

from job_board.ats import collect_ats
from job_board.models import Source, SourceResult
from job_board.web import detect_ats, job_links, jobposting_nodes, parse_detail


class StubFetcher:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.urls = []

    async def json(self, url, **kwargs):
        self.urls.append(url)
        return next(self.replies)


async def test_lever_paginates_and_preserves_every_description_section():
    source = Source(id="lever", name="Employer", company="Employer", kind="lever", tenant="employer", url="https://jobs.lever.co/employer")
    item = {"id": "1", "text": "Software Engineer", "hostedUrl": "https://jobs.lever.co/employer/1",
            "categories": {"location": "India"}, "descriptionPlain": "Overview",
            "lists": [{"text": "Requirements", "content": "<li>Python</li>"}], "additionalPlain": "Travel required"}
    fetch = StubFetcher([[{**item, "id": str(i)} for i in range(100)], [{**item, "id": "101"}]])
    result = SourceResult(source=source)
    await collect_ats(source, fetch, result)
    assert len(result.jobs) == 101
    assert "skip=100" in fetch.urls[-1]
    assert "Python" in result.jobs[0].description
    assert "Travel required" in result.jobs[0].description
    assert result.complete


async def test_ashby_uses_workplace_type_and_excludes_unlisted():
    source = Source(id="ashby", name="Employer", kind="ashby", tenant="employer", url="https://jobs.ashbyhq.com/employer")
    item = {"title": "Software Engineer", "jobUrl": "https://jobs.ashbyhq.com/employer/1",
            "location": "India", "isRemote": True, "workplaceType": "Hybrid", "isListed": True}
    fetch = StubFetcher([{"jobs": [item, {**item, "isListed": False}]}])
    result = SourceResult(source=source)
    await collect_ats(source, fetch, result)
    assert len(result.jobs) == 1
    assert result.jobs[0].workplace_type == "hybrid"


def test_jsonld_graph_and_applicant_countries():
    source = Source(id="web", name="Example", kind="generic", url="https://example.com/jobs")
    data = {"@graph": [{"@type": "Organization", "name": "Ignored"}, {
        "@type": "JobPosting", "title": "Software Engineer", "description": "<p>Build APIs</p>",
        "hiringOrganization": {"name": "Example"}, "jobLocationType": "TELECOMMUTE",
        "jobLocation": {"address": {"addressCountry": "US"}},
        "applicantLocationRequirements": [{"@type": "Country", "name": "India"}],
    }]}
    html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
    assert len(jobposting_nodes(BeautifulSoup(html, "html.parser"))) == 1
    job = parse_detail(html, "https://example.com/jobs/1", source)[0]
    assert job.countries == ["India"]
    assert job.workplace_type == "remote"
    assert job.company == "Example"


@pytest.mark.parametrize(("url", "kind", "tenant"), [
    ("https://job-boards.greenhouse.io/automatticcareers/jobs/123", "greenhouse", "automatticcareers"),
    ("https://boards.greenhouse.io/embed/job_board?for=example", "greenhouse", "example"),
    ("https://jobs.eu.lever.co/example/123", "lever", "example"),
    ("https://jobs.ashbyhq.com/todoist-inc", "ashby", "todoist-inc"),
    ("https://crowdstrike.wd5.myworkdayjobs.com/en-US/crowdstrikecareers", "workday", "crowdstrike"),
])
def test_real_ats_url_shapes(url, kind, tenant):
    source = detect_ats(url, name="Example", source_id="example")
    assert source.kind == kind
    assert source.tenant == tenant


def test_workable_job_shortcode_is_not_mistaken_for_employer_tenant():
    assert detect_ats("https://apply.workable.com/j/123/", name="Example", source_id="example") is None


def test_railway_metadata_only_jsonld_gets_visible_description_and_anywhere_location():
    source = Source(id="railway", name="Railway", company="Railway", kind="generic", url="https://railway.com/careers")
    data = {"@type": "JobPosting", "title": "Full-Stack Engineer",
            "description": "Apply at Railway. Remote (anywhere), full-time.",
            "jobLocationType": "TELECOMMUTE"}
    html = f'<script type="application/ld+json">{json.dumps(data)}</script><main><h1>Full-Stack Engineer</h1><p>Build reliable APIs and interfaces using TypeScript. Own infrastructure and join a global team. Requirements: production experience and strong communication.</p></main>'
    job = parse_detail(html, "https://railway.com/careers/full-stack", source)[0]
    assert job.location == "Remote (anywhere)"
    assert "TypeScript" in job.description


def test_weekday_navigation_does_not_consume_job_detail_limit():
    soup = BeautifulSoup('<nav><a href="/jobs/remote-frontend-developer-jobs">Frontend jobs</a></nav><a href="https://jobs.weekday.works/example-backend-engineer">Backend Engineer</a><a href="/jobs/remote-backend-developer-jobs-2">2</a>', "html.parser")
    details, indexes = job_links(soup, "https://www.weekday.works/jobs/remote-backend-developer-jobs")
    assert details == ["https://jobs.weekday.works/example-backend-engineer"]
    assert indexes == ["https://www.weekday.works/jobs/remote-backend-developer-jobs-2"]


def test_search_landing_page_cannot_become_a_fake_vacancy():
    source = Source(id="weekday", name="Weekday", kind="weekday", url="https://www.weekday.works/jobs")
    html = '<main><h1>620+ Remote Product Manager Jobs in India</h1><p>Browse job descriptions, requirements and responsibilities for hundreds of exciting roles. Location: Mumbai, Maharashtra, India.</p></main>'
    assert parse_detail(html, source.url, source) == []
