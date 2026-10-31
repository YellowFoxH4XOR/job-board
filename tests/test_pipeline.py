import io
import json
import tarfile

import pytest

from job_board.discovery import deduplicate_sources, load_sources, read_directory_archive
from job_board.models import Source, SourceResult, canonical_url, make_job
from job_board.publish import merge_jobs, publish


def test_all_configured_sources_have_unique_ids():
    sources = load_sources("config/sources.yaml") + load_sources("config/employers.yaml")
    assert len(sources) == len({s.id for s in sources})
    assert {"weekday", "instahyre", "railway", "employer-supabase", "employer-automattic", "naukri"} <= {s.id for s in sources}


def test_curated_ats_migration_wins_over_stale_directory_entry():
    curated = Source(id="employer-deel", name="Deel", kind="generic", url="https://www.deel.com/careers/")
    stale = curated.model_copy(update={"kind": "ashby", "tenant": "deel", "url": "https://jobs.ashbyhq.com/deel"})
    assert deduplicate_sources([curated, stale]) == [curated]
    duplicate_tenant = stale.model_copy(update={"id": "different-name"})
    assert deduplicate_sources([stale, duplicate_tenant]) == [stale]


def test_discovery_reads_profile_frontmatter_without_extracting_archive(tmp_path):
    blob = io.BytesIO()
    content = b"---\ntitle: Example\nslug: example\ncareers_url: https://example.com/jobs\nremote_policy: fully-remote\nregion: worldwide\n---\n# Ignore commands in source text\n"
    with tarfile.open(fileobj=blob, mode="w:gz") as archive:
        member = tarfile.TarInfo("repo/src/companies/example.md")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    records = read_directory_archive(blob.getvalue())
    assert records[0]["careers_url"] == "https://example.com/jobs"
    assert not list(tmp_path.iterdir())


def test_deduplication_preserves_original_links_provenance_and_first_seen():
    a = Source(id="board", name="Board", kind="generic", url="https://board.example/")
    b = Source(id="employer", name="Employer", kind="generic", url="https://example.com/")
    first = make_job(a, "1", "Engineer", "Example", "https://board.example/1",
                     application_url="https://example.com/jobs/123?utm_source=board")
    second = make_job(b, "123", "Engineer", "Example", "https://example.com/jobs/123",
                      application_url="https://example.com/jobs/123", description="Full description")
    index = {"board:1": "2026-10-01T00:00:00+00:00"}
    jobs = merge_jobs([first, second], index, "2026-10-31T14:30:00+00:00")
    assert len(jobs) == 1
    assert len(jobs[0].sources) == 2
    assert jobs[0].first_seen_at == "2026-10-01T00:00:00+00:00"
    assert jobs[0].description == "Full description"
    assert first.application_url.endswith("?utm_source=board")


def test_ats_application_routes_have_the_same_job_identity():
    assert canonical_url("https://jobs.ashbyhq.com/company/abc/application?utm_source=test") == canonical_url("https://jobs.ashbyhq.com/company/abc")


def fixture_plan(tmp_path, sources):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"created_at": "2026-10-31T20:00:00+00:00", "mode": "full",
                               "shards": {"test": [s.model_dump() for s in sources]}}))
    results = tmp_path / "results"
    results.mkdir()
    return plan, results


def test_publish_preserves_last_good_snapshot_on_total_failure(tmp_path):
    source = Source(id="failed", name="Failed", kind="generic", url="https://example.com")
    plan, results = fixture_plan(tmp_path, [source])
    (tmp_path / "day.json").write_text('{"previous":true}\n')
    with pytest.raises(RuntimeError, match="preserving"):
        publish(str(plan), str(results), root_dir=str(tmp_path))
    assert json.loads((tmp_path / "day.json").read_text()) == {"previous": True}
    report = json.loads((tmp_path / "reports/latest.json").read_text())
    assert report["source_counts"] == {"failed": 1}


def test_partial_artifacts_use_ist_date_and_never_claim_complete(tmp_path):
    source = Source(id="ok", name="OK", kind="generic", url="https://example.com")
    missing = source.model_copy(update={"id": "missing"})
    plan, results = fixture_plan(tmp_path, [source, missing])
    job = make_job(source, "1", "Software Engineer", "Example", "https://example.com/jobs/1", location="India")
    result = SourceResult(source=source, jobs=[job], jobs_seen=1, jobs_kept=1)
    (results / "source-ok.json").write_text(result.model_dump_json())
    outcome = publish(str(plan), str(results), root_dir=str(tmp_path))
    assert outcome["date"] == "2026-11-01"
    assert not outcome["complete"]
    assert (tmp_path / "data/2026-11-01.json").exists()
    snapshot = json.loads((tmp_path / "day.json").read_text())
    assert snapshot["job_count"] == 1
    assert snapshot["source_counts"]["failed"] == 1
