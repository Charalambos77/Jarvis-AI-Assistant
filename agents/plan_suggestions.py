"""
Suggestions — other ways to do the plan, and swapping them in.

Once research is finished and the Brain has planned who does the work, the plan
is split into named parts (for example "Find the competitors", "Write the
report", "Publish it"). Jarvis then writes down the other ways each part — and
the plan as a whole — could be done: the best way overall, the cheapest, and
the one that brings the best result, plus any other way worth knowing about.
That happens whether or not the owner said how to do it: when they did, their
way stays the plan and the others are there to compare.

The owner reads them on the Suggestions page and can swap one in for a single
part or for the whole plan, and undo the swap. Swaps are allowed until the
execution agents start; from then on the page only shows what was chosen.

Everything for one pipeline lives in one JSON file beside its master blueprint:
    Let Jarvis Handle It/<project>/Implementation plan/Final Plans/suggestions_<plan_id>.json
"""
import copy
import json
import os
import re
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The kinds of suggestion the owner asked for, in the order the page shows them.
KINDS = ("best", "cheapest", "best_result", "fastest", "other")
KIND_LABELS = {
    "best": "Best way",
    "cheapest": "Cheapest",
    "best_result": "Best result",
    "fastest": "Fastest",
    "other": "Another way",
}

# Guards against a runaway model answer, not a target.
MAX_PARTS = 12
MAX_SUGGESTIONS_PER_SCOPE = 5

_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def suggestions_path(plan_id: str, project_name: str = "Default Project") -> str:
    folder = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Final Plans")
    return os.path.join(folder, f"suggestions_{plan_id}.json")


def load(plan_id: str, project_name: str = "Default Project") -> dict | None:
    path = suggestions_path(plan_id, project_name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Suggestions] Could not read {path}: {e}")
        return None


def _save(state: dict) -> None:
    path = suggestions_path(state["plan_id"], state.get("project_name") or "Default Project")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Cleaning what the model returns
# ---------------------------------------------------------------------------

def _slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return s[:40] or fallback


def _text(value, limit: int = 1200) -> str:
    return str(value or "").strip()[:limit]


def _texts(value, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [_text(v, 300) for v in value if _text(v, 300)][:limit]


def _clean_agent(agent, taken: set) -> dict | None:
    """One execution agent in the pipeline's own shape, with an id no other agent uses."""
    if not isinstance(agent, dict) or not agent.get("brief"):
        return None
    role = _text(agent.get("role"), 120)
    base = _slug(agent.get("agent_id") or role, "agent")
    if "_exec_" not in base:
        base = f"{base}_exec_1"
    agent_id, n = base, 2
    while agent_id in taken:
        agent_id = f"{base}_{n}"
        n += 1
    taken.add(agent_id)
    spec = agent.get("output_spec") if isinstance(agent.get("output_spec"), dict) else {}
    tools = agent.get("tools_needed") if isinstance(agent.get("tools_needed"), list) else []
    return {**agent, "agent_id": agent_id, "role": role or agent_id,
            "brief": _text(agent.get("brief"), 4000),
            "tools_needed": [str(t) for t in tools if t], "output_spec": spec}


def _clean_part(part, index: int, taken: set, agents_by_id: dict | None = None) -> dict | None:
    """A named part of the plan and the agents who do it.

    A part from the first split names existing agents by id (`agent_ids`); a part
    inside a suggestion brings its own agents (`agents`).
    """
    if not isinstance(part, dict):
        return None
    agents = []
    if agents_by_id is not None:
        for aid in part.get("agent_ids") or []:
            agent = agents_by_id.pop(str(aid), None)
            if agent:
                agents.append(agent)
    else:
        for a in part.get("agents") or []:
            cleaned = _clean_agent(a, taken)
            if cleaned:
                agents.append(cleaned)
    name = _text(part.get("name"), 120) or f"Part {index}"
    return {
        "part_id": f"part_{index}",
        "name": name,
        "goal": _text(part.get("goal"), 600),
        "approach": _text(part.get("approach"), 1500),
        "estimate": _clean_estimate(part.get("estimate")),
        "agents": agents,
        "source": "jarvis",
    }


def _clean_estimate(value) -> dict:
    value = value if isinstance(value, dict) else {}
    return {
        "cost": _text(value.get("cost"), 200),
        "time": _text(value.get("time"), 200),
        "quality": _text(value.get("quality"), 300),
    }


def _clean_suggestion(raw, scope: str, number: int, taken_ids: set) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "other").strip().lower().replace(" ", "_")
    if kind not in KINDS:
        kind = "other"
    title = _text(raw.get("title"), 160)
    if not title:
        return None
    s = {
        "id": f"{'plan' if scope == 'plan' else scope}_s{number}",
        "scope": scope,
        "kind": kind,
        "kinds": [kind] + [k for k in (raw.get("also") or []) if k in KINDS and k != kind],
        "title": title,
        "summary": _text(raw.get("summary"), 800),
        "how": _text(raw.get("how"), 2000),
        "estimate": _clean_estimate(raw.get("estimate")),
        "pros": _texts(raw.get("pros")),
        "cons": _texts(raw.get("cons")),
        "services": _texts(raw.get("services"), 12),
        "status": "open",
    }
    if scope == "plan":
        parts = []
        for i, p in enumerate((raw.get("parts") or [])[:MAX_PARTS], start=1):
            part = _clean_part(p, i, taken_ids)
            if part and part["agents"]:
                parts.append(part)
        if not parts:
            return None
        s["parts"] = parts
    else:
        agents = [a for a in (_clean_agent(a, taken_ids) for a in raw.get("agents") or []) if a]
        if not agents:
            return None
        s["agents"] = agents
    return s


