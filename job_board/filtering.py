from __future__ import annotations

import re
from datetime import UTC, datetime

from .models import Job, plain

TECH = re.compile(
    r"\b(software|developer|programmer|sde[1-4]?|sdet|frontend|front.end|backend|back.end|"
    r"full.stack|devops|devsecops|sre|site reliability|platform engineer|cloud engineer|"
    r"data (scientist|engineer|analyst|architect)|analytics engineer|machine learning|"
    r"artificial intelligence|ai engineer|ml engineer|llm|android|ios|mobile engineer|"
    r"security engineer|cyber.?security|infosec|penetration test|qa engineer|quality (engineer|assurance)|"
    r"test(ing| automation| tooling)? engineer|database|postgres|kubernetes|linux|"
    r"systems? engineer|network engineer|infra engineer|infrastructure|solutions? (architect|engineer)|"
    r"technical (support|consultant|account)|consulting engineer|sales engineer|"
    r"engineering (manager|director|lead)|head of engineering|cto|"
    r"product (manager|designer|engineer)|ux|ui|design engineer|"
    r"staff engineer|principal engineer|founding engineer|apple engineer|"
    r"member of technical staff|ruby|python|java|golang|rust|typescript|react|vue|wordpress)\b",
    re.I,
)
NON_TECH = re.compile(
    r"\b(recruiter|recruitment|talent acquisition|civil engineer|mechanical engineer|"
    r"chemical engineer|electrical engineer|field sales|account executive|"
    r"sales manager|marketing manager|construction|piping|manufacturing)\b", re.I,
)
INDIA = re.compile(
    r"\b(india|bengaluru|bangalore|hyderabad|pune|mumbai|chennai|new delhi|delhi|"
    r"noida|gurugram|gurgaon|kolkata|ahmedabad|kochi|coimbatore|jaipur|"
    r"thiruvananthapuram|trivandrum|chandigarh|indore|surat|lucknow|bhubaneswar|ncr)\b", re.I,
)
WORLDWIDE = re.compile(r"\b(worldwide|world.wide|anywhere|global|all countries)\b", re.I)
OTHER_COUNTRY = re.compile(
    r"\b(united states|usa|u\.?s\.? only|us only|united kingdom|uk only|canada|"
    r"germany|france|australia|new zealand|singapore|japan|netherlands|europe|emea|"
    r"namer|north america|latin america|latam|san francisco|new york|london|berlin)\b", re.I,
)
TALENT_POOL = re.compile(
    r"talent (community|pool|network)|general applications?|expression[s]? of interest|"
    r"future (opportunities|consideration|openings)|register your interest|join our network|"
    r"spontaneous application|speculative application", re.I,
)
CLOSED = re.compile(
    r"this (job|position|role|vacancy) (is |has been )?(no longer (available|accepting)|closed|filled)|"
    r"applications? (are |is )?closed|no longer accepting applications|job has expired", re.I,
)


def workplace(value: str | None, location: str = "", description: str = "") -> str:
    explicit = plain(value).lower().replace("_", " ").replace("-", " ")
    if explicit in {"hybrid"}:
        return "hybrid"
    if explicit in {"onsite", "on site", "office"}:
        return "onsite"
    if re.search(r"\bhybrid\b|\b[2-5] (?:days? (?:a |per )?week (?:in (?:the )?office)|days? in (?:the )?office)\b", description, re.I):
        return "hybrid"
    if explicit in {"remote", "telecommute", "telecommuting"}:
        return "remote"
    text = f"{location} {description[:600]}"
    if re.search(r"\bhybrid\b", location, re.I):
        return "hybrid"
    if re.search(r"\b(remote|work from home|home.based|fully distributed)\b", text, re.I):
        return "remote"
    if re.search(r"\b(on.site|office.based)\b", location, re.I):
        return "onsite"
    return "unknown"


def expired(value: str | None, now: datetime | None = None) -> bool:
    if not value:
        return False
    try:
        # A date-only deadline includes the full local day; don't expire it at midnight.
        raw = value.strip()
        if len(raw) == 10:
            raw += "T23:59:59+05:30"
        deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return deadline < (now or datetime.now(UTC))
    except ValueError:
        return False


