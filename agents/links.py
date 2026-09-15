"""Links in research are only ever ones a tool actually returned.

Pipeline 9's Cycle 1 lead cited nine links like
https://www.web_search_output.com/Singapore_national_AI_strategy_... web_search
handed agents a summary with no links in it, so they wrote their own. The old
check filed those under "unverified" and kept them: in the findings file, in
front of the lead, in the blueprint. Now a link no tool returned is taken out
wherever it appears, and the agent's confidence pays for it.
"""
import json
import re
from urllib.parse import urlsplit

URL_RE = re.compile(r"https?://[^\s)\]\"'<>]+")
_TRAILING = ".,;:!?"

# An agent that makes up links has made up something; its confidence can't stand.
INVENTED_LINK_CONFIDENCE_CAP = 0.6


def _key(url: str) -> tuple[str, str]:
    """(host without www, path without a trailing slash), so small spelling differences match."""
    try:
        parts = urlsplit(url.strip().rstrip(_TRAILING))
        host = (parts.hostname or "").lower()
    except ValueError:
        return "", ""
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    if parts.query:
        path += "?" + parts.query
    return host, path


class Evidence:
    """Every link, and every site named, in what the tools returned."""

    def __init__(self, texts=()):
        self._links: set[tuple[str, str]] = set()
        self.urls: list[str] = []
        self._text = ""
        self.add(texts)

    def add(self, texts) -> None:
        chunks = []
        for item in texts or []:
            text = item if isinstance(item, str) else json.dumps(item, default=str, ensure_ascii=False)
            chunks.append(text)
            for url in URL_RE.findall(text):
                url = url.rstrip(_TRAILING)
                key = _key(url)
                if key[0] and key not in self._links:
                    self._links.add(key)
                    self.urls.append(url)
        self._text += "\n" + "\n".join(chunks).lower()

    def has(self, url: str) -> bool:
        host, path = _key(url)
        if not host:
            return False
        if (host, path) in self._links:
            return True
        # A bare homepage is fine when the site itself came up: a search summary names
        # "Acme (acme.ie)" without writing the link. A page deeper in a site is not; its
        # exact address has to have been returned.
        if not path:
            pattern = r"(?<![a-z0-9.-])(?:www\.)?" + re.escape(host) + r"(?![a-z0-9-]|\.[a-z0-9])"
            return re.search(pattern, self._text) is not None
        return False


def strip_unevidenced_links(value, evidence: Evidence, skip_keys=("recommended_tools",)):
    """(value with every link no tool returned taken out, the links taken out).

    A string that was only a link becomes None (and is dropped from a list); a link
    inside a sentence is cut out of it. `skip_keys` are left alone: a recommended
    tool's documentation link is not a research claim.
    """
    removed: list[str] = []

    def clean(v):
        if isinstance(v, dict):
            return {k: (item if k in skip_keys else clean(item)) for k, item in v.items()}
        if isinstance(v, list):
            return [item for item in (clean(x) for x in v) if item is not None]
        if not isinstance(v, str):
            return v
        bad = [u.rstrip(_TRAILING) for u in URL_RE.findall(v)]
        bad = [u for u in bad if not evidence.has(u)]
        if not bad:
            return v
        removed.extend(bad)
        text = v
        for url in bad:
            text = text.replace(url, "")
        text = re.sub(r"\(\s*\)|\[\s*\]|<\s*>", "", text)
        text = re.sub(r"[ \t]{2,}", " ", text).strip(" \t-:,;")
        return text or None

    cleaned = clean(value)
    return cleaned, list(dict.fromkeys(removed))


def cap_for_invented_links(result: dict, links) -> dict:
    """Record the links taken out of a result, and cap its confidence."""
    links = list(dict.fromkeys(links or []))
    if not links:
        return result
    result["invented_links_removed"] = int(result.get("invented_links_removed") or 0) + len(links)
    try:
        confidence = float(result.get("confidence", INVENTED_LINK_CONFIDENCE_CAP))
    except (TypeError, ValueError):
        confidence = INVENTED_LINK_CONFIDENCE_CAP
    result["confidence"] = min(confidence, INVENTED_LINK_CONFIDENCE_CAP)
    note = f"Capped: it wrote {len(links)} link(s) that none of its tools returned; they were removed."
    result["confidence_note"] = " ".join(x for x in (result.get("confidence_note"), note) if x)
    return result
