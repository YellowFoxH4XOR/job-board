from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import urlencode

from .filtering import workplace
from .http import Fetcher
from .models import Source, SourceResult, make_job, plain


def timestamp(value) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC).isoformat()
    return str(value) if value else None


async def collect_feed(source: Source, fetch: Fetcher, result: SourceResult) -> None:
    kind = source.kind
    if kind == "remotive":
        data = await fetch.json(source.url)
        result.pages_fetched += 1
        result.coverage_scope = "Remotive public active-job API; 24-hour publication delay; premium inventory excluded"
        for item in data["jobs"]:
            result.jobs.append(make_job(source, item["id"], item["title"], item["company_name"], item["url"],
                location=item.get("candidate_required_location", ""), workplace_type="remote",
                description=plain(item.get("description")), posted_at=item.get("publication_date"),
                employment_type=item.get("job_type"), salary={"text": item["salary"]} if item.get("salary") else None,
                application_url=item["url"]))
    elif kind == "remoteok":
        data = await fetch.json(source.url)
        result.pages_fetched += 1
        result.partial("Public Remote OK feed; full active-catalog coverage and pagination are not guaranteed")
        for item in data:
            if not isinstance(item, dict) or not item.get("id") or not item.get("position"):
                continue  # First object is API attribution/legal metadata, not a job.
            url = item.get("url") or item.get("apply_url")
            if not url:
                continue
            result.jobs.append(make_job(source, item["id"], item["position"], item.get("company"), url,
                location=item.get("location") or "", workplace_type="remote", description=plain(item.get("description")),
                posted_at=item.get("date"), skills=item.get("tags") or [], application_url=item.get("apply_url") or url))
    elif kind == "fourdayweek":
        for page in range(1, source.max_pages + 1):
            data = await fetch.json(f"{source.url}?{urlencode({'page': page, 'limit': 100, 'country': 'India'})}")
            result.pages_fetched += 1
            result.expected_count = data.get("total")
            result.coverage_scope = "4dayweek public India-filtered API; up to 48-hour publication delay"
            for item in data["data"]:
                company = item.get("company") or {}
                locations = item.get("locations") or []
                result.jobs.append(make_job(source, item["id"], item["title"], company.get("name"), item["url"],
                    location=plain([x.get("country") for x in locations]), countries=[x["country"] for x in locations if x.get("country")],
                    workplace_type=workplace(item.get("work_arrangement")), description=plain(item.get("description")),
                    posted_at=item.get("posted_at"), valid_through=item.get("expires_at"),
                    salary={"min": item.get("salary_min"), "max": item.get("salary_max"),
                            "currency": item.get("salary_currency"), "period": item.get("salary_period"), "unit": "cents"},
                    employment_type=item.get("contract_type"), application_url=item["url"],
                    skills=[x["name"] for x in item.get("skills", []) if x.get("name")]))
            if not data.get("has_more"):
                break
        else:
            result.partial("4dayweek page budget reached")
    elif kind == "himalayas":
        result.coverage_scope = "Himalayas public feed; country restrictions preserved"
        cursor = None
        seen_cursors = set()
        seen_jobs = set()
        for _page in range(source.max_pages):
            params = {"limit": 20, **({"cursor": cursor} if cursor else {"offset": 0})}
            data = await fetch.json(f"{source.url}?{urlencode(params)}")
            result.pages_fetched += 1
            items = data.get("jobs", [])
            result.expected_count = data.get("totalCount")
            for item in items:
                countries = item.get("locationRestrictions") or []
                url = item.get("guid") or item.get("applicationLink")
                if not url or url in seen_jobs:
                    continue
                seen_jobs.add(url)
                result.jobs.append(make_job(source, item.get("id") or url, item.get("title"), item.get("companyName"), url,
                    location=plain(countries) or "", countries=countries, workplace_type="remote",
                    description=plain(item.get("description")), posted_at=timestamp(item.get("pubDate")),
                    valid_through=timestamp(item.get("expiryDate")), application_url=item.get("applicationLink")))
            next_cursor = data.get("nextCursor")
            if not next_cursor or not items:
                break
            if next_cursor in seen_cursors:
                result.partial("Himalayas repeated a cursor; stopping to avoid duplicate traversal")
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            result.partial("Himalayas page budget reached")
    elif kind == "workingnomads":
        data = await fetch.json(source.url)
        result.pages_fetched += 1
        result.partial("Working Nomads public exposed feed; premium inventory and full-active coverage unverified")
        for item in data:
            result.jobs.append(make_job(source, item.get("id") or item["url"], item["title"], item.get("company_name"), item["url"],
                location=plain(item.get("location")), workplace_type="remote", description=plain(item.get("description")),
                posted_at=item.get("pub_date"), application_url=item["url"]))
    elif kind == "jobicy":
        await collect_jobicy(source, fetch, result)
    elif kind in {"rss", "teamtailor"}:
        await collect_rss(source, fetch, result)
    else:
        raise ValueError(f"Unknown feed: {kind}")