def _from_model(answer: dict, execution_agents: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Parts, suggestions and the verdict on the current plan, from the model's JSON."""
    agents_by_id = {a["agent_id"]: copy.deepcopy(a) for a in execution_agents if a.get("agent_id")}
    order = [a["agent_id"] for a in execution_agents if a.get("agent_id")]

    parts = []
    for i, p in enumerate((answer.get("parts") or [])[:MAX_PARTS], start=1):
        part = _clean_part(p, i, set(), agents_by_id)
        if part and part["agents"]:
            parts.append(part)
    # An agent the model forgot to place still has to run: it gets a part of its own.
    leftovers = [agents_by_id[aid] for aid in order if aid in agents_by_id]
    if leftovers:
        parts.append({
            "part_id": f"part_{len(parts) + 1}", "name": "Everything else",
            "goal": "Work the plan needs that did not fit a named part.",
            "approach": "", "estimate": _clean_estimate(None), "agents": leftovers, "source": "jarvis",
        })
    for i, p in enumerate(parts, start=1):
        p["part_id"] = f"part_{i}"

    # Suggested agents may not reuse an id the plan already has.
    taken = set(order)
    suggestions = []
    raw_by_scope = answer.get("suggestions") or {}
    if isinstance(raw_by_scope, list):
        grouped: dict = {}
        for s in raw_by_scope:
            if isinstance(s, dict):
                grouped.setdefault(str(s.get("scope") or "plan"), []).append(s)
        raw_by_scope = grouped

    # The model names parts by their number or name; map both onto part ids.
    def scope_for(key: str) -> str | None:
        key = str(key).strip()
        if key.lower() in ("plan", "whole_plan", "whole plan", "all"):
            return "plan"
        m = re.search(r"(\d+)", key)
        if m and 1 <= int(m.group(1)) <= len(parts):
            return parts[int(m.group(1)) - 1]["part_id"]
        for p in parts:
            if p["name"].lower() == key.lower():
                return p["part_id"]
        return None

    for key, items in raw_by_scope.items():
        scope = scope_for(key)
        if not scope or not isinstance(items, list):
            continue
        n = 1
        for raw in items[:MAX_SUGGESTIONS_PER_SCOPE]:
            s = _clean_suggestion(raw, scope, n, taken)
            if s:
                suggestions.append(s)
                n += 1

    current = answer.get("current_plan") if isinstance(answer.get("current_plan"), dict) else {}
    verdict = {
        "estimate": _clean_estimate(current.get("estimate")),
        "kinds": [k for k in (current.get("is") or []) if k in KINDS],
        "note": _text(current.get("note"), 800),
        "followed_owner": bool(current.get("followed_owner_instructions")),
    }
    return parts, suggestions, verdict


# ---------------------------------------------------------------------------
# Asking the model
# ---------------------------------------------------------------------------

SUGGESTIONS_PROMPT = """Research for this job is finished and the execution plan below is what Jarvis will do.
Split the plan into parts and tell the owner the other ways it could be done.

ORIGINAL TASK / OWNER'S BRIEF:
{task}

THE PLAN (execution agents, in order):
{agents}

WHAT THE RESEARCH FOUND (master blueprint):
{blueprint}

{connected}
RULES:
1. Split the plan into 1 to {max_parts} named parts in the order the work happens, each one clear outcome
   (e.g. "Find the competitors", "Write the report", "Publish it"). Every agent_id above goes in exactly one part.
   Give each part a short `approach` saying how the plan does it, and an `estimate` of cost, time and quality.
2. For EACH part, and for the WHOLE plan, suggest other ways to do it. Always consider:
   - "best": the way you would recommend overall, balancing cost, time and quality.
   - "cheapest": the lowest money cost (free tools, fewer paid calls, manual steps).
   - "best_result": the highest quality result, even if it costs more or takes longer.
   Add "fastest" or "other" only when it is a genuinely different way worth knowing.
   If the plan already IS one of these, do not repeat it: say so in `current_plan.is` and suggest the others.
   A suggestion that is the same way under a different name is not a suggestion; leave it out.
3. If the owner's brief says HOW to do something, the plan keeps their way. Still suggest the alternatives,
   compare them honestly, and set `followed_owner_instructions` to true.
4. Every suggestion is concrete enough to run: a part suggestion lists the execution agents that would replace
   that part's agents; a whole-plan suggestion lists its own parts, each with its agents. Agents use the same shape
   as above (agent_id ending in _exec_N, role, brief, tools_needed, output_spec). Keep what the owner asked for:
   every deliverable in the brief must still be produced.
5. Name the services each suggestion needs in `services` and say in `estimate.cost` what they cost (e.g. "free",
   "about $20 of API credits", "a $49/month plan"). Use what the research found; say "unknown" rather than invent a price.
   Calls that cost money still wait for the owner in the Control room, so never plan around avoiding approval.
6. `pros` and `cons` are short plain sentences a non-expert understands.

Return JSON only:
{{
  "parts": [{{"name": "...", "goal": "...", "approach": "...", "agent_ids": ["..."],
              "estimate": {{"cost": "...", "time": "...", "quality": "..."}}}}],
  "current_plan": {{"is": ["best"], "note": "why the plan is shaped this way",
                    "followed_owner_instructions": false,
                    "estimate": {{"cost": "...", "time": "...", "quality": "..."}}}},
  "suggestions": {{
    "plan": [{{"kind": "cheapest", "also": [], "title": "...", "summary": "...", "how": "...",
               "estimate": {{"cost": "...", "time": "...", "quality": "..."}},
               "pros": ["..."], "cons": ["..."], "services": ["..."],
               "parts": [{{"name": "...", "goal": "...", "approach": "...", "agents": [{{...agent...}}]}}]}}],
    "part_1": [{{"kind": "best_result", "also": [], "title": "...", "summary": "...", "how": "...",
                 "estimate": {{...}}, "pros": [], "cons": [], "services": [], "agents": [{{...agent...}}]}}]
  }}
}}"""


def _connected_note() -> str:
    try:
        from agents.tool_onboarding import connected_services_note
        return connected_services_note()
    except Exception:
        return ""


def _ask_model(prompt: str) -> dict:
    """One Gemini call returning JSON. Replaced in tests."""
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=(
                "You are Jarvis's planner. You split an execution plan into parts and compare other ways "
                "to do each part: the best, the cheapest and the best result. Output valid JSON only."
            ),
            response_mime_type="application/json",
        ),
    )
    return json.loads(response.text)


