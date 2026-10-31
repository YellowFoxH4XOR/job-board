from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from pathlib import Path

from .ats import collect_ats
from .feeds import collect_feed
from .filtering import classify
from .http import BlockedError, Fetcher
from .models import Source, SourceResult, now_iso
from .portals import collect_portal
from .web import ATS_KINDS, collect_web

LOG = logging.getLogger(__name__)
FEED_KINDS = {"remotive", "remoteok", "fourdayweek", "himalayas", "workingnomads", "jobicy", "rss", "teamtailor"}


async def collect_one(source: Source, fetch: Fetcher) -> SourceResult:
    result = SourceResult(source=source)
    LOG.info("Collecting %s (%s)", source.name, source.kind)
    try:
        async with asyncio.timeout(source.timeout_seconds):
            if source.kind in ATS_KINDS:
                await collect_ats(source, fetch, result)
            elif source.kind in FEED_KINDS:
                await collect_feed(source, fetch, result)
            elif source.kind == "jobspy":
                await collect_portal(source, result)
            else:
                await collect_web(source, fetch, result)
    except BlockedError as exc:
        result.status = "partial" if result.jobs else "blocked"
        result.complete = False
        result.errors.append(str(exc))
    except TimeoutError:
        result.status = "partial" if result.jobs else "failed"
        result.complete = False
        result.errors.append(f"Source time budget reached ({source.timeout_seconds}s)")
    except Exception as exc:
        result.status = "partial" if result.jobs else "failed"
        result.complete = False
        result.errors.append(f"{type(exc).__name__}: {str(exc)[:500]}")
    seen = {}
    rejected = Counter()
    for job in result.jobs:
        key = (job.sources[0].source_id, job.sources[0].source_job_id)
        seen[key] = job
    result.jobs_seen = len(seen)
    kept = []
    for job in seen.values():
        eligible, reason = classify(job)
        if eligible:
            kept.append(job)
        else:
            rejected[reason] += 1
    result.jobs = kept
    result.jobs_kept = len(kept)
    if rejected:
        result.warn("Filtered: " + "; ".join(f"{reason}: {count}" for reason, count in rejected.items()))
    result.finished_at = now_iso()
    LOG.info("%s: %s, %d seen, %d matching", source.id, result.status, result.jobs_seen, result.jobs_kept)
    return result


async def collect_shard(plan_path: str, shard: str, output_dir: str, concurrency: int = 4) -> dict:
    plan = json.loads(Path(plan_path).read_text())
    sources = [Source.model_validate(s) for s in plan["shards"][shard]]
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    fetch = Fetcher()
    gate = asyncio.Semaphore(concurrency)

    async def work(source: Source) -> SourceResult:
        async with gate:
            result = await collect_one(source, fetch)
            # Checkpoint every source so an interrupted shard does not discard its completed work.
            (destination / f"source-{source.id}.json").write_text(result.model_dump_json() + "\n")
            return result

    try:
        results = await asyncio.gather(*(work(source) for source in sources))
    finally:
        await fetch.close()
    summary = {"shard": shard, "finished_at": now_iso(),
               "sources": [r.summary() for r in results], "job_count": sum(r.jobs_kept for r in results)}
    (destination / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
