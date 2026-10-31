"""Isolate each JobSpy request so an unresponsive portal can be terminated cleanly."""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import sys


def main() -> None:
    from jobspy import scrape_jobs

    request = json.load(sys.stdin)
    kwargs = {"site_name": [request["site"]], "search_term": request["query"],
              "location": "India", "country_indeed": "India", "results_wanted": request.get("limit", 1000),
              "description_format": "plain", "fetch_description": True, "verbose": 1}
    signature = inspect.signature(scrape_jobs)
    if not any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in signature.parameters}
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        frame = scrape_jobs(**kwargs)
    records = json.loads(frame.to_json(orient="records", date_format="iso"))
    json.dump({"jobs": records, "errors": frame.attrs.get("errors", {}),
               "warnings": frame.attrs.get("warnings", {}), "log": captured.getvalue()[-2000:]}, sys.stdout)


if __name__ == "__main__":
    main()
