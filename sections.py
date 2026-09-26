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


# ---------------------------------------------------------------------------
# The crew — a section's standing agents
#
# A pipeline's constellation exists for the length of one run. A section is a
# lasting workspace, so it gets a lasting one: departments ("baby sections")
# holding named agents, drawn on the dashboard at rest and handed to the Brain
# as the starting roster whenever a new pipeline is started inside the section.
#
# Everything here is derived from work that actually happened. The founding
# pipeline's real agent plan is recovered off disk, and every agent carries the
# agent_ids it came from and the memory topics those agents genuinely recorded.
# Nothing is invented from the section's name.
# ---------------------------------------------------------------------------

CREW_FILE = "Crew.json"
CREW_NOTE = "The crew.md"

# A section is a whole project, and it gets as many parts and agents as the
# work needs. These are only guards against a runaway model reply, far above
# anything a real plan reaches; the constellation cycles its colours past 16.
MAX_DEPARTMENTS = 40
MAX_AGENTS_PER_DEPARTMENT = 25


def _norm(text: str) -> str:
    """Match roles and domains by wording rather than by punctuation."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def crew_path(folder: str) -> str:
    return os.path.join(section_dir(folder), CREW_FILE)


def empty_crew() -> dict:
    return {"version": 1, "updated": _now(), "departments": []}


def read_crew(folder: str) -> dict:
    path = crew_path(folder)
    if not os.path.exists(path):
        return empty_crew()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[Sections] Could not read the crew: {e}")
        return empty_crew()
    if not isinstance(data, dict) or not isinstance(data.get("departments"), list):
        return empty_crew()
    return data


def write_crew(folder: str, crew: dict, section_name: str = "") -> str:
    """Save the crew, and write the readable version next to the summary."""
    crew = dict(crew or empty_crew())
    crew["version"] = 1
    crew["updated"] = _now()
    path = crew_path(folder)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(crew, f, indent=2, ensure_ascii=False)
    try:
        _write_crew_note(folder, crew, section_name)
    except Exception as e:
        # The note is for reading; never let it stop the crew being saved.
        print(f"[Sections] Could not write the crew note: {e}")
    return path


def _write_crew_note(folder: str, crew: dict, section_name: str = "") -> str:
    """The crew as markdown, so it reads like everything else in the folder."""
    out = [
        _frontmatter({
            "type": "section-crew",
            "section": section_name or folder,
            "updated": crew.get("updated"),
        }),
        "",
        "# The crew",
        "",
        "The standing agents of this section. Every pipeline started here begins "
        "from them. Edit the crew from the section dashboard — this file is "
        "written from `Crew.json` and is overwritten.",
        "",
    ]
    for dept in crew.get("departments", []):
        out += [f"## {dept.get('domain', 'Unnamed')}", ""]
        if dept.get("goal"):
            out += [dept["goal"], ""]
        for agent in dept.get("agents", []):
            lead = " — lead" if agent.get("is_lead") else ""
            out.append(f"- **{agent.get('role', 'Unnamed')}**{lead}: {agent.get('brief', '')}")
            if agent.get("why"):
                out.append(f"  - _{agent['why']}_")
        out.append("")
    path = os.path.join(knowledge_dir(folder), CREW_NOTE)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path


# ---------------------------------------------------------------------------
# What the crew is derived FROM — real work, read off disk
# ---------------------------------------------------------------------------

def agent_plans_on_disk(folder: str) -> list[dict]:
    """Every pipeline's real agent plan, recovered from the folder.

    `save_agent_plan_file` writes the whole plan as a fenced JSON block at the
    end of `Implementation plan/Agents/agent_plan_<plan_id>.md`, so the actual
    cycles, domains, goals, roles and briefs survive the process that made them.
    That is the only honest seed for a standing crew: agents that really ran.
    """
    plans_dir = os.path.join(SECTIONS_ROOT, folder, "Implementation plan", "Agents")
    if not os.path.isdir(plans_dir):
        return []
    found = []
    for name in sorted(os.listdir(plans_dir)):
        if not (name.startswith("agent_plan_") and name.endswith(".md")):
            continue
        try:
            with open(os.path.join(plans_dir, name), "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            print(f"[Sections] Could not read {name}: {e}")
            continue
        blocks = re.findall(r"```json\s*(.*?)```", text, flags=re.DOTALL)
        if not blocks:
            continue
        try:
            plan = json.loads(blocks[-1])
        except Exception as e:
            print(f"[Sections] {name} has no readable plan: {e}")
            continue
        if isinstance(plan, dict):
            found.append({"plan_id": name[len("agent_plan_"):-3], "agent_plan": plan})
    return found


def agent_evidence(folder: str) -> dict[str, list[str]]:
    """Which topics each agent actually recorded, keyed by its real agent_id.

    The pipeline writes one memory file per agent named `<agent_id>.json`, so
    this attribution is exact rather than guessed. It is what lets the crew say
    "this agent wrote that note" instead of "this agent sounds relevant".
    """
    evidence: dict[str, list[str]] = {}
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
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            topics = [str(k).replace("_", " ").strip() for k in payload.keys()]
            evidence.setdefault(name[:-5], []).extend(t for t in topics if t)
    return evidence


def _mechanical_why(agent: dict) -> str:
    """Provenance in one line, written from facts rather than by a model."""
    ids = agent.get("from_agent_ids") or []
    bits = []
    if ids:
        bits.append("Ran as " + ", ".join(f"`{i}`" for i in ids[:3]))
    ev = agent.get("evidence") or []
    if ev:
        bits.append("recorded " + ", ".join(ev[:3]))
    return ". ".join(bits) if bits else "Kept from the section's own pipelines."


def crew_from_agent_plans(folder: str, plans: list[dict] | None = None) -> dict:
    """Build a crew mechanically: one department per cycle, its real agents.

    This is the floor. It runs with no model at all, so a section always ends up
    with a crew that reflects what its pipelines actually did — a dead model
    call costs the merging and the wording, never the constellation itself.

    Only exact role repeats are merged here. Deciding that two differently named
    roles are the same job is a judgement, and a judgement guessed by string
    similarity is exactly the kind of stupid result this must not produce.
    """
    plans = agent_plans_on_disk(folder) if plans is None else plans
    evidence = agent_evidence(folder)
    departments: list[dict] = []
    by_domain: dict[str, dict] = {}
    seen_roles: dict[str, dict] = {}

    for entry in plans:
        plan_id = entry.get("plan_id", "")
        for cycle in (entry.get("agent_plan", {}).get("cycles") or []):
            domain = (cycle.get("domain") or "").strip()
            if not domain:
                continue
            key = _norm(domain)
            dept = by_domain.get(key)
            if not dept:
                if len(departments) >= MAX_DEPARTMENTS:
                    continue
                dept = {
                    "id": "dept_" + (slugify(domain, "dept").lower().replace(" ", "_") or "dept"),
                    "domain": domain,
                    "goal": (cycle.get("goal") or "").strip(),
                    "origin": "founding",
                    "from_plan_ids": [],
                    "agents": [],
                }
                by_domain[key] = dept
                departments.append(dept)
            if plan_id and plan_id not in dept["from_plan_ids"]:
                dept["from_plan_ids"].append(plan_id)

            lead = cycle.get("lead_specialist") or {}
            for cfg in [lead] + list(cycle.get("advisory_agents") or []):
                if not isinstance(cfg, dict):
                    continue
                role = (cfg.get("role") or "").strip()
                brief = (cfg.get("brief") or "").strip()
                # An agent with no brief is an agent with no job. Dropping it is
                # the difference between a crew and a list of job titles.
                if not role or not brief:
                    continue
                agent_id = cfg.get("agent_id") or ""
                rkey = _norm(role)
                standing = seen_roles.get(rkey)
                if standing:
                    # The same role across two cycles is one standing agent that
                    # worked twice, not two agents.
                    if agent_id and agent_id not in standing["from_agent_ids"]:
                        standing["from_agent_ids"].append(agent_id)
                        standing["evidence"] = sorted(
                            set(standing["evidence"]) | set(evidence.get(agent_id, []))
                        )
                        standing["why"] = _mechanical_why(standing)
                    if plan_id and plan_id not in standing["from_plan_ids"]:
                        standing["from_plan_ids"].append(plan_id)
                    continue
                if len(dept["agents"]) >= MAX_AGENTS_PER_DEPARTMENT:
                    continue
                agent = {
                    "role": role,
                    "brief": brief,
                    "is_lead": cfg is lead,
                    "tools_needed": [t for t in (cfg.get("tools_needed") or []) if t][:6],
                    "memory_query": (cfg.get("memory_query") or "").strip(),
                    "origin": "founding",
                    "from_agent_ids": [agent_id] if agent_id else [],
                    "from_plan_ids": [plan_id] if plan_id else [],
                    "evidence": sorted(set(evidence.get(agent_id, []))),
                    "why": "",
                }
                agent["why"] = _mechanical_why(agent)
                dept["agents"].append(agent)
                seen_roles[rkey] = agent

    return normalise_crew({"departments": departments})


# ---------------------------------------------------------------------------
# Keeping a crew sane, and growing it without losing hand edits
# ---------------------------------------------------------------------------

def normalise_crew(data, keep_ids: bool = True) -> dict:
    """Clean a crew from anywhere — a model, the dashboard, or disk.

    The rules here are what stop a bad crew reaching the constellation: no
    nameless departments, no department with nobody in it, no agent without a
    brief, no role repeated across the section, exactly one lead per
    department, and a cap that keeps every department a distinct colour.
    """
    departments = []
    seen_domains: set[str] = set()
    seen_roles: set[str] = set()
    kept_by_role: dict[str, dict] = {}
    holder = data if isinstance(data, dict) else {}
    source = holder.get("departments") if isinstance(data, dict) else data
    for raw in (source or []):
        if not isinstance(raw, dict):
            continue
        domain = str(raw.get("domain") or "").strip()
        if not domain or _norm(domain) in seen_domains:
            continue
        if len(departments) >= MAX_DEPARTMENTS:
            break

        agents = []
        for a in (raw.get("agents") or []):
            if not isinstance(a, dict):
                continue
            role = str(a.get("role") or "").strip()
            brief = str(a.get("brief") or "").strip()
            if not role or not brief:
                continue
            if _norm(role) in seen_roles:
                # The same job twice is one agent, but what it answers for is
                # kept, or folding the duplicate would silently drop an ask.
                standing = kept_by_role.get(_norm(role))
                if standing is not None:
                    for c in (a.get("covers") or []):
                        if c and str(c) not in standing["covers"]:
                            standing["covers"].append(str(c))
                continue
            if len(agents) >= MAX_AGENTS_PER_DEPARTMENT:
                break
            seen_roles.add(_norm(role))
            origin = a.get("origin") if a.get("origin") in ("founding", "merged", "brief") else "brief"
            agents.append({
                "role": role,
                "brief": brief,
                "is_lead": bool(a.get("is_lead")),
                "tools_needed": [str(t) for t in (a.get("tools_needed") or []) if t][:6],
                "memory_query": str(a.get("memory_query") or "").strip(),
                "origin": origin,
                "from_agent_ids": [str(i) for i in (a.get("from_agent_ids") or []) if i],
                "from_plan_ids": [str(i) for i in (a.get("from_plan_ids") or []) if i],
                "evidence": [str(e) for e in (a.get("evidence") or []) if e][:8],
                "why": str(a.get("why") or "").strip(),
                # Which of the section's asks this agent answers for. It is what
                # lets the plan prove nothing the user asked was forgotten.
                "covers": [str(c) for c in (a.get("covers") or []) if c],
            })
            kept_by_role[_norm(role)] = agents[-1]
        if not agents:
            # A department with nobody in it is a label, not a baby section.
            continue

        leads = [a for a in agents if a["is_lead"]]
        if not leads:
            agents[0]["is_lead"] = True
        elif len(leads) > 1:
            # Exactly one lead, as every cycle has: the rest are advisory.
            for extra in leads[1:]:
                extra["is_lead"] = False

        seen_domains.add(_norm(domain))
        dept_id = str(raw.get("id") or "").strip() if keep_ids else ""
        if not dept_id:
            dept_id = "dept_" + (slugify(domain, "dept").lower().replace(" ", "_") or "dept")
        origin = raw.get("origin") if raw.get("origin") in ("founding", "merged", "brief") else "brief"
        departments.append({
            "id": dept_id,
            "domain": domain,
            "goal": str(raw.get("goal") or "").strip(),
            "origin": origin,
            "from_plan_ids": [str(i) for i in (raw.get("from_plan_ids") or []) if i],
            "agents": agents,
        })

    return {
        "version": 1,
        "updated": _now(),
        "departments": departments,
        # What was deliberately taken out. Without this, re-reading the
        # pipelines would hand back every agent the user just dropped and every
        # duplicate role Jarvis merged away.
        "retired_roles": sorted({_norm(r) for r in (holder.get("retired_roles") or []) if r}),
        "retired_domains": sorted({_norm(d) for d in (holder.get("retired_domains") or []) if d}),
    }


def mark_retired(crew: dict, reference: dict) -> dict:
    """Record what this crew leaves out of `reference`, so it stays left out.

    `reference` is the mechanical crew — every department and agent the folder
    can account for. Anything in it that the user or Jarvis removed is retired
    by name, and a later merge will not quietly bring it back. Provenance an
    agent already claims counts as accounted for, which is what makes a
    deliberate merge of two duplicate roles survive a re-read.
    """
    crew = dict(crew or empty_crew())
    kept_roles = {_norm(a["role"]) for d in crew.get("departments", [])
                  for a in d.get("agents", [])}
    kept_domains = {_norm(d["domain"]) for d in crew.get("departments", [])}
    claimed_ids = {i for d in crew.get("departments", []) for a in d.get("agents", [])
                   for i in a.get("from_agent_ids", [])}

    retired_roles = set(crew.get("retired_roles") or [])
    retired_domains = set(crew.get("retired_domains") or [])
    for dept in (reference or {}).get("departments", []):
        if _norm(dept["domain"]) not in kept_domains:
            retired_domains.add(_norm(dept["domain"]))
        for agent in dept.get("agents", []):
            # A role folded into another agent is accounted for; a role simply
            # dropped was a decision. Either way it must not come back on its
            # own, so both are retired by name.
            rkey = _norm(agent["role"])
            if rkey not in kept_roles:
                retired_roles.add(rkey)

    crew["retired_roles"] = sorted(retired_roles)
    crew["retired_domains"] = sorted(retired_domains)
    return crew


def merge_crew(existing: dict, incoming: dict) -> dict:
    """Grow a crew without overwriting it.

    A section accumulates, and the crew is edited by hand, so a later pipeline
    may add departments and agents but may never rewrite or remove what is
    already standing. Only the provenance of an existing agent is extended,
    because that is new fact rather than new opinion — and anything retired
    stays out.
    """
    merged = normalise_crew(existing or empty_crew())
    by_domain = {_norm(d["domain"]): d for d in merged["departments"]}
    known_roles = {_norm(a["role"]) for d in merged["departments"] for a in d["agents"]}
    claimed_ids = {i for d in merged["departments"] for a in d["agents"]
                   for i in a.get("from_agent_ids", [])}
    retired_roles = set(merged.get("retired_roles") or [])
    retired_domains = set(merged.get("retired_domains") or [])

    def wanted(agent: dict) -> bool:
        rkey = _norm(agent["role"])
        if rkey in known_roles or rkey in retired_roles:
            return False
        # Already folded into a standing agent under another name.
        return not (set(agent.get("from_agent_ids") or []) & claimed_ids)

    for dept in normalise_crew(incoming or empty_crew())["departments"]:
        dkey = _norm(dept["domain"])
        target = by_domain.get(dkey)
        if not target:
            if dkey in retired_domains or len(merged["departments"]) >= MAX_DEPARTMENTS:
                continue
            dept["agents"] = [a for a in dept["agents"] if wanted(a)]
            if not dept["agents"]:
                continue
            if not any(a["is_lead"] for a in dept["agents"]):
                dept["agents"][0]["is_lead"] = True
            merged["departments"].append(dept)
            by_domain[dkey] = dept
            known_roles.update(_norm(a["role"]) for a in dept["agents"])
            continue

        for plan_id in dept.get("from_plan_ids", []):
            if plan_id not in target["from_plan_ids"]:
                target["from_plan_ids"].append(plan_id)

        by_role = {_norm(a["role"]): a for a in target["agents"]}
        for agent in dept["agents"]:
            standing = by_role.get(_norm(agent["role"]))
            if standing:
                for field in ("from_agent_ids", "from_plan_ids", "evidence", "covers"):
                    for value in agent.get(field, []):
                        if value not in standing[field]:
                            standing[field].append(value)
                continue
            if not wanted(agent) or len(target["agents"]) >= MAX_AGENTS_PER_DEPARTMENT:
                continue
            agent["is_lead"] = False       # the standing lead keeps the department
            target["agents"].append(agent)
            known_roles.add(_norm(agent["role"]))

    merged["retired_roles"] = sorted(retired_roles)
    merged["retired_domains"] = sorted(retired_domains)
    merged["updated"] = _now()
    return merged


# ---------------------------------------------------------------------------
# What the crew is shown, and what it hands on
# ---------------------------------------------------------------------------

def verify_crew_provenance(folder: str, crew: dict) -> dict:
    """Check every claim of provenance against the folder, and correct it.

    A model asked to organise real agents will sometimes attribute one to an
    `agent_id` that never existed, which is the most damaging kind of stupid
    result here: it makes an invented agent look like established work. So the
    claim is not trusted. Only ids that really appear in a plan on disk survive,
    the evidence is replaced by what that id genuinely recorded, and an agent
    left with nothing real behind it is demoted to what it actually is — a new
    agent that exists because of the brief.
    """
    real_ids: dict[str, str] = {}          # agent_id -> its real role
    for entry in agent_plans_on_disk(folder):
        for cycle in (entry.get("agent_plan", {}).get("cycles") or []):
            lead = cycle.get("lead_specialist") or {}
            for cfg in [lead] + list(cycle.get("advisory_agents") or []):
                if isinstance(cfg, dict) and cfg.get("agent_id"):
                    real_ids[str(cfg["agent_id"])] = (cfg.get("role") or "").strip()
    evidence = agent_evidence(folder)

    for dept in crew.get("departments", []):
        for agent in dept.get("agents", []):
            kept = [i for i in agent.get("from_agent_ids", []) if i in real_ids]
            agent["from_agent_ids"] = kept
            agent["evidence"] = sorted({t for i in kept for t in evidence.get(i, [])})[:8]
            if not kept:
                agent["origin"] = "brief"
                if not agent.get("why"):
                    agent["why"] = "New in this section — nothing on disk covered this work."
            else:
                # Two real ids behind one role is a genuine merge, not a claim.
                agent["origin"] = "merged" if len(kept) > 1 else "founding"
                agent["why"] = agent.get("why") or _mechanical_why(agent)
        dept_real = any(a["origin"] != "brief" for a in dept.get("agents", []))
        if not dept_real:
            dept["origin"] = "brief"
        elif dept.get("origin") not in ("founding", "merged"):
            dept["origin"] = "founding"
    return crew


def crew_material(folder: str, max_chars: int = 6000) -> str:
    """The real agents of this section's pipelines, as text for the model.

    Deliberately the raw record rather than a summary: the model's job is to
    organise agents that existed, not to imagine agents that would suit the
    name of the section.
    """
    plans = agent_plans_on_disk(folder)
    if not plans:
        return ""
    evidence = agent_evidence(folder)
    out = []
    for entry in plans:
        plan = entry.get("agent_plan", {})
        out.append(f"### Pipeline `{entry.get('plan_id')}` — {plan.get('task_summary', '')}")
        for cycle in (plan.get("cycles") or []):
            out.append(f"- Cycle {cycle.get('cycle_id')}: **{cycle.get('domain', '')}** — "
                       f"{cycle.get('goal', '')}")
            lead = cycle.get("lead_specialist") or {}
            for cfg in [lead] + list(cycle.get("advisory_agents") or []):
                if not isinstance(cfg, dict):
                    continue
                role = (cfg.get("role") or "").strip()
                if not role:
                    continue
                aid = cfg.get("agent_id", "")
                tag = "lead" if cfg is lead else "advisory"
                line = f"  - `{aid}` ({tag}) **{role}** — {(cfg.get('brief') or '').strip()}"
                topics = evidence.get(aid) or []
                line += f"  [recorded: {', '.join(topics[:4])}]" if topics else "  [recorded nothing]"
                out.append(line)
        out.append("")
    return "\n".join(out)[:max_chars]


def crew_seed_text(section: dict, crew: dict | None = None, max_chars: int = 4000) -> str:
    """The standing crew, as text a new pipeline's planning begins from.

    This is what makes the constellation mean something: the Brain is told who
    this section already has and what each of them owns, so a new pipeline
    adapts the section's crew instead of re-inventing a cast every run.
    """
    folder = section["folder"]
    crew = read_crew(folder) if crew is None else crew
    departments = crew.get("departments") or []
    if not departments:
        return ""

    name = section.get("name") or folder
    out = [f"## The standing crew of section — {name}", ""]
    for dept in departments:
        out.append(f"### {dept['domain']}")
        if dept.get("goal"):
            out.append(dept["goal"])
        for agent in dept["agents"]:
            tag = "lead" if agent.get("is_lead") else "advisory"
            out.append(f"- **{agent['role']}** ({tag}) — {agent['brief']}")
        out.append("")

    out += [
        "### How to use the crew",
        "",
        "These agents already exist in this section and their findings are on disk. "
        "Plan this pipeline around them: where a cycle you need is covered by a "
        "standing agent, reuse that agent's exact role name so its earlier work is "
        "credited to it, and add a new agent only where nothing standing covers the "
        "work. Never rename a standing agent, and never create a second agent doing "
        "the same job under a different name.",
        "",
    ]
    return "\n".join(out)[:max_chars]


def crew_counts(crew: dict | None) -> dict:
    departments = (crew or {}).get("departments") or []
    return {
        "departments": len(departments),
        "agents": sum(len(d.get("agents") or []) for d in departments),
    }


# ---------------------------------------------------------------------------
# The section plan — the whole project, not just the pipeline it grew from
#
# A section is where one pipeline turns into an entire operation: competitive
# research that is one part of building a company, or the first step of a
# research article. So a section is planned as a whole. Jarvis lists every ask,
# researches what a project of this kind needs to work end to end, decides
# whether the founding pipeline is the beginning or one part of it, and lays
# out every part with the agents it needs.
#
#   Section plan.json         ← the parts, the asks, the research
#   Crew.json                 ← the agents (each department is one part)
#   Knowledge/The plan.md     ← the same plan, readable
#   Knowledge/Section research.md
#
# The agents live only in the crew, so the dashboard's edits and the plan never
# disagree about who exists. A part and its department share one id.
# ---------------------------------------------------------------------------

PLAN_FILE = "Section plan.json"
PLAN_NOTE = "The plan.md"
RESEARCH_NOTE = "Section research.md"
PART_STATUSES = ("done", "in_progress", "todo")
POSITIONS = ("beginning", "part")


def plan_path(folder: str) -> str:
    return os.path.join(section_dir(folder), PLAN_FILE)


def empty_plan() -> dict:
    return {"version": 1, "updated": _now(), "understanding": {}, "position": {},
            "asks": [], "research": [], "parts": [], "services": []}


def part_id_for(title: str) -> str:
    """The id a part shares with its department in the crew."""
    return "dept_" + (slugify(title, "dept").lower().replace(" ", "_") or "dept")


def normalise_asks(raw) -> list[dict]:
    """Every ask once, with a stable id and the words it came from."""
    asks, seen = [], set()
    for item in (raw or []):
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text or _norm(text) in seen:
            continue
        seen.add(_norm(text))
        asks.append({
            "id": str(item.get("id") or "").strip() or f"ask_{len(asks) + 1}",
            "text": text,
            "quote": str(item.get("quote") or "").strip(),
            # "owner": the owner asked for it. "research": the research showed the
            # section needs it to be done fully and correctly. Both must be covered.
            "origin": "research" if item.get("origin") == "research" else "owner",
            "why": str(item.get("why") or "").strip(),
            # A requirement the owner chose not to add. Only research requirements
            # can be skipped: what the owner asked for is always planned.
            "skipped": bool(item.get("skipped")) and item.get("origin") == "research",
        })
    # Ids must be unique even when a model repeats one.
    used = set()
    for n, ask in enumerate(asks, 1):
        if ask["id"] in used:
            ask["id"] = f"ask_{n}"
            while ask["id"] in used:
                ask["id"] += "b"
        used.add(ask["id"])
    return asks


def asks_from_words(texts: list[str]) -> list[dict]:
    """The asks, read mechanically out of what the user wrote.

    Used when the model is unreachable: one ask per sentence or line of the
    user's own words, so nothing they wrote goes unplanned just because the
    model that would have phrased it better was down.
    """
    asks = []
    for text in texts or []:
        for piece in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
            piece = piece.strip(" -*\t")
            if len(piece.split()) >= 3:
                asks.append({"text": piece, "quote": piece})
    return normalise_asks(asks)


def resolve_covers(covers, asks: list[dict]) -> list[str]:
    """Turn what a model or user wrote in `covers` into ask ids.

    A model may cite an ask by its id or by its wording, so both are accepted;
    anything that names no real ask is dropped rather than counted as cover.
    """
    by_id = {a["id"]: a["id"] for a in asks}
    by_text = {_norm(a["text"]): a["id"] for a in asks}
    out = []
    for c in covers or []:
        c = str(c or "").strip()
        aid = by_id.get(c) or by_text.get(_norm(c))
        if aid and aid not in out:
            out.append(aid)
    return out


def split_plan(data: dict, asks: list[dict]) -> tuple[list[dict], dict]:
    """A model's parts-with-agents, split into plan parts and a crew.

    Returns (parts, crew). Every department id is the part's id, and every
    `covers` list is resolved to real ask ids on both sides.
    """
    raw_parts = (data or {}).get("parts") if isinstance(data, dict) else None
    departments, parts = [], []
    titles = {}
    for n, raw in enumerate(raw_parts or [], 1):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or raw.get("domain") or "").strip()
        if not title or _norm(title) in titles:
            continue
        pid = part_id_for(title)
        titles[_norm(title)] = pid
        agents = []
        for a in (raw.get("agents") or []):
            if isinstance(a, dict):
                a = dict(a)
                a["covers"] = resolve_covers(a.get("covers"), asks)
                agents.append(a)
        departments.append({"id": pid, "domain": title,
                            "goal": str(raw.get("goal") or "").strip(),
                            "origin": raw.get("origin") or "brief", "agents": agents})
        status = raw.get("status") if raw.get("status") in PART_STATUSES else "todo"
        try:
            order = int(raw.get("order") or n)
        except (TypeError, ValueError):
            order = n
        parts.append({
            "id": pid,
            "title": title,
            "goal": str(raw.get("goal") or "").strip(),
            "order": order,
            "status": status,
            "depends_on": [str(d) for d in (raw.get("depends_on") or []) if d],
            "deliverables": [str(d).strip() for d in (raw.get("deliverables") or []) if str(d).strip()],
            "covers": resolve_covers(raw.get("covers"), asks),
            "plan_ids": [str(i) for i in (raw.get("plan_ids") or []) if i],
        })
    # depends_on is written by title; keep it as part ids from here on.
    for part in parts:
        part["depends_on"] = [titles.get(_norm(d), d) for d in part["depends_on"]
                              if titles.get(_norm(d), d) != part["id"]]
    return parts, {"departments": departments}


def plan_coverage(plan: dict, crew: dict) -> dict:
    """Which agents answer for each ask, and which asks nobody answers for.

    An ask counts as covered only when a standing agent lists it: a part that
    mentions an ask without anyone to do the work has not covered it.
    """
    by_ask: dict[str, list[str]] = {a["id"]: [] for a in (plan or {}).get("asks", [])
                                     if not a.get("skipped")}
    for dept in (crew or {}).get("departments", []):
        for agent in dept.get("agents", []):
            for aid in agent.get("covers") or []:
                if aid in by_ask and agent["role"] not in by_ask[aid]:
                    by_ask[aid].append(agent["role"])
    return {"covered": by_ask, "uncovered": [aid for aid, roles in by_ask.items() if not roles]}


def close_coverage_gaps(plan: dict, crew: dict) -> tuple[dict, dict, list[str]]:
    """Give every ask still uncovered a part and an agent of its own.

    This is the last line of the rule that nothing the user asked for is
    forgotten. It runs after Jarvis has had his chances to plan the ask
    properly, so what it adds is plain and labelled for what it is — easy to
    spot, and easy to rename or fold into another part.
    """
    uncovered = plan_coverage(plan, crew)["uncovered"]
    if not uncovered:
        return plan, crew, []
    asks = {a["id"]: a for a in plan.get("asks", [])}
    roles = {_norm(a["role"]) for d in crew.get("departments", []) for a in d.get("agents", [])}
    order = max([p.get("order", 0) for p in plan.get("parts", [])] + [0])
    added = []
    for aid in uncovered:
        text = asks[aid]["text"]
        what = "requirement" if asks[aid].get("origin") == "research" else "ask"
        title = slugify(text, "Ask")[:60].strip()
        pid = part_id_for(title)
        role = f"{title} Owner"
        if _norm(role) in roles:
            role = f"{title} Owner ({aid})"
        roles.add(_norm(role))
        agent = {
            "role": role, "is_lead": True, "origin": "brief", "covers": [aid],
            "brief": f"Owns this {what} until it is planned properly: {text}",
            "why": f"Added so this {what} is not forgotten. Nothing else in the plan covered it.",
        }
        # A part already named like this ask takes the agent, because a second
        # department with the same name would be merged away, and the ask with it.
        same = next((d for d in crew.get("departments", [])
                     if _norm(d.get("domain")) == _norm(title) or d.get("id") == pid), None)
        if same:
            agent["is_lead"] = False
            same.setdefault("agents", []).append(agent)
            part = next((p for p in plan["parts"] if p["id"] == same.get("id")), None)
            if part is not None and aid not in part.setdefault("covers", []):
                part["covers"].append(aid)
            added.append(aid)
            continue
        if any(p["id"] == pid for p in plan["parts"]):
            pid = part_id_for(f"{title} {aid}")
        order += 1
        plan["parts"].append({
            "id": pid, "title": title, "goal": text, "order": order, "status": "todo",
            "depends_on": [], "deliverables": [], "covers": [aid], "plan_ids": [],
            "gap": True,
        })
        crew.setdefault("departments", []).append({
            "id": pid, "domain": title, "goal": text, "origin": "brief", "agents": [agent],
        })
        added.append(aid)
    return plan, crew, added


def normalise_plan(data, crew: dict | None = None) -> dict:
    """Clean a plan from anywhere, and keep it consistent with the crew.

    Parts whose department is gone from the crew (dropped on the dashboard)
    are dropped too, and parts are ordered the way they should run.
    """
    data = data if isinstance(data, dict) else {}
    plan = empty_plan()
    plan["understanding"] = {k: str(v).strip() for k, v in (data.get("understanding") or {}).items()
                             if isinstance(v, (str, int, float)) and str(v).strip()}
    pos = data.get("position") or {}
    plan["position"] = {
        "kind": pos.get("kind") if pos.get("kind") in POSITIONS else "beginning",
        "part_id": str(pos.get("part_id") or "").strip(),
        "why": str(pos.get("why") or "").strip(),
    }
    plan["asks"] = normalise_asks(data.get("asks"))
    plan["research"] = [
        {"question": str(r.get("question") or "").strip(),
         "summary": str(r.get("summary") or "").strip(),
         "sources": [s for s in (r.get("sources") or []) if isinstance(s, dict)][:8]}
        for r in (data.get("research") or []) if isinstance(r, dict) and r.get("question")
    ]
    plan["services"] = [
        {"name": str(s.get("name") or "").strip(), "why": str(s.get("why") or "").strip()}
        for s in (data.get("services") or []) if isinstance(s, dict) and s.get("name")
    ]
    dept_ids = None
    if crew is not None:
        dept_ids = {d["id"] for d in crew.get("departments", [])}
    parts, seen = [], set()
    for p in (data.get("parts") or []):
        if not isinstance(p, dict) or not p.get("id") or p["id"] in seen:
            continue
        if dept_ids is not None and p["id"] not in dept_ids:
            continue
        seen.add(p["id"])
        part = dict(p)
        part["status"] = p.get("status") if p.get("status") in PART_STATUSES else "todo"
        part.setdefault("order", len(parts) + 1)
        for key in ("depends_on", "deliverables", "covers", "plan_ids"):
            part[key] = [str(x) for x in (p.get(key) or []) if x]
        parts.append(part)
    # A department the user added on the dashboard is a part too.
    if crew is not None:
        for d in crew.get("departments", []):
            if d["id"] not in seen:
                # Work that really ran is done; anything else is still to do.
                ran = d.get("origin") in ("founding", "merged")
                parts.append({"id": d["id"], "title": d["domain"], "goal": d.get("goal", ""),
                              "order": len(parts) + 1, "status": "done" if ran else "todo",
                              "depends_on": [],
                              "deliverables": [], "covers": [], "plan_ids": []})
        titles = {d["id"]: d["domain"] for d in crew.get("departments", [])}
        for part in parts:
            part["title"] = titles.get(part["id"], part.get("title", ""))
    kept = {p["id"] for p in parts}
    for part in parts:
        part["depends_on"] = [d for d in part["depends_on"] if d in kept]
    parts.sort(key=lambda p: (p.get("order") or 0))
    for n, part in enumerate(parts, 1):
        part["order"] = n
    plan["parts"] = parts
    if plan["position"]["part_id"] not in kept:
        plan["position"]["part_id"] = next(
            (p["id"] for p in parts if p["status"] == "done"), "")
    return plan


def read_plan(folder: str) -> dict:
    path = os.path.join(SECTIONS_ROOT, folder, PLAN_FILE)
    if not os.path.exists(path):
        return empty_plan()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else empty_plan()
    except Exception as e:
        print(f"[Sections] Could not read the section plan: {e}")
        return empty_plan()


def has_plan(folder: str) -> bool:
    return bool(read_plan(folder).get("parts"))


def write_plan(folder: str, plan: dict, section_name: str = "", crew: dict | None = None) -> str:
    """Save the plan, with a readable copy and the research beside the knowledge."""
    crew = read_crew(folder) if crew is None else crew
    plan = normalise_plan(plan, crew)
    plan["updated"] = _now()
    path = plan_path(folder)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    try:
        _write_plan_note(folder, plan, crew, section_name)
        if plan.get("research"):
            _write_research_note(folder, plan, section_name)
    except Exception as e:
        print(f"[Sections] Could not write the plan notes: {e}")
    return path


def _write_plan_note(folder: str, plan: dict, crew: dict, section_name: str = "") -> str:
    agents_by_part = {d["id"]: d.get("agents", []) for d in crew.get("departments", [])}
    asks = {a["id"]: a for a in plan.get("asks", [])}
    pos = plan.get("position") or {}
    out = [
        _frontmatter({"type": "section-plan", "section": section_name or folder,
                      "updated": plan.get("updated")}),
        "",
        "# The plan",
        "",
        "Every part this section needs to work, in order, with the agents that do it. "
        "Written from `Section plan.json` and `Crew.json`; edit the plan from the section "
        "dashboard.",
        "",
    ]
    goal = (plan.get("understanding") or {}).get("goal")
    if goal:
        out += ["## Goal", "", goal, ""]
    if pos.get("why"):
        where = "the beginning of this section" if pos.get("kind") == "beginning" \
            else "one part of this section"
        out += ["## Where the founding pipeline fits", "", f"It is {where}. {pos['why']}", ""]
    out += ["## Parts", ""]
    for part in plan.get("parts", []):
        out.append(f"### {part['order']}. {part['title']} ({part['status'].replace('_', ' ')})")
        if part.get("goal"):
            out.append(part["goal"])
        for d in part.get("deliverables", []):
            out.append(f"- Deliverable: {d}")
        for agent in agents_by_part.get(part["id"], []):
            lead = " (lead)" if agent.get("is_lead") else ""
            out.append(f"- **{agent['role']}**{lead}: {agent.get('brief', '')}")
        out.append("")
    if asks:
        coverage = plan_coverage(plan, crew)["covered"]
        out += ["## Every ask and requirement, and who covers it", ""]
        for aid, ask in asks.items():
            if ask.get("skipped"):
                continue
            if ask.get("origin") == "research":
                ask = dict(ask, text="(needed) " + ask["text"])
            who = ", ".join(coverage.get(aid) or []) or "NOBODY YET"
            out.append(f"- {ask['text']} — {who}")
        out.append("")
    path = os.path.join(knowledge_dir(folder), PLAN_NOTE)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path


def _write_research_note(folder: str, plan: dict, section_name: str = "") -> str:
    out = [
        _frontmatter({"type": "section-research", "section": section_name or folder,
                      "updated": plan.get("updated")}),
        "",
        "# Section research",
        "",
        "What Jarvis researched to understand everything this section needs. "
        "Overwritten each time the section is re-planned.",
        "",
    ]
    for item in plan.get("research", []):
        out += [f"## {item['question']}", "", item.get("summary") or "_(no answer found)_", ""]
        for src in item.get("sources", []):
            title = src.get("title") or src.get("url") or "source"
            url = src.get("url") or src.get("uri") or ""
            out.append(f"- [{title}]({url})" if url else f"- {title}")
        out.append("")
    path = os.path.join(knowledge_dir(folder), RESEARCH_NOTE)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    return path


def set_part_pipeline(folder: str, part_id: str, plan_id: str, section_name: str = "") -> bool:
    """Record that a pipeline was started for one part of the plan."""
    plan = read_plan(folder)
    part = next((p for p in plan.get("parts", []) if p["id"] == part_id), None)
    if not part:
        return False
    if plan_id not in part.setdefault("plan_ids", []):
        part["plan_ids"].append(plan_id)
    if part.get("status") != "done":
        part["status"] = "in_progress"
    write_plan(folder, plan, section_name)
    return True


def sync_part_statuses(plan: dict, pipelines: list[dict]) -> dict:
    """A part is done when a pipeline run for it completed, and running while one runs."""
    status_of = {p["id"]: (p.get("status") or "") for p in pipelines or []}
    for part in plan.get("parts", []):
        states = [status_of.get(pid, "") for pid in part.get("plan_ids", [])]
        if any(s in ("complete", "completed", "done") for s in states):
            part["status"] = "done"
        elif any(s == "running" for s in states) and part.get("status") != "done":
            part["status"] = "in_progress"
    return plan


def next_parts(plan: dict) -> list[dict]:
    """The parts that can start now: not done, and everything they wait on is done."""
    done = {p["id"] for p in plan.get("parts", []) if p.get("status") == "done"}
    return [p for p in plan.get("parts", [])
            if p.get("status") == "todo" and all(d in done for d in p.get("depends_on", []))]


def plan_seed_text(section: dict, part_id: str | None = None, max_chars: int = 5000) -> str:
    """The whole plan, as the Brain sees it when a pipeline starts in the section.

    A pipeline run for one part must know it is one part of something larger:
    what came before it, what comes after, and exactly which asks it answers
    for, so it neither redoes a neighbouring part nor leaves its own asks out.
    """
    folder = section["folder"]
    plan = read_plan(folder)
    if not plan.get("parts"):
        return ""
    crew = read_crew(folder)
    agents_by_part = {d["id"]: d.get("agents", []) for d in crew.get("departments", [])}
    asks = {a["id"]: a["text"] for a in plan.get("asks", []) if not a.get("skipped")}
    name = section.get("name") or folder
    out = [f"## The plan of section — {name}", ""]
    goal = (plan.get("understanding") or {}).get("goal")
    if goal:
        out += [f"Goal of the whole section: {goal}", ""]
    this = next((p for p in plan["parts"] if p["id"] == part_id), None) if part_id else None
    if this:
        out += [f"### THIS PIPELINE IS PART {this['order']}: {this['title']}", this.get("goal", ""), ""]
        if this.get("deliverables"):
            out += ["It must deliver:"] + [f"- {d}" for d in this["deliverables"]] + [""]
        mine = sorted({aid for a in agents_by_part.get(this["id"], []) for aid in a.get("covers", [])}
                      | set(this.get("covers", [])))
        if mine:
            out += ["What this part answers for — the owner's asks and what the research showed "
                    "the section needs. Every one must be delivered:"]
            out += [f"- {asks[a]}" for a in mine if a in asks] + [""]
        roles = [a["role"] for a in agents_by_part.get(this["id"], [])]
        if roles:
            out += ["Its standing agents (reuse these exact role names): " + ", ".join(roles), ""]
    out.append("### All parts")
    for part in plan["parts"]:
        mark = " ← this pipeline" if this and part["id"] == this["id"] else ""
        out.append(f"{part['order']}. {part['title']} [{part['status'].replace('_', ' ')}]{mark}")
    out += ["", "Do not redo work a part marked done already produced; its findings are in "
            "the section's knowledge. Stay inside this pipeline's part.", ""]
    return "\n".join(out)[:max_chars]


def choose_needs(plan: dict, crew: dict, skipped_ids) -> tuple[dict, dict]:
    """Apply the owner's choice of which research requirements to add.

    A requirement left out is kept on the list, marked skipped, so it can be
    added back later. Agents that exist only for skipped requirements go, and
    so does a part left with nobody in it. A requirement added back that
    nobody covers any more gets a part of its own when the gaps are closed.
    """
    skipped = {str(i) for i in (skipped_ids or [])}
    for ask in plan.get("asks", []):
        if ask.get("origin") == "research":
            ask["skipped"] = ask["id"] in skipped
    gone = {a["id"] for a in plan.get("asks", []) if a.get("skipped")}
    for dept in crew.get("departments", []):
        dept["agents"] = [a for a in dept.get("agents", [])
                          if not (a.get("covers") and set(a["covers"]) <= gone)]
    crew["departments"] = [d for d in crew.get("departments", []) if d.get("agents")]
    return plan, crew