def build(plan_id: str, project_name: str, task: str, execution_agents: list[dict],
          master_blueprint: dict | None, event_logger=None) -> dict:
    """Split the plan into parts and write the suggestions for it. Never raises.

    If the model fails, the plan is still shown as one part so the page can show
    what will run, and the failure is recorded so the owner can ask again.
    """
    execution_agents = [a for a in (execution_agents or []) if isinstance(a, dict) and a.get("agent_id")]
    if event_logger:
        event_logger({"event_type": "narrative", "source": "Brain", "data": {
            "phase": "planning", "icon": "💡",
            "message": "Splitting the plan into parts and looking for cheaper and better ways to do each one..."}})

    prompt = SUGGESTIONS_PROMPT.format(
        task=_text(task, 8000),
        agents=json.dumps(execution_agents, indent=2, ensure_ascii=False),
        blueprint=json.dumps(master_blueprint or {}, indent=2, ensure_ascii=False)[:30000],
        connected=_connected_note(),
        max_parts=MAX_PARTS,
    )
    error = ""
    try:
        parts, suggestions, verdict = _from_model(_ask_model(prompt), execution_agents)
    except Exception as e:
        print(f"[Suggestions] Could not write suggestions for {plan_id}: {e}")
        error = str(e)[:300]
        parts, suggestions, verdict = [], [], {"estimate": _clean_estimate(None), "kinds": [], "note": "", "followed_owner": False}
    if not parts:
        parts = [{"part_id": "part_1", "name": "The whole plan", "goal": "", "approach": "",
                  "estimate": _clean_estimate(None), "agents": copy.deepcopy(execution_agents), "source": "jarvis"}]

    state = {
        "plan_id": plan_id,
        "project_name": project_name,
        "task": _text(task, 600),
        "brief": _text(task, 8000),
        "created_at": time.time(),
        "updated_at": time.time(),
        "status": "failed" if error else "ready",
        "error": error,
        "locked": False,
        "parts": parts,
        "original_parts": copy.deepcopy(parts),
        "current_plan": verdict,
        "suggestions": suggestions,
        "history": [],
    }
    with _LOCK:
        _save(state)
    if event_logger:
        event_logger({"event_type": "suggestions_ready", "source": "Brain", "data": {
            "plan_id": plan_id, "parts": len(parts), "suggestions": len(suggestions), "failed": bool(error)}})
        if suggestions:
            event_logger({"event_type": "narrative", "source": "Brain", "data": {
                "phase": "gate", "icon": "💡",
                "message": f"{len(suggestions)} other way(s) to do this plan are on the Suggestions page. "
                           "You can swap any of them in before approving the execution plan."}})
    return state


