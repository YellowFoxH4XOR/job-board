from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Job, Source, SourceResult, now_iso


def merge_jobs(jobs: list[Job], first_seen: dict[str, str], observed_at: str) -> list[Job]:
    merged: dict[str, Job] = {}
    # Prefer the fuller description when the same canonical application is syndicated.
    for job in sorted(jobs, key=lambda j: len(j.description), reverse=True):
        if job.id not in merged:
            merged[job.id] = job.model_copy(deep=True)
        else:
            existing = merged[job.id]
            known = {(r.source_id, r.source_job_id) for r in existing.sources}
            existing.sources.extend(r for r in job.sources if (r.source_id, r.source_job_id) not in known)
            if not existing.salary and job.salary:
                existing.salary = job.salary
    for job in merged.values():
        keys = [job.id] + [f"{r.source_id}:{r.source_job_id}" for r in job.sources]
        earliest = min([first_seen[k] for k in keys if k in first_seen] or [observed_at])
        job.first_seen_at = earliest
        for key in keys:
            first_seen[key] = earliest
        job.sources.sort(key=lambda r: (r.source_id, r.source_job_id))
    return sorted(merged.values(), key=lambda j: (j.company.lower(), j.title.lower(), j.id))


def atomic_json(path: Path, value, *, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False,
                  indent=2 if pretty else None, separators=None if pretty else (",", ":"))
        handle.write("\n")
    temporary.replace(path)


def publish(plan_path: str, results_dir: str, *, root_dir: str = ".", registry_path: str | None = None) -> dict:
    root = Path(root_dir)
    plan = json.loads(Path(plan_path).read_text())
    expected = {s["id"]: Source.model_validate(s) for group in plan["shards"].values() for s in group}
    results = {}
    for path in Path(results_dir).rglob("source-*.json"):
        result = SourceResult.model_validate_json(path.read_text())
        if result.source.id in expected:
            results[result.source.id] = result
    for source_id, source in expected.items():
        if source_id not in results:
            results[source_id] = SourceResult(source=source, status="failed", complete=False,
                finished_at=now_iso(), errors=["No shard artifact received for this source"])
    when = now_iso()
    date = datetime.fromisoformat(plan["created_at"]).astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
    counts = Counter(r.status for r in results.values())
    report = {"date": date, "started_at": plan["created_at"], "finished_at": when,
              "run_url": os.getenv("RUN_URL"), "mode": plan.get("mode", "full"),
              "discovered_profiles": plan.get("discovered_profiles", 0),
              "discovery_errors": plan.get("discovery_errors", []),
              "source_counts": dict(counts), "sources": [r.summary() for r in sorted(results.values(), key=lambda x: x.source.id)]}
    atomic_json(root / "reports" / f"{date}.json", report, pretty=True)
    atomic_json(root / "reports" / "latest.json", report, pretty=True)
    successful = [r for r in results.values() if r.status in {"success", "partial"}]
    jobs = [job for result in successful for job in result.jobs]
    if not successful or not jobs:
        raise RuntimeError("No matching jobs collected; preserving the previous day.json. See reports/latest.json.")
    first_path = root / "state" / "first_seen.json"
    first_seen = json.loads(first_path.read_text()) if first_path.exists() else {}
    jobs = merge_jobs(jobs, first_seen, when)
    complete = all(r.complete and r.status == "success" for r in results.values()) and not plan.get("discovery_errors")
    snapshot = {"schema_version": 1, "date": date, "timezone": "Asia/Kolkata",
                "scheduled_time": "20:00", "started_at": plan["created_at"], "collected_at": when,
                "run_url": os.getenv("RUN_URL"), "mode": plan.get("mode", "full"),
                "complete": complete,
                "coverage_note": "Observed, matching listings from configured sources. See source coverage; remote does not imply worldwide eligibility. Missing records from failed/partial sources are not confirmed closures.",
                "job_count": len(jobs), "source_counts": dict(counts),
                "sources": report["sources"], "jobs": [job.model_dump() for job in jobs]}
    # Validate everything before replacing either published file.
    encoded = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > 90_000_000:
        raise RuntimeError("Snapshot exceeds the 90 MB Git publication guard; previous snapshot preserved")
    atomic_json(root / "data" / f"{date}.json", snapshot)
    atomic_json(root / "day.json", snapshot)
    atomic_json(first_path, first_seen)
    if registry_path and plan.get("mode") == "full":
        registry = json.loads(Path(registry_path).read_text())
        atomic_json(root / "registry" / "discovered.json", registry, pretty=True)
    summary = f"# Daily job collection\n\n- Date: **{date} (IST)**\n- Matching jobs: **{len(jobs)}**\n- Sources: **{len(results)}**\n- Coverage complete: **{complete}**\n- Source outcomes: `{dict(counts)}`\n\n| Source | Status | Seen | Kept | Notes |\n|---|---|---:|---:|---|\n"
    for result in sorted(results.values(), key=lambda r: r.source.id):
        notes = "; ".join(result.errors + result.warnings)[:300].replace("|", "/").replace("\n", " ")
        summary += f"| {result.source.name} | {result.status} | {result.jobs_seen} | {result.jobs_kept} | {notes} |\n"
    (root / "reports" / "latest.md").write_text(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as handle:
            handle.write(summary)
    return {"date": date, "job_count": len(jobs), "sources": len(results), "complete": complete, "source_counts": dict(counts)}
