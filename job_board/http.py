from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "JobBoardCollector/0.1 (+https://github.com/YellowFoxH4XOR/job-board)"


class BlockedError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, cache_dir: str = ".cache/http", pace: float = 0.6):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pace = pace
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(35, connect=15), follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html,application/xml;q=0.9,*/*;q=0.8"},
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12),
        )
        self.locks: dict[str, asyncio.Lock] = {}
        self.last_request: dict[str, float] = {}
        self.robots: dict[str, RobotFileParser] = {}
        self.robots_locks: dict[str, asyncio.Lock] = {}
        self.memory: dict[str, httpx.Response] = {}
        self.host_delays: dict[str, float] = {}
        self.renderer = None

    async def close(self) -> None:
        if self.renderer:
            await self.renderer.close()
        await self.client.aclose()

    async def render(self, url: str, *, selector: str | None = None,
                     next_selector: str | None = None, max_pages: int = 1) -> str:
        await self.check_robots(url)
        if self.renderer is None:
            from .browser import Renderer
            self.renderer = Renderer()
        return await self.renderer.render(url, selector=selector, next_selector=next_selector, max_pages=max_pages)

    async def request(self, url: str, *, method: str = "GET", data: dict | None = None,
                      html: bool = False, cache: bool = True) -> httpx.Response:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(f"Not an HTTP URL: {url}")
        if html:
            await self.check_robots(url)
        key = hashlib.sha256(f"{method}:{url}:{json.dumps(data, sort_keys=True)}".encode()).hexdigest()
        if cache and key in self.memory:
            return self.memory[key]
        cached_path = self.cache_dir / f"{key}.json"
        old = {}
        if cache and method == "GET" and cached_path.exists():
            try:
                old = json.loads(cached_path.read_text())
            except (ValueError, OSError):
                pass
        headers = {}
        if old.get("etag"):
            headers["If-None-Match"] = old["etag"]
        if old.get("last_modified"):
            headers["If-Modified-Since"] = old["last_modified"]
        # Never attach the repository credential to career sites or redirect targets.
        if parts.hostname == "api.github.com" and os.getenv("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
        lock = self.locks.setdefault(parts.netloc, asyncio.Lock())
        for attempt in range(3):
            async with lock:
                delay = self.host_delays.get(parts.netloc, self.pace) - (time.monotonic() - self.last_request.get(parts.netloc, 0))
                if delay > 0:
                    await asyncio.sleep(delay)
                self.last_request[parts.netloc] = time.monotonic()
                try:
                    response = await self.client.request(method, url, json=data, headers=headers)
                except httpx.TransportError:
                    if attempt == 2:
                        raise
                    response = None
            if response is None or response.status_code in {429, 500, 502, 503, 504}:
                if attempt < 2:
                    wait = response.headers.get("Retry-After", "") if response else ""
                    await asyncio.sleep(min(float(wait), 45) if wait.isdigit() else 2 ** (attempt + 1))
                    continue
            if response is None:
                raise RuntimeError(f"No response from {parts.netloc}")
            if response.status_code == 304 and old.get("body") is not None:
                response = httpx.Response(200, text=old["body"],
                                          headers={"content-type": old.get("content_type", "text/html")},
                                          request=response.request)
            if response.status_code in {401, 403, 429}:
                raise BlockedError(f"HTTP {response.status_code} from {parts.netloc}")
            response.raise_for_status()
            if html and any(s in response.text[:12000].lower() for s in (
                "verify you are human", "checking your browser", "enable javascript and cookies to continue",
                "just a moment...", "unusual traffic from your computer network",
            )):
                raise BlockedError(f"Bot verification page from {parts.netloc}")
            if cache:
                self.memory[key] = response
                if method == "GET" and len(response.content) < 3_000_000:
                    body = {"body": response.text, "etag": response.headers.get("etag"),
                            "last_modified": response.headers.get("last-modified"),
                            "content_type": response.headers.get("content-type")}
                    cached_path.write_text(json.dumps(body, ensure_ascii=False))
            return response
        raise RuntimeError(f"Retry budget exhausted for {parts.netloc}")

    async def json(self, url: str, **kwargs):
        return (await self.request(url, **kwargs)).json()

    async def text(self, url: str, **kwargs) -> str:
        return (await self.request(url, **kwargs)).text

    async def check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        lock = self.robots_locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self.robots:
                parser = RobotFileParser()
                try:
                    r = await self.request(f"{origin}/robots.txt", cache=False)
                    parser.parse(r.text.splitlines())
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        parser.parse(["User-agent: *", "Allow: /"])
                    else:
                        raise
                self.robots[origin] = parser
            parser = self.robots[origin]
        if not parser.can_fetch(USER_AGENT, url):
            raise BlockedError(f"robots.txt disallows {parts.path}")
        delay = parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*")
        if delay:
            self.host_delays[parts.netloc] = max(self.pace, min(delay, 30))
