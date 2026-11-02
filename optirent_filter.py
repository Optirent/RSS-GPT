"""Filters for OPTIRENT tender notifications from service.bund.de RSS feeds.

Conservative relevance filtering: reject clearly unrelated trade packages, while
keeping ambiguous notices for later manual review rather than losing bids.
"""
from datetime import datetime
from html import unescape
import re
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

BERLIN = ZoneInfo("Europe/Berlin")
DEADLINE_RE = re.compile(
    r"\b(?:Angebotsfrist|Teilnahmefrist|Bewerbungsfrist)\s*:\s*"
    r"(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})",
    re.IGNORECASE,
)
UNRELATED_TRADES = re.compile(
    r"(?:"
    r"\bsonnenschutzfolien?\b|"
    r"\bakustikdecken\b|"
    r"\bbodenbelagsarbeiten\b|"
    r"\b(?:sanitärtechnische[nr]?\s+)?tiefbauarbeiten\b|"
    r"\berd[-,\s]*kanal[-,\s]*und\s+betonarbeiten\b|"
    r"\bfertigteilfundamente\b|"
    r"\bvorbereitende[nr]?\s+maßnahmen\b|"
    r"\babbruch\s*/\s*schadstoffsanierung\b|"
    r"\b(?:lieferung|montage)\s+(?:und\s+montage\s+)?inventar\b|"
    r"\barchitektenleistung(?:en)?\b|"
    r"\bbaulogistik\b|"
    r"\ballgemeine\s+baustelleneinrichtung\b"
    r")",
    re.IGNORECASE,
)


def source_content(entry):
    """Return RSS description/content, including for entries read from old XML."""
    content = entry.get("content") or []
    if content:
        first = content[0]
        val = first.get("value") if hasattr(first, "get") else None
        if val:
            return val
    return entry.get("article") or entry.get("description") or ""


def text_content(entry):
    return unescape(BeautifulSoup(source_content(entry), "html.parser").get_text(" ", strip=True))


def offer_deadline(entry):
    """Read the bid/participation deadline; never confuse publication end with it."""
    match = DEADLINE_RE.search(text_content(entry))
    if not match:
        return None
    try:
        return datetime.strptime(
            match.group(1) + " " + match.group(2), "%d.%m.%Y %H:%M"
        ).replace(tzinfo=BERLIN)
    except ValueError:
        return None


def canonical_key(entry):
    """Normalize the same notice from subreport and subreport15 + tracking URLs."""
    link = (entry.get("link") or entry.get("id") or "").strip()
    if not link:
        return ""
    parts = urlsplit(link)
    path = re.sub(r"/subreport15/", "/subreport/", parts.path, flags=re.IGNORECASE)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def keep_entry(entry, now=None):
    """Return (accepted, reason) for an individual published announcement."""
    if now is None:
        now = datetime.now(BERLIN)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=BERLIN)
    else:
        now = now.astimezone(BERLIN)

    title = unescape(entry.get("title") or "").strip()
    deadline = offer_deadline(entry)
    if deadline is None:
        return False, "no parseable bid/participation deadline"
    if deadline <= now:
        return False, "bid/participation deadline expired"

    # Do not discard ambiguous headings like "Bauvorhaben Neubau Schule";
    # the matching keyword may be present only in the source announcement.
    if UNRELATED_TRADES.search(title):
        return False, "separately tendered ancillary trade"
    return True, "open tender"


def deduplicate_active(entries, now=None):
    """Filter and deduplicate both past RSS items and freshly fetched notices."""
    kept = []
    seen = set()
    for entry in entries:
        accepted, _ = keep_entry(entry, now=now)
        if not accepted:
            continue
        key = canonical_key(entry)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(entry)
    return kept
