# India + remote technology jobs

Daily, source-attributed JSON snapshots of technology jobs in India and fully remote jobs explicitly open to India or worldwide applicants.

[![Checks](https://github.com/YellowFoxH4XOR/job-board/actions/workflows/ci.yml/badge.svg)](https://github.com/YellowFoxH4XOR/job-board/actions/workflows/ci.yml)
[![Daily collection](https://github.com/YellowFoxH4XOR/job-board/actions/workflows/daily.yml/badge.svg)](https://github.com/YellowFoxH4XOR/job-board/actions/workflows/daily.yml)

## Outputs

- [`day.json`](day.json): latest successful nonempty snapshot.
- `data/YYYY-MM-DD.json`: daily snapshots, dated in **Asia/Kolkata**.
- [`reports/latest.md`](reports/latest.md): readable source coverage and failures.
- [`reports/latest.json`](reports/latest.json): machine-readable collection diagnostics.
- `registry/discovered.json`: employer-discovery candidates and resolved ATS sources.
- `state/first_seen.json`: stable first-observed timestamps, retained across missed runs.

The workflow starts at **20:00 IST / 14:30 UTC** daily. GitHub may delay scheduled starts; publication happens after collection completes. Run it manually from **Actions → Daily job collection → Run workflow → full**. Smoke mode generates a preview artifact without replacing published snapshots.

## What is collected

The seed registry includes Weekday, Instahyre, major portals (LinkedIn, Indeed, Naukri, Glassdoor), India/startup/specialist boards, remote feeds, and direct employers including **Railway, Supabase, Automattic, HighLevel, GitLab, Cal.com, Todoist, Tiger Data, Temporal, and CrowdStrike**.

- Documented public ATS adapters: Greenhouse, Lever, Ashby, SmartRecruiters, Workable, enabled Personio XML, and Teamtailor RSS.
- Public Workday career-site adapter; each tenant/site is tracked independently.
- Public feeds: Remotive, Remote OK, 4dayweek.io, Jobicy, Himalayas, Working Nomads, and RSS/Atom.
- HTML/JSON-LD discovery for career pages and public boards.
- Isolated JobSpy processes for the four major portals, with a technology-role query matrix and bounded 200-record batches. Completed batches survive later timeouts. Descriptions are retained as supplied by the search response; per-job detail fetching is off by default to avoid losing an entire query to slow detail requests.
- Automatic company discovery from the community-maintained [Remote In Tech](https://github.com/remoteintech/remote-jobs) profiles. Each full run refreshes directory metadata, recognizes direct ATS URLs, and checks up to 80 unresolved career sites (oldest checked first). Resolved sources join daily collection. Directory region/remote-policy labels are **hints, not proof of vacancy eligibility**.

Edit `config/sources.yaml` and `config/employers.yaml` to add or tune sources. `config/watchlist.yaml` records gated/paid/unresolved sources and aliases; these are not silently counted as operational adapters.

## Coverage is explicit

`complete` and per-source reports are part of the data contract. This is a maintained set of sources, **not an exhaustive census of every job on the internet**.

- `success`: the adapter finished its configured collection scope.
- `partial`: a feed window, search cap, crawl/time budget, missing page, or unverified catalog boundary limits coverage.
- `blocked`: a source denied access or its robots policy prevents collection.
- `unsupported`: a page did not expose structured/readable job records; a dedicated or rendering adapter is needed.
- `failed`: a transport/parser/runtime failure, or a missing shard artifact.

HTML discovery and keyword searches never claim exhaustive coverage. The collector does not bypass login, bot verification, or source access restrictions. Missing records from failed/partial sources are **not confirmed closures**. Partial runs publish observed records and report gaps. A total failure or zero matching results preserves the last good `day.json`; diagnostics remain available as workflow artifacts.

Known feed boundaries include Remotive's 24-hour delay, 4dayweek's up-to-48-hour delay, Jobicy's seven-day discovery window/three-hour delay, and RSS retention windows. Jobicy IDs are retained in the Actions cache and status-checked; cache loss means older unknown IDs cannot be recovered from its discovery feed.

## Filtering and record identity

India-based technology roles can be onsite, hybrid, or remote. International roles require fully remote work **and** explicit India/worldwide eligibility. Generic “remote” or “APAC” labels are insufficient. Specific country restrictions override generic remote-company benefits. Travel, timezone overlap, city requirements and shifts are retained when exposed by a source.

Job descriptions, original listing/application links, salary metadata, timestamps and source references are retained. Deduplication uses job-specific canonical application URLs, without rewriting the original attribution links. Talent pools and explicitly closed/expired vacancies are excluded. `status: listed` means advertised by the source at collection time, not independently employer-confirmed hiring.

## Run locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv can install it).

```bash
uv sync --extra portals
uv run ruff check .
uv run pytest -q
uv run job-board discover --mode smoke
uv run --extra portals job-board collect --shard feeds
```

`run/plan.json` contains all generated shard names. Collect each desired shard into a separate directory, then merge:

```bash
uv run --extra portals job-board collect --shard employer-0 --output run/results/employer-0
uv run job-board publish --root run/preview
```

For a full plan use `uv run job-board discover --mode full`. Optional `GITHUB_TOKEN` is sent only to `api.github.com`. Public feed collection requires no paid scraper service. API-key/subscription-only sources remain on the watchlist until deliberately configured.

## Operations

Collection is sharded across GitHub-hosted runners and committed once. Workflows cannot overlap. Source checkpoints are uploaded even when a shard fails. Conditional HTTP caches retain ETag/Last-Modified responses; source crawl delays are respected. Dependencies are locked in `uv.lock`, and third-party Actions are pinned to commit SHAs.

Use the Actions run summary and `reports/latest.json` to inspect source failures or truncation before interpreting counts. Public repository schedules can be disabled by GitHub after extended inactivity; re-enable the workflow from Actions if necessary. Daily data commits ordinarily keep this repository active.
