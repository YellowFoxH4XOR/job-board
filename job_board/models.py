from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def plain(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(filter(None, (plain(v) for v in value)))
    if isinstance(value, dict):
        return plain(value.get("name") or value.get("value") or "")
    text = html.unescape(str(value))
    if re.search(r"</?[a-zA-Z][^>]*>", text):
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    """Normalize identity only; retain the untouched original/application URLs in output."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or "/"
    if parts.hostname in {"jobs.ashbyhq.com", "jobs.lever.co", "jobs.eu.lever.co"}:
        path = re.sub(r"/(?:application|apply)$", "", path)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")
             and k.lower() not in {"gh_src", "source", "ref", "referrer", "trackingid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    kind: str
    url: str
    company: str | None = None
    tenant: str | None = None
    group: Literal["feed", "board", "employer", "portal"] = "employer"
    enabled: bool = True
    max_pages: int = 100
    max_details: int = 250
    timeout_seconds: int = 600
    options: dict[str, Any] = Field(default_factory=dict)
    discovery_url: str | None = None


class Reference(BaseModel):
    source_id: str
    source_name: str
    source_job_id: str
    url: str
    collected_at: str


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = ""
    title: str
    company: str
    location: str = ""
    countries: list[str] = Field(default_factory=list)
    workplace_type: Literal["remote", "hybrid", "onsite", "unknown"] = "unknown"
    employment_type: str | None = None
    description: str = ""
    salary: dict[str, Any] | None = None
    skills: list[str] = Field(default_factory=list)
    experience: str | None = None
    posted_at: str | None = None
    updated_at: str | None = None
    valid_through: str | None = None
    application_url: str | None = None
    url: str
    first_seen_at: str = ""
    last_seen_at: str = ""
    status: Literal["listed", "closed", "talent_pool"] = "listed"
    india_eligibility: Literal["india", "worldwide", "unknown", "excluded"] = "unknown"
    eligibility_evidence: str = ""
    remote_scope: str | None = None
    requirements: list[str] = Field(default_factory=list)
    sources: list[Reference] = Field(default_factory=list)


class SourceResult(BaseModel):
    source: Source
    status: Literal["success", "partial", "failed", "blocked", "unsupported"] = "success"
    complete: bool = True
    coverage_scope: str = "published jobs on this configured source"
    started_at: str = Field(default_factory=now_iso)
    finished_at: str | None = None
    pages_fetched: int = 0
    jobs_seen: int = 0
    jobs_kept: int = 0
    expected_count: int | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    jobs: list[Job] = Field(default_factory=list)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def partial(self, message: str) -> None:
        self.complete = False
        if self.status == "success":
            self.status = "partial"
        self.warn(message)

    def summary(self) -> dict:
        return self.model_dump(exclude={"jobs"})


def make_job(source: Source, source_job_id: Any, title: Any, company: Any, url: str,
             **kwargs: Any) -> Job:
    when = now_iso()
    job = Job(
        title=plain(title), company=plain(company) or source.company or source.name,
        url=url, last_seen_at=when,
        sources=[Reference(source_id=source.id, source_name=source.name,
                           source_job_id=str(source_job_id or url), url=url,
                           collected_at=when)], **kwargs,
    )
    # Job-specific application URLs unify syndication; generic application roots do not.
    identity_url = job.application_url or job.url
    parts = urlsplit(identity_url)
    if parts.path.rstrip("/").lower() in {"", "/jobs", "/careers", "/apply"} and not parts.query:
        identity_url = job.url
    identity = canonical_url(identity_url) or f"{source.id}:{source_job_id}"
    job.id = hashlib.sha256(identity.encode()).hexdigest()[:24]
    return job