def classify(job: Job) -> tuple[bool, str]:
    if not job.title or not job.url:
        return False, "missing title or URL"
    if TALENT_POOL.search(job.title):
        job.status = "talent_pool"
        return False, "talent pool rather than a vacancy"
    if job.status != "listed" or expired(job.valid_through) or CLOSED.search(job.description[:2500]):
        job.status = "closed"
        return False, "closed or expired"
    if NON_TECH.search(job.title) or not TECH.search(job.title):
        return False, "outside technology role scope"

    job.workplace_type = workplace(job.workplace_type, job.location, job.description)
    structured_india = any(c.strip().lower() in {"in", "ind", "india"} for c in job.countries)
    if structured_india or INDIA.search(job.location):
        job.india_eligibility = "india"
        job.eligibility_evidence = f"Job location: {job.location}; countries: {', '.join(job.countries)}"
        job.remote_scope = "india" if job.workplace_type == "remote" else None
    elif job.workplace_type != "remote":
        return False, "no India location and not fully remote"
    else:
        location = job.location.strip()
        # A specific role's country restriction wins over generic work-from-anywhere benefits.
        foreign_codes = [c for c in job.countries if c.lower() not in {"", "in", "ind", "india"}]
        if (foreign_codes or OTHER_COUNTRY.search(location)) and not WORLDWIDE.search(location):
            job.india_eligibility = "excluded"
            return False, "remote position is restricted to other countries"
        country_requirement = re.search(
            r"(?:must (?:be |reside |live )|candidates (?:must be |based in )|"
            r"open (?:only )?to |(?:role|position) is (?:only )?(?:open|available) (?:to |in ))"
            r"[^.;\n]{0,180}", job.description, re.I,
        )
        if country_requirement and OTHER_COUNTRY.search(country_requirement.group()) and not INDIA.search(country_requirement.group()):
            job.india_eligibility = "excluded"
            job.eligibility_evidence = country_requirement.group()
            return False, "description explicitly restricts candidate geography"

        eligible_india = re.search(
            r"(?:eligible|hiring|hire|candidates|applicants|location|based|reside|work remotely)[^.!?\n]{0,200}\bindia\b",
            job.description, re.I,
        )
        anywhere = re.search(
            r"(?:work (?:from )?anywhere (?:in the world)?|hire (?:globally|worldwide)|"
            r"remote (?:opportunities )?(?:globally|worldwide)|no restrictions on location|"
            r"never place restrictions on location|"
            r"(?:looking for|seeking|recruiting|hiring)[^.;\n]{0,130}(?:engineers|developers|candidates|talent)[^.;\n]{0,70}worldwide)", job.description, re.I,
        )
        if eligible_india:
            job.india_eligibility = "india"
            job.eligibility_evidence = eligible_india.group()
            job.remote_scope = "india"
        elif WORLDWIDE.search(location) or (anywhere and location.lower().strip() not in {"apac", "asia", "remote, apac"}):
            job.india_eligibility = "worldwide"
            job.eligibility_evidence = f"Job location: {location}" if WORLDWIDE.search(location) else anywhere.group()
            job.remote_scope = "worldwide"
        else:
            return False, "remote India eligibility is unconfirmed"

    # Residence-timezone bands differ from merely requesting overlapping working hours.
    zone = re.search(
        r"(?:main location|based|located|reside|must be within)[^.;\n]{0,100}?"
        r"(?:UTC|GMT)\s*([+-]\d+(?:\.\d+)?)\s*(?:to|and|[-–])\s*(?:UTC|GMT)\s*([+-]\d+(?:\.\d+)?)",
        job.description, re.I,
    )
    if zone and not min(float(zone[1]), float(zone[2])) <= 5.5 <= max(float(zone[1]), float(zone[2])):
        job.india_eligibility = "excluded"
        job.eligibility_evidence = zone.group()
        return False, "India outside required residence timezone band"

    sentences = re.split(r"(?<=[.!?])\s+|\n", job.description)
    job.requirements = [s[:450] for s in sentences if re.search(
        r"\b(time.?zone|overlap|shift time|reside|relocat|travel|retreat|on.call|work authori)", s, re.I
    )][:12]
    return True, "eligible"
