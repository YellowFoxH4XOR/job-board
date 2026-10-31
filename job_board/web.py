from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .embedded import embedded_links, homerun_vacancies, nodes, payloads
from .filtering import CLOSED, INDIA, NON_TECH, TECH, WORLDWIDE, workplace
from .http import BlockedError, Fetcher
from .models import Job, Source, SourceResult, canonical_url, make_job, plain

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


def jobposting_nodes(soup: BeautifulSoup, base_url: str = "") -> list[dict]:
    records = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text(), strict=False)
        except (ValueError, TypeError):
            continue
        for node in json_nodes(data):
            kinds = node.get("@type", [])
            if "JobPosting" in ([kinds] if isinstance(kinds, str) else kinds or []):
                record = node.copy()
                if not record.get("url") and base_url:
                    # Several boards put multiple JSON-LD jobs on a listing page without URLs.
                    title = plain(record.get("title")).casefold()
                    matches = [a for a in soup.select("a[href]")
                               if title and plain(a.get_text(" ", strip=True)).casefold() == title]
                    company = plain(record.get("hiringOrganization")).casefold()
                    specific = [a for a in matches if company and company in plain(
                        (a.find_parent(["tr", "article", "li"]) or a.parent).get_text(" ", strip=True)
                    ).casefold()]
                    candidates = specific or matches
                    if len({a["href"] for a in candidates}) == 1:
                        record["url"] = urljoin(base_url, candidates[0]["href"])
                records.append(record)
    if len(records) > 1:
        for record in records:
            if not record.get("url"):
                record["_unmapped_listing"] = True
    return records


def as_list(value):
    return value if isinstance(value, list) else [value] if value else []


def from_jobposting(node: dict, source: Source, page_url: str) -> Job | None:
    if not node.get("title") or node.get("_unmapped_listing"):
        return None
    company = plain(node.get("hiringOrganization")) or source.company or source.name
    places = []
    countries = []
    eligibility = as_list(node.get("applicantLocationRequirements"))
    for loc in as_list(node.get("jobLocation")):
        if isinstance(loc, str):
            places.append(loc)
            continue
        if not isinstance(loc, dict):
            continue
        address = loc.get("address") or loc
        if isinstance(address, str):
            places.append(address)
            continue
        if not isinstance(address, dict):
            continue
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
    postings = jobposting_nodes(soup, url)
    jobs = [job for node in postings if (job := from_jobposting(node, source, url))]
    if jobs:
        # Some sites (notably Railway) put only a meta-description in JSON-LD.
        for node in soup.select("script,style,nav,footer,form,header"):
            node.decompose()
        main = soup.select_one('[itemprop="description"], .job-description, #job-description, .job_description, article, main') or soup.body or soup
        full_description = main.get_text("\n", strip=True)
        for job in jobs:
            if len(job.description) < 500 and len(full_description) > len(job.description):
                job.description = full_description[:60000]
        if len(jobs) == 1 and CLOSED.search(full_description[:10000]):
            for job in jobs:
                job.status = "closed"
        return jobs
    title_tag = soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""
    if not title:
        meta = soup.select_one('meta[property="og:title"]')
        title = meta.get("content", "") if meta else soup.title.get_text(" ", strip=True) if soup.title else ""
        title = re.split(r"\s+(?:@|\|)\s+", title, maxsplit=1)[0].strip()
    if not title or re.search(r"^(careers|jobs|open (roles|positions)|current openings|page not found|404|search jobs)$|\bjobs\b|\bjob (?:search|openings|vacancies)\b", title, re.I):
        return []
    for node in soup.select("script,style,nav,footer,form,header"):
        node.decompose()
    main = soup.select_one('[itemprop="description"], .job-description, #job-description, .job_description, article, main') or soup.body or soup
    text = main.get_text("\n", strip=True)
    if title_tag:
        heading = title_tag.get_text("\n", strip=True)
        start = text.find(heading)
        if start > 0:
            text = text[start:]
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
        if not company and urlsplit(url).hostname == "datajobs.com":
            company = soup.find("h2").get_text(" ", strip=True) if soup.find("h2") else None
        if not company and urlsplit(url).hostname == "javascript.jobs":
            company_link = soup.select_one('.job-details-title a, a[href*="/company/"]:not([href*="/create/"])')
            company = company_link.get_text(" ", strip=True) if company_link else None
        if not company:
            company_match = re.search(r"(?:^|\n)at\s+([^\n]{2,100})(?:\n|$)", text[:1800], re.I)
            company = company_match[1].strip() if company_match else None
    location_match = re.search(r"(?:^|\n)(?:Location|Job location|locations|Work location|Region)\s*[:\n]\s*([^\n]{2,150})", text, re.I)
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
    deadline = re.search(r"(?:Application Deadline|Closing Date)\s*[:\n]\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})", text, re.I)
    if deadline:
        for date_format in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
            try:
                job.valid_through = datetime.strptime(deadline[1], date_format).date().isoformat()
                break
            except ValueError:
                continue
    if CLOSED.search(text[:8000]):
        job.status = "closed"
    return [job]