async def collect_jobicy(source: Source, fetch: Fetcher, result: SourceResult) -> None:
    # Persist known IDs, then status-check older entries instead of treating feed expiry as closure.
    state_path = fetch.cache_dir.parent / "jobicy-history.json"
    history = json.loads(state_path.read_text()) if state_path.exists() else {}
    cursor = None
    for _ in range(source.max_pages):
        query = urlencode({"count": 100, **({"cursor": cursor} if cursor else {})})
        data = await fetch.json(f"{source.url}?{query}")
        result.pages_fetched += 1
        for item in data.get("jobs", []):
            history[str(item["id"])] = item
        cursor = data.get("nextCursor")
        if not cursor:
            break
    else:
        result.partial("Jobicy cursor page budget reached")
    result.partial("Jobicy discovery covers the last 7 days, delayed 3 hours; older unknown IDs cannot be bootstrapped")
    ids = list(history)
    active = set()
    for start in range(0, len(ids), 100):
        data = await fetch.json(f"https://jobicy.com/api/jobs/status?{urlencode({'ids': ','.join(ids[start:start + 100])})}")
        result.pages_fetched += 1
        for record in data.get("jobs", []):
            key = str(record["id"])
            if record.get("status") == "active":
                active.add(key)
            elif record.get("status") == "closed":
                history.pop(key, None)
    state_path.write_text(json.dumps(history, ensure_ascii=False))
    for key in active:
        item = history[key]
        result.jobs.append(make_job(source, key, item["jobTitle"], item.get("companyName"), item["url"],
            location=plain(item.get("jobGeo")), workplace_type="remote", description=plain(item.get("jobDescription")),
            posted_at=item.get("pubDate"), employment_type=plain(item.get("jobType")), application_url=item["url"]))


def xml_value(element: ET.Element, *names: str) -> str:
    for child in element.iter():
        if child.tag.split("}")[-1] in names and child.text:
            return child.text
    return ""


async def collect_rss(source: Source, fetch: Fetcher, result: SourceResult) -> None:
    if source.kind != "teamtailor":
        result.partial("RSS/Atom feed retention may omit older active vacancies")
    for page in range(source.max_pages if source.kind == "teamtailor" else 1):
        url = source.url
        if source.kind == "teamtailor":
            url = f"{source.url.rstrip('/')}/jobs.rss?offset={page * 100}&per_page=100"
        root = ET.fromstring(await fetch.text(url))
        result.pages_fetched += 1
        items = [x for x in root.iter() if x.tag.split("}")[-1] in {"item", "entry"}]
        for item in items:
            title = xml_value(item, "title")
            job_url = xml_value(item, "link")
            if not job_url:
                job_url = next((x.attrib.get("href", "") for x in item.iter() if x.tag.split("}")[-1] == "link"), "")
            if not job_url:
                continue
            description = plain(xml_value(item, "encoded", "description", "content", "summary"))
            company = source.company or xml_value(item, "company", "company_name")
            # Several remote feeds encode company in the title: Company: Role.
            if not company and ": " in title:
                company, title = title.split(": ", 1)
            if not company and " at " in title:
                title, company = title.rsplit(" at ", 1)
            location = plain(xml_value(item, "location", "region", "country", "jobLocation"))
            if not location:
                import re
                match = re.search(r"(?:location|headquarters|region)\s*:\s*([^\n.;]{2,100})", description, re.I)
                location = match[1] if match else ""
            result.jobs.append(make_job(source, xml_value(item, "guid", "id") or job_url, title,
                company or "Not specified", job_url, description=description, location=location,
                workplace_type=workplace("remote" if source.options.get("remote_only") else xml_value(item, "remote_status"), location, description),
                posted_at=xml_value(item, "pubDate", "published", "updated") or None,
                application_url=job_url))
        if len(items) < 100:
            break
    else:
        if source.kind == "teamtailor":
            result.partial("Teamtailor RSS page budget reached")
