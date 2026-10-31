from __future__ import annotations

import asyncio
import json
import sys

from .filtering import workplace
from .models import Source, SourceResult, make_job, plain


async def collect_portal(source: Source, result: SourceResult) -> None:
    limit = source.options.get("results_per_query", 1000)
    queries = source.options.get("queries", ["software engineer"])
    result.partial("Keyword-query coverage; portals may cap results and this is not a full-site census")
    failed = 0
    for query in queries:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "job_board.portals_worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps({"site": source.tenant, "query": query, "limit": limit}).encode()),
                source.options.get("query_timeout_seconds", 150),
            )
        except (TimeoutError, asyncio.CancelledError):
            proc.kill()
            await proc.communicate()
            if asyncio.current_task().cancelling():
                raise
            failed += 1
            result.warn(f"{query}: query timed out")
            continue
        if proc.returncode:
            failed += 1
            result.warn(f"{query}: JobSpy failed: {err.decode(errors='replace')[-700:]}")
            continue
        try:
            data = json.loads(out)
        except ValueError:
            failed += 1
            result.warn(f"{query}: JobSpy returned malformed output")
            continue
        result.pages_fetched += 1
        if data.get("errors"):
            failed += 1
            result.warn(f"{query}: {str(data['errors'])[:700]}")
        if data.get("warnings"):
            result.warn(f"{query}: {str(data['warnings'])[:700]}")
        records = data.get("jobs", [])
        if len(records) >= limit:
            result.warn(f"{query}: result limit reached ({limit})")
        for item in records:
            url = item.get("job_url")
            if not url or not item.get("title"):
                continue
            location = plain(item.get("location"))
            description = plain(item.get("description"))
            result.jobs.append(make_job(source, item.get("id") or url, item["title"], item.get("company"), url,
                location=location, description=description,
                workplace_type=workplace("remote" if item.get("is_remote") else None, location, description),
                employment_type=plain(item.get("job_type")) or None,
                posted_at=item.get("date_posted"), application_url=item.get("job_url_direct") or url,
                skills=item["skills"] if isinstance(item.get("skills"), list) else [],
                salary={"min": item.get("min_amount"), "max": item.get("max_amount"),
                        "currency": item.get("currency"), "period": item.get("interval")},
                experience=plain(item.get("experience_range")) or None))
    if failed == len(queries) and not result.jobs:
        result.status = "failed"
        result.errors.append("Every portal query failed or was blocked")
