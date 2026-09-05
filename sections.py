"""
Section knowledge on disk.

A section is a lasting workspace grown out of one finished pipeline. Everything
about it lives in one folder — the same `Let Jarvis Handle It/<Project>/` folder
the founding pipeline already wrote into — so nothing has to be moved or copied
when a pipeline becomes a section.

Knowledge is kept as markdown with YAML frontmatter and [[wikilinks]] rather
than the per-agent JSON blobs a single pipeline leaves behind. That is not an
Obsidian dependency: the folder is plain markdown that any agent can read
through the filesystem MCP server, and Obsidian is only a lens you can point at
it for backlinks and the graph. It never needs to be running.

This module deliberately knows nothing about Gemini. Callers that want written
prose pass in a `summarise` callable; without one, the summary is still
assembled from what is on disk.
"""
import json
import os
import re
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECTIONS_ROOT = os.path.join(BASE_DIR, "Let Jarvis Handle It")

# The living summary. Named in prose because you are meant to open and edit it.
SUMMARY_NAME = "What this section knows.md"
SECTION_NOTE = "Section.md"
KNOWLEDGE_DIR = "Knowledge"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str, fallback: str = "note") -> str:
    """A filename that is still readable as a wikilink target."""
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", " ", (text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or fallback


def section_dir(folder: str, *parts: str) -> str:
    """A path inside the section's folder, creating it on the way."""
    path = os.path.join(SECTIONS_ROOT, folder, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def knowledge_dir(folder: str) -> str:
    return section_dir(folder, KNOWLEDGE_DIR)


def summary_path(folder: str) -> str:
    return os.path.join(knowledge_dir(folder), SUMMARY_NAME)


def _frontmatter(fields: dict) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            if not value:
                continue
            lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        else:
            text = str(value)
            # Anything with a colon or a leading marker has to be quoted or the
            # frontmatter stops parsing at that line.
            if re.search(r"[:#\[\]{}]|^\s", text) or "\n" in text:
                text = '"' + text.replace('"', "'").replace("\n", " ") + '"'
            lines.append(f"{key}: {text}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The section note — what this section is, written by the user at creation
# ---------------------------------------------------------------------------

def write_section_note(section: dict, pipelines: list[dict] | None = None) -> str:
    """The section's front page: its brief, its pipelines, links to its knowledge."""
    folder = section["folder"]
    out = [
        _frontmatter({
            "type": "section",
            "name": section.get("name", folder),
            "section_id": section.get("id"),
            "created": section.get("created_at"),
            "updated": _now(),
        }),
        "",
        f"# {section.get('name') or folder}",
        "",
        "## What this section is about",
        "",
        (section.get("brief") or "_No brief written._").strip(),
        "",
        "## What it knows",
        "",
        f"See [[{SUMMARY_NAME[:-3]}]] — the living summary, updated after every pipeline.",
        "",
    ]

    if pipelines:
        out += ["## Pipelines", ""]
        for p in pipelines:
            label = p.get("task_summary") or p.get("task") or p.get("id")
            label = str(label).strip().splitlines()[0][:120]
            founding = " — founding" if p.get("id") == section.get("founding_plan_id") else ""
            out.append(f"- `{p.get('id')}` — {label} ({p.get('status', 'unknown')}){founding}")
        out.append("")

    path = os.path.join(section_dir(folder), SECTION_NOTE)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path


# ---------------------------------------------------------------------------
# Turning a finished pipeline's memory into readable, linkable notes
# ---------------------------------------------------------------------------

def _memory_entries(folder: str) -> list[dict]:
    """Read the per-agent JSON a pipeline leaves in memory/, flattened.

    The pipeline writes one file per agent per cycle, each a flat dict of prose
    keyed by topic. Nothing in there is named after what it is about, so the key
    becomes the topic and the filename becomes the attribution.
    """
    entries = []
    for bucket in ("high_value", "general"):
        bucket_dir = os.path.join(SECTIONS_ROOT, folder, "memory", bucket)
        if not os.path.isdir(bucket_dir):
            continue
        for name in sorted(os.listdir(bucket_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(bucket_dir, name), "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception as e:
                print(f"[Sections] Could not read {name}: {e}")
                continue
            if not isinstance(payload, dict):
                continue
            agent = name[:-5].replace("_", " ")
            for topic, body in payload.items():
                if not isinstance(body, str) or not body.strip():
                    continue
                entries.append({
                    "topic": topic.replace("_", " ").strip(),
                    "body": body.strip(),
                    "agent": agent,
                    "weight": bucket,
                })
    return entries


def harvest_pipeline_memory(folder: str) -> list[str]:
    """Write the pipeline's JSON memory out as one markdown note per topic.

    Notes are additive: a second pipeline covering the same topic appends to the
    existing note rather than replacing it, because a section accumulates.
    """
    entries = _memory_entries(folder)
    if not entries:
        return []

    by_topic: dict[str, list[dict]] = {}
    for entry in entries:
        by_topic.setdefault(entry["topic"] or "General", []).append(entry)

    written = []
    kdir = knowledge_dir(folder)
    for topic, items in by_topic.items():
        name = slugify(topic, "General")
        path = os.path.join(kdir, f"{name}.md")
        existing = ""
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = f.read()
            except Exception:
                existing = ""

        blocks = []
        for item in items:
            # Never write the same paragraph twice — pipelines repeat themselves.
            if item["body"] in existing:
                continue
            blocks.append(f"### {item['agent']}\n\n{item['body']}\n")
        if not blocks:
            continue

        if existing:
            body = existing.rstrip() + "\n\n" + "\n".join(blocks)
        else:
            head = _frontmatter({
                "type": "knowledge",
                "topic": topic,
                "section": folder,
                "created": _now(),
            })
            link = slugify(folder, "Section")
            body = f"{head}\n\n# {topic}\n\nPart of [[{link}]].\n\n" + "\n".join(blocks)

        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        written.append(path)
    return written


def knowledge_notes(folder: str) -> list[dict]:
    """Every topic note in the section, newest first, with a short preview."""
    kdir = os.path.join(SECTIONS_ROOT, folder, KNOWLEDGE_DIR)
    if not os.path.isdir(kdir):
        return []
    notes = []
    for name in os.listdir(kdir):
        if not name.endswith(".md") or name == SUMMARY_NAME:
            continue
        path = os.path.join(kdir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            stat = os.stat(path)
        except Exception:
            continue
        # Skip frontmatter and headings when building the preview.
        preview = re.sub(r"^---.*?---", "", text, flags=re.DOTALL)
        preview = re.sub(r"^#+ .*$", "", preview, flags=re.MULTILINE).strip()
        notes.append({
            "name": name[:-3],
            "path": path,
            "modified": stat.st_mtime,
            "preview": preview[:280],
        })
    return sorted(notes, key=lambda n: n["modified"], reverse=True)


# ---------------------------------------------------------------------------
# The living summary
# ---------------------------------------------------------------------------

def read_summary(folder: str) -> str:
    path = summary_path(folder)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[Sections] Could not read summary: {e}")
        return ""


def summary_body(folder: str) -> str:
    """The summary without the wrapper — what a prompt actually wants.

    Both the frontmatter and the file's own `# What this section knows` heading
    are written by write_summary(), so neither belongs in text that gets quoted
    under a heading of its own somewhere else.
    """
    text = re.sub(r"^---.*?---\s*", "", read_summary(folder), flags=re.DOTALL).strip()
    return re.sub(r"^#\s*What this section knows\s*\n+", "", text, flags=re.IGNORECASE).strip()


def write_summary(folder: str, text: str, section_name: str = "") -> str:
    """Save the living summary, keeping the frontmatter Jarvis manages.

    You are meant to edit this file by hand, so your text is written back
    verbatim; only the wrapper is regenerated.
    """
    body = re.sub(r"^---.*?---\s*", "", text or "", flags=re.DOTALL).strip()
    # The heading is part of the wrapper, so drop it if the caller kept it.
    body = re.sub(r"^#\s*What this section knows\s*\n+", "", body, flags=re.IGNORECASE)
    head = _frontmatter({
        "type": "section-summary",
        "section": section_name or folder,
        "updated": _now(),
    })
    path = summary_path(folder)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{head}\n\n# What this section knows\n\n{body}\n")
    return path


def refresh_summary(section: dict, summarise=None) -> str:
    """Rebuild the living summary from the section's knowledge.

    `summarise(brief, material, previous)` may return written prose. When it is
    absent or fails, the summary is still assembled from what is on disk — a
    section must never end up with no readable knowledge just because a model
    call did not come back.
    """
    folder = section["folder"]
    harvest_pipeline_memory(folder)
    notes = knowledge_notes(folder)
    previous = summary_body(folder)

    material = "\n\n".join(f"## {n['name']}\n{n['preview']}" for n in notes[:40])

    text = ""
    if summarise:
        try:
            text = summarise(section.get("brief", ""), material, previous) or ""
        except Exception as e:
            print(f"[Sections] Summary generation failed: {e}")
            text = ""

    if not text.strip():
        lines = []
        if notes:
            lines += ["## Topics covered", ""]
            lines += [f"- [[{n['name']}]]" for n in notes]
            lines.append("")
        if previous and not notes:
            lines.append(previous)
        text = "\n".join(lines).strip() or "_Nothing recorded yet._"

    write_summary(folder, text, section.get("name", ""))
    return text


# ---------------------------------------------------------------------------
# What a new pipeline inside the section starts out knowing
# ---------------------------------------------------------------------------

def knowledge_digest(section: dict, max_chars: int = 6000) -> str:
    """The section's standing knowledge, as text to fold into a pipeline brief.

    Both routes, as agreed: this digest is injected so agents begin already
    knowing the section, and the full material stays on disk for them to open
    when a step needs more than the digest carries.
    """
    folder = section["folder"]
    name = section.get("name") or folder
    out = [f"# Section context — {name}", ""]

    brief = (section.get("brief") or "").strip()
    if brief:
        out += ["## What this section is about", "", brief, ""]

    summary = summary_body(folder)
    if summary:
        out += ["## What this section already knows", "", summary, ""]

    notes = knowledge_notes(folder)
    if notes:
        out += ["## Knowledge notes available in full", ""]
        rel = os.path.join("Let Jarvis Handle It", folder, KNOWLEDGE_DIR)
        for n in notes[:40]:
            out.append(f"- `{os.path.join(rel, n['name'] + '.md')}`")
        out.append("")

    out += [
        "## How to use this",
        "",
        "This is standing knowledge from earlier work in this section. Treat it as "
        "already established — do not research it again from scratch. Where you need "
        "more than the summary carries, open the knowledge notes listed above; they "
        "are plain markdown on disk. Build on this rather than repeating it.",
        "",
    ]

    return "\n".join(out)[:max_chars]


def pipeline_material(folder: str, max_chars: int = 5000) -> str:
    """What the founding pipeline actually found, read straight off disk.

    Used by the clarification round in the "make this a section" window, which
    runs *before* the section exists. It must not call `harvest_pipeline_memory`
    or `refresh_summary`: those write into the folder, and nothing may be
    written until the user presses Create section. So this reads the same
    material and returns it as text, leaving the disk untouched.
    """
    out = []

    summary = summary_body(folder)
    if summary:
        out += ["## Summary already on disk", "", summary, ""]

    entries = _memory_entries(folder)
    if entries:
        out += ["## What the pipeline's agents recorded", ""]
        # High-value first: if the cap bites, it should bite on the filler.
        for entry in sorted(entries, key=lambda e: e["weight"] != "high_value"):
            out += [f"### {entry['topic']} (from {entry['agent']})", "", entry["body"], ""]

    notes = knowledge_notes(folder)
    if notes and not entries:
        # A folder that has been harvested before keeps its knowledge in the
        # notes rather than in memory/, so fall back to their previews.
        out += ["## Knowledge notes in the folder", ""]
        for n in notes[:20]:
            out += [f"### {n['name']}", "", n["preview"], ""]

    if not out:
        return ""
    return "\n".join(out)[:max_chars]