def has_no_openings(soup: BeautifulSoup) -> bool:
    jobs = homerun_vacancies(soup)
    if jobs is not None:
        return len(jobs) == 0
    visible = plain(soup.get_text(" ", strip=True)).replace("’", "'")
    return bool(re.search(
        r"\bno (?:current(?:ly)? |open |available )?(?:job openings|openings|positions|roles|vacancies)\b|"
        r"\b(?:don't|do not|doesn't|does not) have (?:any )?(?:job openings|open roles|open positions)\b|"
        r"\bnot (?:currently )?hiring\b|\bOpen Positions\s*\(\s*0\s*\)", visible, re.I,
    ))


def listing_jobs(soup: BeautifulSoup, base_url: str, source: Source) -> list[Job]:
    jobs = [j for n in jobposting_nodes(soup, base_url) if (j := from_jobposting(n, source, base_url))]
    # Deel embeds its public job catalog in React Flight records; do not execute the scripts.
    if "deel.com" in (urlsplit(base_url).hostname or ""):
        for payload in payloads(soup):
            for record in nodes(payload):
                if not (record.get("ashby_id") and record.get("title") and record.get("is_listed") is True):
                    continue
                application = record.get("external_link")
                if not application:
                    continue
                job_url = re.sub(r"/application/?$", "/overview", application)
                locations = record.get("all_locations") or [record.get("location_name")]
                jobs.append(make_job(source, record["ashby_id"], record["title"], source.company, job_url,
                    location=plain(locations), countries=[c for c in locations if c],
                    description=plain(record.get("full_job_description")),
                    workplace_type=workplace(record.get("workplace_type"), plain(locations)),
                    posted_at=record.get("ashby_published_date"), updated_at=record.get("ashby_updated_at"),
                    valid_through=record.get("application_deadline"), employment_type=record.get("employment_type"),
                    application_url=application,
                    salary={"text": record["compensation_tier_summary"]} if record.get("compensation_tier_summary") else None))
    return jobs


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
        is_page = ("next" in anchor.get("rel", []) or bool(re.fullmatch(r"(?:next(?: page)?|older(?: posts)?)[\s»›→]*", label, re.I))
                   or (label.isdigit() and ("page" in parts.query or "/jobs/" in path or "/careers" in path))
                   or "/jobs/browse/" in path)
        if is_page:
            indexes.append(url)
            continue
        # Source-specific URL shapes avoid spending detail requests on nav/category directories.
        patterns = {
            "nodesk.co": r"^/remote-jobs/[^/]{24,}/?$",
            "tryremote.com": r"^/remote-jobs/\d+-",
            "remote.io": r"^/remote-jobs/[^/]+/[^/]+-\d+/?$",
            "datajobs.com": r"/[^/]+/[^/]+Job~\d+$",
            "careers.kula.ai": r"^/[^/]+/\d+-",
            "jobs.ashbyhq.com": r"^/[^/]+/[0-9a-f-]{36}/?$",
            "apna.co": r"^/job/",
            "cutshort.io": r"^/job/",
            "wellfound.com": r"^/jobs/\d+",
            "builtin.com": r"^/job/",
            "internshala.com": r"^/(?:job|internship)/detail/",
            "peerlist.io": r"/careers/[^/]+/job",
            "javascript.jobs": r"^/job/",
            "web3.career": r"^/[^/]+/\d+/?$",
            "www.hirist.tech": r"^/j/",
        }
        pattern = patterns.get(parts.hostname)
        if pattern:
            if re.search(pattern, parts.path, re.I):
                details.append(url)
            continue
        if re.search(r"/job-details/[0-9a-f-]{36}/overview", path):
            details.append(url)
        elif re.search(r"/job-\d|/job/|/jobs/[^/]+|/careers/(?:details/)?[^/]+|/positions/[^/]+|/opportunities/[^/]+|/p/[a-z0-9]{6,}", path):
            if path.rstrip("/").split("/")[-1] not in {"all", "all-jobs", "engineering", "benefits", "hiring-process", "company-culture", "about", "jobs", "search", "remote", "locations"}:
                details.append(url)
        elif host == "jobs.weekday.works" and path != "/":
            details.append(url)
        elif parts.hostname == "jobs.weekday.works" and path != "/":
            details.append(url)
    details.extend(embedded_links(soup))
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
    hydrated = False
    listing_records: dict[str, Job] = {}
    while queue and result.pages_fetched < source.max_pages:
        url = queue.popleft()
        if url in visited:
            continue
        visited.add(url)
        try:
            response = await fetch.request(url, html=True)
        except Exception as exc:
            if isinstance(exc, BlockedError):
                raise
            if len(seeds) > 1:
                result.partial(f"Listing page failed: {url}: {type(exc).__name__}: {str(exc)[:180]}")
                continue
            raise
        text = response.text
        url = str(response.url)
        result.pages_fetched += 1
        soup = BeautifulSoup(text, "html.parser")
        if source.group == "employer" and not source.options.get("skip_ats_detection"):
            matches = find_ats(text, url, source)
            if matches:
                for detected in matches:
                    if detected.kind == "teamtailor":
                        await collect_feed(detected, fetch, result)
                    else:
                        await collect_ats(detected, fetch, result)
                return
        links, indexes = job_links(soup, url)
        listed = listing_jobs(soup, url, source)
        explicit_empty = explicit_empty or has_no_openings(soup)
        if not listed and not links and not explicit_empty and source.options.get("render"):
            rendered = await fetch.render(url, selector=source.options.get("render_wait_selector"),
                                          next_selector=source.options.get("render_next_selector"),
                                          max_pages=min(source.options.get("render_max_pages", 1), source.max_pages - result.pages_fetched + 1))
            soup = BeautifulSoup(rendered, "html.parser")
            hydrated = True
            stats = fetch.renderer.stats.get(url, {}) if fetch.renderer else {}
            result.pages_fetched += max(0, stats.get("pages", 1) - 1)
            if stats.get("limited"):
                result.partial("More rendered listing pages exist beyond the configured browser page limit")
            links, indexes = job_links(soup, url)
            listed = listing_jobs(soup, url, source)
            explicit_empty = explicit_empty or has_no_openings(soup)
        for job in listed:
            result.jobs.append(job)
            listing_records[canonical_url(job.url)] = job
            if len(job.description) >= 500:
                details_seen.add(canonical_url(job.url))
        if listing_records:
            def priority(link: str) -> int:
                record = listing_records.get(canonical_url(link))
                if record is None:
                    return 2
                if NON_TECH.search(record.title) or not TECH.search(record.title):
                    return 3
                return 0 if INDIA.search(record.location) or WORLDWIDE.search(record.location) else 1
            links.sort(key=priority)
        visible = soup.get_text(" ", strip=True)
        for next_url in indexes:
            if next_url not in visited:
                queue.append(next_url)
        if not indexes and re.search(r"Next\s*(?:»|›)|Showing\s+1\s*[-–]\s*\d+\s+(?:out )?of\s+\d{3,}", visible):
            result.partial("Listing has pagination controls but no crawlable next-page URL was exposed")
        for detail_url in links:
            identity = canonical_url(detail_url)
            if identity in details_seen:
                continue
            details_seen.add(identity)
            if details_done >= source.max_details:
                result.partial(f"Detail budget ({source.max_details}) reached; more listings may exist")
                break
            details_done += 1
            try:
                detail_response = await fetch.request(detail_url, html=True)
                detail_html = detail_response.text
                jobs = parse_detail(detail_html, str(detail_response.url), source)
                if not jobs and source.options.get("render_details"):
                    detail_html = await fetch.render(str(detail_response.url), selector=source.options.get("detail_wait_selector", "h1"))
                    jobs = parse_detail(detail_html, str(detail_response.url), source)
                if jobs:
                    existing = listing_records.get(identity)
                    if existing and len(jobs) == 1:
                        # Keep reliable listing country/ID metadata when a detail page lacks it.
                        jobs[0].sources[0].source_job_id = existing.sources[0].source_job_id
                        if not jobs[0].location:
                            jobs[0].location = existing.location
                            jobs[0].countries = existing.countries
                        jobs[0].application_url = existing.application_url
                        jobs[0].id = existing.id
                    result.jobs.extend(jobs)
                else:
                    # A directory link can lead to another index rather than a vacancy.
                    nested = BeautifulSoup(detail_html, "html.parser")
                    children, more = job_links(nested, str(detail_response.url))
                    if (children or more) and detail_url not in visited:
                        queue.append(detail_url)
            except BlockedError as exc:
                result.partial(f"Detail access blocked: {exc}")
                if "robots.txt" not in str(exc):
                    # Do not keep hammering an origin after it denies access.
                    result.warn("Stopped source traversal after access denial; no bypass attempted")
                    queue.clear()
                    break
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
    if hydrated:
        result.warn("JavaScript-rendered public listing page used")
