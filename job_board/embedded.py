"""Read public hydration data as JSON. Never evaluate page-supplied JavaScript."""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup


def nodes(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from nodes(item)


def payloads(soup: BeautifulSoup):
    decoder = json.JSONDecoder()
    flight = []
    for script in soup.select("script"):
        text = script.string or script.get_text()
        if script.get("type") == "application/json" or script.get("id") == "__NEXT_DATA__":
            try:
                yield json.loads(text)
            except ValueError:
                pass
        for match in re.finditer(r"self\.__next_f\.push\(", text):
            try:
                packet, _ = decoder.raw_decode(text[match.end():])
                if isinstance(packet, list) and len(packet) > 1 and isinstance(packet[1], str):
                    flight.append(packet[1])
            except (ValueError, TypeError):
                continue
    for line in "".join(flight).splitlines():
        if not re.match(r"^[0-9a-f]+:", line):
            continue
        try:
            yield json.loads(line.partition(":")[2])
        except ValueError:
            continue
    for element in soup.select("job-list[v-bind]"):
        try:
            yield json.loads(element["v-bind"])
        except ValueError:
            continue


def homerun_vacancies(soup: BeautifulSoup) -> list[dict] | None:
    for element in soup.select("job-list[v-bind]"):
        try:
            data = json.loads(element["v-bind"])
            vacancies = data.get("content", {}).get("vacancies")
            if isinstance(vacancies, list):
                return vacancies
        except (ValueError, AttributeError):
            continue
    return None


def embedded_links(soup: BeautifulSoup) -> list[str]:
    """Only recognize observed job-record schemas, not arbitrary title-bearing objects."""
    found = []
    for payload in payloads(soup):
        for node in nodes(payload):
            if node.get("ashby_id") and node.get("title") and node.get("is_listed") is True:
                url = node.get("external_link")
                if isinstance(url, str):
                    found.append(re.sub(r"/application/?$", "/overview", url))
            if node.get("title") and node.get("url") and "location_id" in node and "department_id" in node:
                found.append(node["url"])
    return list(dict.fromkeys(found))
