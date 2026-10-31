from __future__ import annotations

import json
import re
from collections import deque
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .filtering import CLOSED, workplace
from .http import BlockedError, Fetcher
from .models import Job, Source, SourceResult, make_job, plain

ATS_KINDS = {"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "workday", "personio"}


def detect_ats(url: str, *, name: str, source_id: str) -> Source | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = [p for p in parts.path.split("/") if p]
    tenant = path[0] if path else ""
    kind = None
    options = {}
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io"}:
        tenant = parse_qs(parts.query).get("for", [tenant])[0]
        if tenant not in {"embed", "embed.html"}:
            kind = "greenhouse"
    elif host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        kind = "lever"
        options = {"region": "eu"} if host == "jobs.eu.lever.co" else {}
    elif host == "jobs.ashbyhq.com":
        kind = "ashby"
    elif host in {"careers.smartrecruiters.com", "jobs.smartrecruiters.com"}:
        kind = "smartrecruiters"
    elif host == "apply.workable.com" and tenant != "j":
        kind = "workable"
    elif host.endswith(".myworkdayjobs.com") and path:
        kind = "workday"
        index = 1 if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", path[0]) else 0
        if len(path) <= index:
            return None
        options = {"site": path[index]}
        tenant = host.split(".")[0]
        url = f"https://{host}/{path[index]}"
    elif host.endswith((".jobs.personio.de", ".jobs.personio.com")):
        kind, tenant, url = "personio", host.split(".")[0], f"https://{host}"
    elif host.endswith(".teamtailor.com"):
        kind, tenant, url = "teamtailor", host.split(".")[0], f"https://{host}"
    if not kind or not tenant:
        return None
    return Source(id=source_id, name=name, company=name, kind=kind, tenant=tenant,
                  url=url, options=options, group="employer", timeout_seconds=900)


def find_ats(html: str, url: str, source: Source) -> list[Source]:
    soup = BeautifulSoup(html, "html.parser")
    urls = [url]
    urls += [urljoin(url, tag.get("href") or tag.get("src")) for tag in soup.select("a[href],iframe[src]")]
    # Public embeds often contain the board URL in inline scripts, without a visible link.
    urls += re.findall(r"https://(?:job-boards\.greenhouse\.io|boards\.greenhouse\.io|jobs\.lever\.co|jobs\.ashbyhq\.com|apply\.workable\.com|careers\.smartrecruiters\.com)/[\w.-]+", html)
    found = {}
    for candidate in urls:
        detected = detect_ats(candidate, name=source.company or source.name, source_id=source.id)
        if detected:
            key = f"{detected.kind}:{detected.tenant}:{detected.options.get('site', '')}"
            found[key] = detected
    return list(found.values())


def json_nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from json_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from json_nodes(item)


def jobposting_nodes(soup: BeautifulSoup) -> list[dict]:
    records = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for node in json_nodes(data):
            kinds = node.get("@type", [])
            if "JobPosting" in ([kinds] if isinstance(kinds, str) else kinds):
                records.append(node)
    return records


def as_list(value):
    return value if isinstance(value, list) else [value] if value else []


def from_jobposting(node: dict, source: Source, page_url: str) -> Job | None:
    if not node.get("title"):
        return None
    company = plain(node.get("hiringOrganization")) or source.company or source.name
    places = []
    countries = []
    eligibility = as_list(node.get("applicantLocationRequirements"))
    for loc in as_list(node.get("jobLocation")):
        if isinstance(loc, str):
            places.append(loc)
            continue
        address = loc.get("address") or loc
        places.append(plain([address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")]) or plain(loc.get("name")))
        country = plain(address.get("addressCountry"))
        if country:
            countries.append(country)
    if eligibility:
        countries = [plain(x) for x in eligibility if plain(x)]
        places = countries
    location = plain(places)
    description = plain(node.get("description"))
    if not location:
        label = re.search(r"Remote\s*\((?:anywhere|worldwide|global|India)\)|Home based\s*[-–]\s*Worldwide", description, re.I)
        if label:
            location = label.group()
    identifier = node.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name")
    url = node.get("url") or page_url
    if not isinstance(url, str):
        url = page_url
    salary = node.get("baseSalary")
    return make_job(source, identifier or url, node["title"], company, url,
                    location=location, countries=countries, description=description,
                    workplace_type=workplace(plain(node.get("jobLocationType")), location, description),
                    employment_type=plain(node.get("employmentType")) or None,
                    posted_at=node.get("datePosted"), valid_through=node.get("validThrough"),
                    salary=salary if isinstance(salary, dict) else {"text": plain(salary)} if salary else None,
                    skills=[plain(x) for x in as_list(node.get("skills"))], application_url=url)


def parse_detail(html: str, url: str, source: Source) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    nodes = jobposting_nodes(soup)
    jobs = [job for node in nodes if (job := from_jobposting(node, source, url))]
    if jobs:
        # Some sites (notably Railway) put only a meta-description in JSON-LD.
        for node in soup.select("script,style,nav,footer,form,header"):
            node.decompose()
        main = soup.select_one('[itemprop="description"], .job-description, #job-description, .job_description, article, main') or soup.body or soup
        full_description = main.get_text("\n", strip=True)
        for job in jobs:
            if len(job.description) < 500 and len(full_description) > len(job.description):
                job.description = full_description[:60000]
        if CLOSED.search(full_description[:10000]):
            for job in jobs:
                job.status = "closed"
        return jobs
    title_tag = soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""
    if not title or re.search(r"^(careers|jobs|open (roles|positions)|current openings|page not found|404|search jobs)$|\bjobs\b|\bjob (?:search|openings|vacancies)\b", title, re.I):
        return []
    for node in soup.select("script,style,nav,footer,form,header"):
        node.decompose()
    main = soup.select_one('[itemprop="description"], .job-description, #job-description, .job_description, article, main') or soup.body or soup
    text = main.get_text("\n", strip=True)
    if len(text) < 150 or not re.search(r"responsibilit|requirements|qualifications|about (the |this )?role|what you|job description|experience|apply", text, re.I):
        return []
    company = source.company
    if not company:
        heading = soup.find("h2")
        if heading and heading.get_text(strip=True).lower().startswith("at "):
            company = heading.get_text(" ", strip=True)[3:]
        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if not company and " hiring for " in page_title:
            company = page_title.split(" hiring for ")[0]
        if not company and " job at " in page_title:
            company = page_title.split(" job at ", 1)[1].split(" - ")[0].strip()
    location_match = re.search(r"(?:^|\n)(?:Location|Job location|locations|Work location)\s*[:\n]\s*([^\n]{2,150})", text, re.I)
    location = location_match[1].strip() if location_match else ""
    if not location:
        match = re.search(r"(?:Remote\s*\([^\n)]{2,80}\)|Remote\s*[-–,:]\s*(?:India|Global|Worldwide)|Home based\s*[-–]\s*Worldwide)", text[:1800], re.I)
        location = match[0] if match else ""
    if source.kind == "instahyre" and not location:
        match = re.search(r"\n(?:at [^\n]+\n)?\s*([^\n]{2,100})\s*\d+\s*[-–]\s*\d+\s*Years", text[:1200], re.I)
        location = match[1] if match else ""
    if not company:
        company = "Not specified"
    job = make_job(source, url, title, company, url, location=location, description=text[:60000],
                   workplace_type=workplace(None, location, text), application_url=url)
    if CLOSED.search(text[:8000]):
        job.status = "closed"
    return [job]


def job_links(soup: BeautifulSoup, base: str) -> tuple[list[str], list[str]]:
    details, indexes = [], []
    host = urlsplit(base).hostname or ""
    root = ".".join(host.split(".")[-2:])
    for anchor in soup.select("a[href]"):
        url = urljoin(base, anchor["href"])
        parts = urlsplit(url)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            continue
        if not (parts.hostname == host or parts.hostname.endswith("." + root)):
            continue
        label = anchor.get_text(" ", strip=True)
        path = parts.path.lower()
        if any(x in path for x in ("/login", "/signin", "/register", "/privacy", "/terms", "/blog/", "/post/", "/salary", "/contact")):
            continue
        if "#" in url and parts.path == urlsplit(base).path and not parts.query:
            continue
        if host.endswith("weekday.works"):
            if parts.hostname == "jobs.weekday.works" and path != "/":
                details.append(url)
            elif parts.hostname == "www.weekday.works" and path.startswith("/jobs/") and (
                label.isdigit() or "next" in anchor.get("rel", [])
                or path.startswith(urlsplit(base).path.rstrip("/"))
            ):
                indexes.append(url)
            continue
        is_page = ("next" in anchor.get("rel", []) or label.lower() in {"next", "next »", "next page", "older", "older posts"}
                   or (label.isdigit() and ("page" in parts.query or "/jobs/" in path or "/careers" in path))
                   or "/jobs/browse/" in path)
        if is_page:
            indexes.append(url)
        elif re.search(r"/job-\d|/job/|/jobs/[^/]+|/careers/(?:details/)?[^/]+|/positions/[^/]+|/opportunities/[^/]+|/p/[a-z0-9]{6,}", path):
            if path.rstrip("/").split("/")[-1] not in {"all", "all-jobs", "engineering", "benefits", "hiring-process", "company-culture", "about", "jobs", "search", "remote", "locations"}:
                details.append(url)
        elif host == "jobs.weekday.works" and path != "/":
            details.append(url)
        elif parts.hostname == "jobs.weekday.works" and path != "/":
            details.append(url)
    return list(dict.fromkeys(details)), list(dict.fromkeys(indexes))


async def collect_web(source: Source, fetch: Fetcher, result: SourceResult) -> None:
    from .ats import collect_ats
    from .feeds import collect_feed

    seeds = source.options.get("seed_urls") or [source.url]
    queue = deque(seeds)
    visited = set()
    details_seen = set()
    details_done = 0
    explicit_empty = False
    while queue and result.pages_fetched < source.max_pages:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        try:
            response = await fetch.request(url, html=True)
        except Exception as exc:
            if len(seeds) > 1:
                result.partial(f"Listing page failed: {url}: {type(exc).__name__}: {str(exc)[:180]}")
                continue
            raise
        text = response.text
        url = str(response.url)
        result.pages_fetched += 1
        soup = BeautifulSoup(text, "html.parser")
        if source.group == "employer":
            matches = find_ats(text, url, source)
            if matches:
                for detected in matches:
                    if detected.kind == "teamtailor":
                        await collect_feed(detected, fetch, result)
                    else:
                        await collect_ats(detected, fetch, result)
                return
        for node in jobposting_nodes(soup):
            job = from_jobposting(node, source, url)
            if job:
                result.jobs.append(job)
                details_seen.add(job.url)
        visible = soup.get_text(" ", strip=True)
        if re.search(r"(?:we (?:currently )?(?:have|do not have)|there are|there aren't|no) (?:any |currently |no )?(?:open roles|job openings|open positions)(?: right now| at (?:the )?moment)?", visible, re.I):
            explicit_empty = bool(re.search(r"no (?:open roles|job openings|open positions)|do not have any job openings|currently no open roles", visible, re.I))
        links, indexes = job_links(soup, url)
        for next_url in indexes:
            if next_url not in visited:
                queue.append(next_url)
        if not indexes and re.search(r"Next\s*(?:»|›)|Showing\s+1\s*[-–]\s*\d+\s+(?:out )?of\s+\d{3,}", visible):
            result.partial("Listing has pagination controls but no crawlable next-page URL was exposed")
        for detail_url in links:
            if detail_url in details_seen:
                continue
            details_seen.add(detail_url)
            if details_done >= source.max_details:
                result.partial(f"Detail budget ({source.max_details}) reached; more listings may exist")
                break
            details_done += 1
            try:
                detail_response = await fetch.request(detail_url, html=True)
                jobs = parse_detail(detail_response.text, str(detail_response.url), source)
                if jobs:
                    result.jobs.extend(jobs)
                else:
                    # A directory link can lead to another index rather than a vacancy.
                    nested = BeautifulSoup(detail_response.text, "html.parser")
                    children, more = job_links(nested, str(detail_response.url))
                    if (children or more) and detail_url not in visited:
                        queue.append(detail_url)
            except BlockedError as exc:
                result.partial(f"Detail access blocked: {exc}")
            except Exception as exc:
                result.partial(f"Detail failed: {type(exc).__name__}: {str(exc)[:180]}")
        if details_done >= source.max_details:
            break
    if queue:
        result.partial("Crawl ended with unvisited listing pages")
    if not result.jobs and not explicit_empty:
        result.status = "unsupported"
        result.complete = False
        result.errors.append("No job records or explicit no-openings message exposed; dynamic rendering or a dedicated adapter is required")
    elif result.jobs:
        result.coverage_scope = "Public HTML/JSON-LD vacancies reachable from the configured career/listing pages"
        # Discovery links are not an authoritative catalog count.
        result.partial("Public HTML discovery does not establish exhaustive active-catalog coverage")