# ---------------------------------------------------------------------------
# Reading the plan back, and swapping suggestions in
# ---------------------------------------------------------------------------

def execution_agents(state: dict) -> list[dict]:
    """The plan's agents in part order: what the pipeline runs."""
    return [copy.deepcopy(a) for p in state.get("parts") or [] for a in p.get("agents") or []]


def _parts_snapshot(state: dict) -> list[dict]:
    return copy.deepcopy(state.get("parts") or [])


def apply(plan_id: str, project_name: str, suggestion_id: str) -> dict:
    """Swap a suggestion in: one part's agents, or the whole plan's parts."""
    with _LOCK:
        state = load(plan_id, project_name)
        if not state:
            return {"error": "This pipeline has no suggestions yet."}
        if state.get("locked"):
            return {"error": "The execution agents have already started, so the plan can't change now."}
        s = next((x for x in state.get("suggestions") or [] if x.get("id") == suggestion_id), None)
        if not s:
            return {"error": f"No suggestion '{suggestion_id}'."}
        if s.get("status") == "applied":
            return {"ok": True, "state": state}
        if s.get("status") == "outdated":
            return {"error": "That suggestion was for a part the plan no longer has."}

        state.setdefault("history", []).append({
            "at": time.time(), "suggestion_id": suggestion_id, "scope": s["scope"],
            "parts_before": _parts_snapshot(state),
            "statuses_before": {x["id"]: x.get("status") for x in state["suggestions"]},
        })

        if s["scope"] == "plan":
            state["parts"] = [dict(copy.deepcopy(p), source=f"suggestion:{suggestion_id}") for p in s["parts"]]
            for x in state["suggestions"]:
                if x["scope"] == "plan":
                    x["status"] = "applied" if x["id"] == suggestion_id else "open"
                else:
                    # Those parts are gone; their suggestions no longer fit.
                    x["status"] = "outdated"
        else:
            part = next((p for p in state["parts"] if p["part_id"] == s["scope"]), None)
            if not part:
                return {"error": "That part is no longer in the plan."}
            # Suggested agents were given ids no original agent uses, but another part
            # may have been swapped since, so check against what the plan has now.
            others = {a["agent_id"] for p in state["parts"] if p is not part for a in p["agents"]}
            agents = [_clean_agent(a, others) for a in s["agents"]]
            part["agents"] = [a for a in agents if a]
            part["approach"] = s.get("how") or s.get("summary") or part.get("approach", "")
            part["estimate"] = s.get("estimate") or part.get("estimate")
            part["source"] = f"suggestion:{suggestion_id}"
            for x in state["suggestions"]:
                if x["scope"] == s["scope"]:
                    x["status"] = "applied" if x["id"] == suggestion_id else "open"
            for x in state["suggestions"]:
                # A whole-plan swap applied earlier no longer describes the plan exactly.
                if x["scope"] == "plan" and x["status"] == "applied":
                    x["status"] = "open"

        state["updated_at"] = time.time()
        _save(state)
        return {"ok": True, "state": state}


