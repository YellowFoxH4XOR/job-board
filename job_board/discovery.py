from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from bs4 import BeautifulSoup

from .http import Fetcher
from .models import Source, now_iso
from .web import detect_ats, find_ats

DIRECTORY_REPO = "remoteintech/remote-jobs"
ARCHIVE = f"https://codeload.github.com/{DIRECTORY_REPO}/tar.gz/refs/heads/main"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def load_sources(path: str) -> list[Source]:
    data = yaml.safe_load(Path(path).read_text())
    return [Source.model_validate(item) for item in data.get("sources", []) if item.get("enabled", True)]


def deduplicate_sources(sources: list[Source]) -> list[Source]:
    """Curated entries come first and override stale directory ATS mappings."""
    identities = set()
    ids = set()
    unique = []
    for source in sources:
        identity = (source.kind, source.tenant or source.url.rstrip("/"), source.options.get("site", ""))
        if source.id in ids or identity in identities:
            continue
        ids.add(source.id)
        identities.add(identity)
        unique.append(source)
    return unique


def read_directory_archive(content: bytes) -> list[dict]:
    records = []
    # Read individual text members in memory; never extract or execute the repository.
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or not re.search(r"/src/companies/[^/]+\.md$", member.name) or member.size > 200_000:
                continue
            stream = archive.extractfile(member)
            if stream is None:
                continue
            text = stream.read().decode("utf-8", errors="replace")
            if not text.startswith("---"):
                continue
            parts = text.split("---", 2)
            if len(parts) != 3:
                continue
            data = yaml.safe_load(parts[1]) or {}
            url = data.get("careers_url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                continue
            records.append({"slug": data.get("slug") or slug(data.get("title", "")),
                            "name": data.get("title", ""), "careers_url": url,
                            "website": data.get("website"),
                            "region_hint": data.get("region"), "remote_policy_hint": data.get("remote_policy"),
                            "directory_url": f"https://github.com/{DIRECTORY_REPO}/blob/main/" + member.name.split("/", 1)[1]})
    return records


async def discover(config_dir: str, output_dir: str, *, new_limit: int = 80, mode: str = "full") -> dict:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    sources = load_sources(f"{config_dir}/sources.yaml") + load_sources(f"{config_dir}/employers.yaml")
    state_path = Path("registry/discovered.json")
    previous = json.loads(state_path.read_text()) if state_path.exists() else {"companies": []}
    old = {item["slug"]: item for item in previous.get("companies", [])}
    report_path = Path("reports/latest.json")
    last_results = json.loads(report_path.read_text()).get("sources", []) if report_path.exists() else []
    invalid_sources = {r["source"]["id"] for r in last_results if r["status"] == "failed"
                       and any(code in " ".join(r.get("errors", [])) for code in ("404", "410", "422"))}
    curated = {s.id: s for s in sources}
    errors = []
    fetch = Fetcher()
    records = []
    try:
        if mode == "full":
            try:
                response = await fetch.request(ARCHIVE, cache=False)
                if len(response.content) > 60_000_000:
                    raise ValueError("Discovery archive exceeds 60 MB")
                records = read_directory_archive(response.content)
                if not records:
                    raise ValueError("No company records found; upstream directory schema may have changed")
            except Exception as exc:
                errors.append(f"Directory refresh failed; retaining previous registry: {type(exc).__name__}: {str(exc)[:250]}")
        if not records:
            records = list(old.values())
        for record in records:
            if record["slug"] in old:
                current = record.copy()
                record.update(old[record["slug"]])
                record.update(current)
                if current["careers_url"] != old[record["slug"]].get("careers_url"):
                    record["needs_revalidation"] = True

        curated_hosts = {urlsplit(s.url).hostname for s in sources if s.group == "employer"}
        resolved = []
        unresolved = []
        for record in records:
            source_id = f"employer-{record['slug']}"
            if source_id in curated:
                record["source"] = curated[source_id].model_dump()
                record["discovery_status"] = "curated_override"
                record.pop("needs_revalidation", None)
            elif source_id in invalid_sources:
                record["needs_revalidation"] = True
            if record.get("source") and not record.get("needs_revalidation"):
                resolved.append(record)
                continue
            direct = detect_ats(record["careers_url"], name=record["name"], source_id=f"employer-{record['slug']}")
            if direct and not record.get("needs_revalidation"):
                direct.discovery_url = record["directory_url"]
                record["source"] = direct.model_dump()
                record["discovery_status"] = "ats_detected"
                resolved.append(record)
            elif record.get("needs_revalidation") or urlsplit(record["careers_url"]).hostname not in curated_hosts:
                unresolved.append(record)
        unresolved.sort(key=lambda r: (not r.get("needs_revalidation", False), r.get("last_checked_at", ""), r.get("region_hint") not in {"worldwide", "asia-pacific"}, r["slug"]))
        gate = asyncio.Semaphore(8)

        async def resolve(record: dict) -> None:
            async with gate:
                record["last_checked_at"] = now_iso()
                seed = Source(id=f"employer-{record['slug']}", name=record["name"], company=record["name"],
                              kind="generic", url=record["careers_url"])
                try:
                    try:
                        response = await asyncio.wait_for(fetch.request(seed.url, html=True), 55)
                    except httpx.HTTPStatusError as exc:
                        website = record.get("website")
                        if exc.response.status_code not in {404, 410, 422} or not record.get("needs_revalidation") or not website:
                            raise
                        response = await asyncio.wait_for(fetch.request(website, html=True), 55)
                    matches = find_ats(response.text, str(response.url), seed)
                    if not matches and record.get("needs_revalidation"):
                        soup = BeautifulSoup(response.text, "html.parser")
                        career_links = [urljoin(str(response.url), a["href"]) for a in soup.select("a[href]")
                                        if re.search(r"careers|recruit|recrut|join us|rejoindre|open positions", a.get_text(" ", strip=True), re.I)]
                        for career_url in list(dict.fromkeys(career_links))[:2]:
                            response = await asyncio.wait_for(fetch.request(career_url, html=True), 45)
                            matches = find_ats(response.text, str(response.url), seed)
                            if matches:
                                break
                    if matches:
                        matches[0].discovery_url = record["directory_url"]
                        record["source"] = matches[0].model_dump()
                        record["discovery_status"] = "ats_detected"
                        record.pop("discovery_error", None)
                        record.pop("needs_revalidation", None)
                    else:
                        record["discovery_status"] = "needs_adapter"
                        record["canonical_careers_url"] = str(response.url)
                except Exception as exc:
                    record["discovery_status"] = "unresolved"
                    record["discovery_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

        if mode == "full":
            await asyncio.gather(*(resolve(r) for r in unresolved[:new_limit]))
        for record in records:
            if record.get("source"):
                sources.append(Source.model_validate(record["source"]))
    finally:
        await fetch.close()

    # One tenant can appear under multiple products/brands and in both seed lists.
    sources = deduplicate_sources(sources)
    if mode == "smoke":
        smoke = {"remotive", "remoteok", "employer-supabase", "employer-highlevel", "employer-gitlab", "employer-automattic", "railway", "weekday", "instahyre", "linkedin", "naukri"}
        sources = [s for s in sources if s.id in smoke]
        for source in sources:
            source.max_pages = min(source.max_pages, 3)
            source.max_details = min(source.max_details, 8)
            source.timeout_seconds = min(source.timeout_seconds, 120)
            if source.kind == "jobspy":
                source.options = {**source.options, "queries": ["software engineer"],
                                  "results_per_query": 100, "batch_size": min(source.options.get("batch_size", 200), 100),
                                  "query_timeout_seconds": source.options.get("query_timeout_seconds", 90)}
    shards: dict[str, list[dict]] = {}
    counters = {"employer": 0, "board": 0}
    for source in sources:
        if source.group == "portal":
            key = f"portal-{source.tenant}"
        elif source.group == "feed":
            key = "feeds"
        else:
            number = counters[source.group]
            counters[source.group] += 1
            key = f"{source.group}-{number % 4}"
        shards.setdefault(key, []).append(source.model_dump())
    registry = {"updated_at": now_iso(), "directory": f"https://github.com/{DIRECTORY_REPO}",
                "profile_count": len(records), "errors": errors, "companies": records}
    (destination / "registry.json").write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    plan = {"created_at": now_iso(), "mode": mode, "sources": len(sources), "shards": shards,
            "discovery_errors": errors, "discovered_profiles": len(records)}
    (destination / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    matrix = {"include": [{"shard": name, "portals": name.startswith("portal-")} for name in shards]}
    (destination / "matrix.json").write_text(json.dumps(matrix))
    return plan
