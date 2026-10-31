from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin, urlsplit

from .filtering import workplace
from .http import Fetcher
from .models import Source, SourceResult, make_job, plain


async def collect_ats(source: Source, fetch: Fetcher, result: SourceResult) -> None:
    tenant = quote(source.tenant or "", safe="")
    if source.kind == "greenhouse":
        data = await fetch.json(f"https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs?content=true")
        result.pages_fetched += 1
        result.expected_count = data.get("meta", {}).get("total")
        for item in data["jobs"]:
            description = plain(item.get("content"))
            location = plain(item.get("location"))
            result.jobs.append(make_job(
                source, item["id"], item["title"], source.company, item["absolute_url"],
                location=location, description=description, updated_at=item.get("updated_at"),
                workplace_type=workplace(None, location, description),
                application_url=item["absolute_url"],
            ))
    elif source.kind == "lever":
        host = "api.eu.lever.co" if source.options.get("region") == "eu" else "api.lever.co"
        for page in range(source.max_pages):
            data = await fetch.json(f"https://{host}/v0/postings/{tenant}?mode=json&limit=100&skip={page * 100}")
            if not isinstance(data, list):
                raise ValueError("Lever did not return a postings list")
            result.pages_fetched += 1
            for item in data:
                cats = item.get("categories", {})
                description = "\n".join(filter(None, [plain(item.get("descriptionPlain") or item.get("description")),
                    *[f"{plain(x.get('text'))}: {plain(x.get('content'))}" for x in item.get("lists", [])],
                    plain(item.get("additionalPlain") or item.get("additional"))]))
                location = plain(cats.get("allLocations") or cats.get("location"))
                result.jobs.append(make_job(
                    source, item["id"], item["text"], source.company, item["hostedUrl"],
                    location=location, description=description,
                    workplace_type=workplace(item.get("workplaceType"), location, description),
                    countries=[item["country"]] if item.get("country") else [],
                    employment_type=cats.get("commitment"), salary=item.get("salaryRange"),
                    application_url=item.get("applyUrl") or item["hostedUrl"],
                ))
            if len(data) < 100:
                break
        else:
            result.partial("Lever page budget reached")
    elif source.kind == "ashby":
        data = await fetch.json(f"https://api.ashbyhq.com/posting-api/job-board/{tenant}?includeCompensation=true")
        result.pages_fetched += 1
        for item in data["jobs"]:
            if item.get("isListed") is False:
                continue
            location = plain([item.get("location"), *[x.get("location") for x in item.get("secondaryLocations", [])]])
            address = item.get("address") or {}
            postal = address.get("postalAddress") or address
            countries = [postal.get("addressCountry")]
            countries += [(x.get("address") or {}).get("addressCountry") for x in item.get("secondaryLocations", [])]
            description = item.get("descriptionPlain") or plain(item.get("descriptionHtml"))
            result.jobs.append(make_job(
                source, item.get("id") or item["jobUrl"], item["title"], source.company, item["jobUrl"],
                location=location, description=description, countries=[c for c in countries if c],
                workplace_type=workplace(item.get("workplaceType"), location, description),
                employment_type=item.get("employmentType"), posted_at=item.get("publishedAt"),
                salary=item.get("compensation"), application_url=item.get("applyUrl"),
            ))
    elif source.kind == "smartrecruiters":
        for page in range(source.max_pages):
            data = await fetch.json(f"https://api.smartrecruiters.com/v1/companies/{tenant}/postings?limit=100&offset={page * 100}")
            result.pages_fetched += 1
            result.expected_count = data.get("totalFound")
            items = data.get("content", [])
            for item in items:
                detail = await fetch.json(item["ref"])
                loc = detail.get("location") or {}
                sections = detail.get("jobAd", {}).get("sections", {})
                description = "\n".join(plain(v.get("text")) for v in sections.values() if isinstance(v, dict))
                url = detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{tenant}/{detail['id']}"
                result.jobs.append(make_job(
                    source, detail["id"], detail["name"], source.company, url,
                    location=plain([loc.get("city"), loc.get("region"), loc.get("country")]),
                    countries=[loc["country"]] if loc.get("country") else [],
                    workplace_type="remote" if loc.get("remote") else workplace(None, description=description),
                    description=description, posted_at=detail.get("releasedDate"),
                    employment_type=plain(detail.get("typeOfEmployment")) or None,
                    application_url=detail.get("applyUrl") or url,
                ))
            if len(items) < 100 or (result.expected_count is not None and (page + 1) * 100 >= result.expected_count):
                break
        else:
            result.partial("SmartRecruiters page budget reached")
    elif source.kind == "workable":
        data = await fetch.json(f"https://www.workable.com/api/accounts/{tenant}?details=true")
        result.pages_fetched += 1
        items = data.get("jobs", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("Workable response has no jobs list")
        for item in items:
            loc = item.get("location") or {}
            description = "\n".join(plain(item.get(k)) for k in ("description", "requirements", "benefits") if item.get(k))
            location = plain(loc) if not isinstance(loc, dict) else plain([loc.get("city"), loc.get("country")])
            url = item.get("url") or item.get("shortlink") or f"https://apply.workable.com/j/{item['shortcode']}/"
            result.jobs.append(make_job(
                source, item.get("id") or item.get("shortcode"), item.get("title"), source.company, url,
                description=description, location=location,
                countries=[loc["country_code"]] if isinstance(loc, dict) and loc.get("country_code") else [],
                workplace_type=workplace(loc.get("workplace_type") if isinstance(loc, dict) else None, location, description),
                posted_at=item.get("published_on") or item.get("created_at"),
                employment_type=item.get("employment_type"), application_url=item.get("application_url") or url,
            ))
    elif source.kind == "workday":
        parts = urlsplit(source.url)
        company = parts.hostname.split(".")[0]
        site = source.options.get("site") or parts.path.rstrip("/").split("/")[-1]
        base = f"https://{parts.netloc}/wday/cxs/{company}/{site}"
        for page in range(source.max_pages):
            data = await fetch.json(f"{base}/jobs", method="POST",
                                    data={"appliedFacets": {}, "limit": 20, "offset": page * 20, "searchText": ""})
            result.pages_fetched += 1
            result.expected_count = data.get("total")
            items = data.get("jobPostings", [])
            for item in items:
                path = item["externalPath"]
                detail = await fetch.json(f"{base}{path}")
                info = detail.get("jobPostingInfo", {})
                if not info or info.get("canApply") is False:
                    continue
                description = plain(info.get("jobDescription"))
                location = plain([info.get("location"), info.get("additionalLocations")])
                url = info.get("externalUrl") or f"https://{parts.netloc}/en-US/{site}{path}"
                result.jobs.append(make_job(
                    source, info.get("jobRequisitionId") or path, info.get("title") or item["title"], source.company, url,
                    location=location, description=description,
                    workplace_type=workplace(info.get("remoteType"), location, description),
                    employment_type=info.get("timeType"), posted_at=info.get("startDate"),
                    application_url=url,
                ))
            if not items or (result.expected_count is not None and (page + 1) * 20 >= result.expected_count):
                break
        else:
            result.partial("Workday page budget reached")
    elif source.kind == "personio":
        text = await fetch.text(source.url.rstrip("/") + "/xml?language=en")
        root = ET.fromstring(text)
        result.pages_fetched += 1
        for item in root.findall(".//position"):
            get = lambda key, element=item: element.findtext(key) or ""  # noqa: E731
            url = urljoin(source.url.rstrip("/") + "/", f"job/{get('id')}")
            description = "\n".join(plain(x.text) for x in item.findall(".//jobDescription/value"))
            result.jobs.append(make_job(source, get("id"), get("name"), source.company, url,
                                       location=get("office"), description=description,
                                       employment_type=get("employmentType"), application_url=url))
    else:
        raise ValueError(f"Unsupported ATS kind: {source.kind}")
