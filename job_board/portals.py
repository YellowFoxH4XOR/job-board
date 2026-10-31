from __future__ import annotations

import asyncio
import json
import sys

from .filtering import workplace
from .models import Source, SourceResult, make_job, plain


async def query_batch(request: dict, timeout: int) -> dict:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "job_board.portals_worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(request).encode()), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if proc.returncode:
        raise RuntimeError(f"JobSpy failed: {err.decode(errors='replace')[-700:]}")
    return json.loads(out)


async def collect_portal(source: Source, result: SourceResult) -> None:
    limit = source.options.get("results_per_query", 1000)
    batch_size = source.options.get("batch_size", 200)
    queries = source.options.get("queries", ["software engineer"])
    result.partial("Keyword-query coverage; portals may cap results and this is not a full-site census")
    result.coverage_scope = "Configured keyword searches; descriptions are retained as supplied by the search response"
    successful_batches = 0
    for query in queries:
        seen_ids = set()
        for offset in range(0, limit, batch_size):
            wanted = min(batch_size, limit - offset)
            try:
                data = await query_batch(
                    {"site": source.tenant, "query": query, "limit": wanted, "offset": offset,
                     "fetch_description": source.options.get("fetch_description", False)},
                    source.options.get("query_timeout_seconds", 150),
                )
            except TimeoutError:
                result.warn(f"{query}, offset {offset}: batch timed out; earlier batches retained")
                break
            except Exception as exc:
                result.warn(f"{query}, offset {offset}: {type(exc).__name__}: {str(exc)[:700]}")
                break
            result.pages_fetched += 1
            records = data.get("jobs", [])
            errors = str(data.get("errors") or "")
            if errors:
                result.warn(f"{query}, offset {offset}: {errors[:700]}")
            else:
                successful_batches += 1
            if data.get("warnings"):
                result.warn(f"{query}: {str(data['warnings'])[:700]}")
            new_records = 0
            for item in records:
                url = item.get("job_url")
                key = item.get("id") or url
                if not url or not item.get("title") or key in seen_ids:
                    continue
                seen_ids.add(key)
                new_records += 1
                location = plain(item.get("location"))
                description = plain(item.get("description"))
                result.jobs.append(make_job(source, key, item["title"], item.get("company"), url,
                    location=location, description=description,
                    workplace_type=workplace("remote" if item.get("is_remote") else None, location, description),
                    employment_type=plain(item.get("job_type")) or None,
                    posted_at=item.get("date_posted"), application_url=item.get("job_url_direct") or url,
                    skills=item["skills"] if isinstance(item.get("skills"), list) else [],
                    salary={"min": item.get("min_amount"), "max": item.get("max_amount"),
                            "currency": item.get("currency"), "period": item.get("interval")},
                    experience=plain(item.get("experience_range")) or None))
            if errors and any(word in errors.lower() for word in ("403", "429", "blocked", "captcha")):
                result.warn("Stopped remaining queries after source access denial; no bypass attempted")
                if not result.jobs:
                    result.status = "blocked"
                    result.errors.append("Portal denied access")
                return
            if errors or len(records) < wanted:
                break
            if not new_records:
                result.warn(f"{query}: pagination repeated existing IDs; stopping this query")
                break
        else:
            result.warn(f"{query}: configured result limit reached ({limit})")
    if not successful_batches and not result.jobs:
        result.status = "failed"
        result.errors.append("Every portal batch failed or timed out")