def undo(plan_id: str, project_name: str) -> dict:
    """Put back what the plan was before the last swap."""
    with _LOCK:
        state = load(plan_id, project_name)
        if not state:
            return {"error": "This pipeline has no suggestions yet."}
        if state.get("locked"):
            return {"error": "The execution agents have already started, so the plan can't change now."}
        if not state.get("history"):
            return {"error": "Nothing to undo."}
        last = state["history"].pop()
        state["parts"] = last["parts_before"]
        for x in state["suggestions"]:
            x["status"] = last["statuses_before"].get(x["id"], x.get("status"))
        state["updated_at"] = time.time()
        _save(state)
        return {"ok": True, "state": state}


def reset(plan_id: str, project_name: str) -> dict:
    """Go back to Jarvis's own plan, dropping every swap."""
    with _LOCK:
        state = load(plan_id, project_name)
        if not state:
            return {"error": "This pipeline has no suggestions yet."}
        if state.get("locked"):
            return {"error": "The execution agents have already started, so the plan can't change now."}
        if not state.get("history"):
            return {"ok": True, "state": state}
        state["history"].append({
            "at": time.time(), "suggestion_id": "", "scope": "reset",
            "parts_before": _parts_snapshot(state),
            "statuses_before": {x["id"]: x.get("status") for x in state["suggestions"]},
        })
        state["parts"] = copy.deepcopy(state.get("original_parts") or state["parts"])
        for x in state["suggestions"]:
            x["status"] = "open"
        state["updated_at"] = time.time()
        _save(state)
        return {"ok": True, "state": state}


def mark_working(plan_id: str, project_name: str) -> None:
    """Show the page that new suggestions are being written."""
    with _LOCK:
        state = load(plan_id, project_name)
        if state and not state.get("locked"):
            state["status"] = "working"
            state["updated_at"] = time.time()
            _save(state)


def lock(plan_id: str, project_name: str) -> dict | None:
    """The plan is final: execution is about to start. Returns the state, or None."""
    with _LOCK:
        state = load(plan_id, project_name)
        if not state:
            return None
        state["locked"] = True
        state["updated_at"] = time.time()
        _save(state)
        return state


def summary(state: dict) -> dict:
    """What the list on the page and the nav count need, without every brief."""
    open_count = sum(1 for s in state.get("suggestions") or [] if s.get("status") == "open")
    return {
        "plan_id": state.get("plan_id"),
        "project_name": state.get("project_name"),
        "task": state.get("task"),
        "status": state.get("status"),
        "locked": bool(state.get("locked")),
        "parts": len(state.get("parts") or []),
        "suggestions": len(state.get("suggestions") or []),
        "open": open_count,
        "swapped": sum(1 for p in state.get("parts") or [] if str(p.get("source", "")).startswith("suggestion:")),
        "updated_at": state.get("updated_at"),
    }
