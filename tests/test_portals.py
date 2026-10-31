from job_board.models import Source, SourceResult
from job_board.portals import collect_portal


def source():
    return Source(id="linkedin", name="LinkedIn", kind="jobspy", tenant="linkedin",
                  group="portal", url="https://in.linkedin.com/jobs/",
                  options={"queries": ["software engineer"], "batch_size": 2, "results_per_query": 6})


async def test_completed_batches_survive_a_later_timeout(monkeypatch):
    requests = []

    async def query(request, timeout):
        requests.append(request)
        if request["offset"]:
            raise TimeoutError
        return {"jobs": [{"id": str(i), "title": "Software Engineer", "company": "Example",
                           "location": "India", "job_url": f"https://example.com/jobs/{i}"} for i in range(2)]}

    monkeypatch.setattr("job_board.portals.query_batch", query)
    s = source()
    result = SourceResult(source=s)
    await collect_portal(s, result)
    assert len(result.jobs) == 2
    assert result.status == "partial"
    assert [r["offset"] for r in requests] == [0, 2]
    assert requests[0]["fetch_description"] is False
    assert any("earlier batches retained" in w for w in result.warnings)


async def test_blocked_portal_stops_without_hammering_remaining_queries(monkeypatch):
    calls = []

    async def query(request, timeout):
        calls.append(request)
        return {"jobs": [], "errors": {"linkedin": "response status code 403 (blocked)"}}

    monkeypatch.setattr("job_board.portals.query_batch", query)
    s = source()
    s.options["queries"] = ["software engineer", "data engineer"]
    result = SourceResult(source=s)
    await collect_portal(s, result)
    assert len(calls) == 1
    assert result.status == "blocked"


async def test_repeated_offset_results_do_not_loop_to_cap(monkeypatch):
    calls = []

    async def query(request, timeout):
        calls.append(request)
        return {"jobs": [{"id": str(i), "title": "Software Engineer", "company": "Example",
                           "location": "India", "job_url": f"https://example.com/jobs/{i}"} for i in range(2)]}

    monkeypatch.setattr("job_board.portals.query_batch", query)
    s = source()
    result = SourceResult(source=s)
    await collect_portal(s, result)
    assert len(calls) == 2
    assert len(result.jobs) == 2
