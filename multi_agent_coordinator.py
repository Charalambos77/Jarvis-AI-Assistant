"""
Multi-Agent Coordinator — orchestrates the full pipeline:
Brain → Multi-Cycle Research Loops → Lead Reviews → Syntheses → Gates
→ Master Compilation → Execution Blueprint Gate → Execution → Quality Checks → Final Gate → Deploy
"""
import asyncio
import json
import os
from google import genai
from google.genai import types
import db
from agents.brain import build_agent_plan, plan_execution_agents, refresh_cycle_briefs
from agents.research_agent import run_research_agent
from agents.execution_agent import run_execution_agent
from agents.synthesis import (run_synthesis_agent, run_master_synthesis, MERGE_FACT_RULES,
                              DISAGREEMENT_RULES, DISAGREEMENT_SHAPE)
from agents.links import URL_RE, Evidence, strip_unevidenced_links, cap_for_invented_links
from agents.brief_changes import record_gate_change

# What a clarified brief means for the agents: it fixes WHAT the user wants, not
# HOW they are allowed to work. Kept here so every planning call says the same thing.
BRIEF_USAGE_RULE = (
    "The brief is authoritative for WHAT the user wants. It is not a limit on HOW you work: "
    "research and search the web freely for anything the brief does not cover. Follow the brief "
    "exactly where it constrains you \u2014 if it names a specific source, tool, or approach, use that "
    "one instead of choosing your own. Attached files live at the paths listed in the brief; open "
    "them when they are relevant."
)
from agents.quality_checker import run_quality_checker, check_research_against_brief
from agents.user_brief import user_brief_block
from agents.deployment_agent import run_deployment_agent

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "second_brain.db")


async def run_research_phase_for_cycle(
    agents: list[dict],
    db_conn,
    task_type: str,
    approved_blueprints: list[dict],
    event_logger=None,
    project_name: str = "Default Project",
    user_brief: str | None = None,
) -> dict:
    """Spawns all research agents for a cycle in parallel and returns their results."""
    if not agents:
        return {"status": "error", "message": "No agents defined for this research cycle."}

    prior_context = json.dumps(approved_blueprints, indent=2) if approved_blueprints else None

    # Load physical project memories from memory/ folder
    physical_memories = []
    mem_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "memory")
    for subdir in ("high_value", "general"):
        subdir_path = os.path.join(mem_dir, subdir)
        if os.path.exists(subdir_path):
            for filename in os.listdir(subdir_path):
                if filename.endswith(".json"):
                    try:
                        with open(os.path.join(subdir_path, filename), "r", encoding="utf-8") as f:
                            data = json.load(f)
                            physical_memories.append(f"[{subdir.upper()} MEMORY - {filename[:-5]}]: {json.dumps(data)}")
                    except Exception:
                        pass

    def on_chunk(agent_id, accumulated_text):
        if event_logger:
            event_logger({"event_type": "thinking_stream", "agent_id": agent_id, "data": accumulated_text})

    # For each agent, fetch memory context first
    tasks = []
    for agent_config in agents:
        agent_id = agent_config.get("agent_id")
        if event_logger:
            event_logger({"event_type": "spawned", "agent_id": agent_id, "data": agent_config})
            event_logger({"event_type": "running", "agent_id": agent_id})
        
        memory_query = agent_config.get("memory_query", "")
        memory_context = None
        if memory_query:
            if event_logger:
                event_logger({"event_type": "memory_query", "agent_id": agent_id, "data": memory_query})
            patterns = db.search_memory_patterns(db_conn, memory_query, task_type=task_type)
            if patterns:
                memory_context = "\n".join(
                    f"- [{p['outcome'].upper()}] {p['pattern']} "
                    f"(metric: {p.get('metric_name','?')} = {p.get('metric_value','?')})"
                    for p in patterns
                )
                if event_logger:
                    event_logger({
                        "event_type": "memory_recalled",
                        "agent_id": agent_id,
                        "data": {"patterns": [{"pattern": p["pattern"], "outcome": p["outcome"]} for p in patterns], "query": memory_query}
                    })

        # Combine database patterns with physical project memory files
        combined_mem = memory_context or ""
        if physical_memories:
            combined_mem = "\n\nPHYSICAL PROJECT MEMORY FILES:\n" + "\n".join(physical_memories) + "\n\n" + combined_mem
        
        tasks.append(run_research_agent(agent_config, combined_mem or None, prior_context, on_chunk_callback=on_chunk, event_logger=event_logger, project_name=project_name, user_brief=user_brief))

    print(f"[Multi-Agent] Spawning {len(tasks)} research agents in parallel...")
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for i, r in enumerate(raw_results):
        agent_id = agents[i].get("agent_id", f"agent_{i}")
        if isinstance(r, Exception):
            results.append({"agent_id": agent_id, "status": "error", "error": str(r), "findings": {}})
            if event_logger:
                event_logger({"event_type": "error", "agent_id": agent_id, "data": str(r)})
        else:
            results.append(r)
            if event_logger:
                event_logger({"event_type": "completed", "agent_id": agent_id, "data": r})
                # Emit findings as separate event for constellation leaf nodes
                if isinstance(r, dict) and r.get("findings"):
                    event_logger({
                        "event_type": "findings_discovered",
                        "agent_id": agent_id,
                        "data": r["findings"]
                    })

    return {
        "status": "ok",
        "agent_results": results,
        "agent_count": len(results)
    }


def pick_cycle(updated_cycles, cycle_id) -> dict | None:
    """The re-planned cycle, matched by its id.

    The Brain is asked to re-plan one cycle but sometimes returns the whole plan.
    Taking the first cycle then replaced cycle 2 with cycle 1's team. A lone cycle
    with a different id is still accepted, since that is the one that was asked for.
    """
    cycles = [c for c in (updated_cycles or []) if isinstance(c, dict)]
    match = next((c for c in cycles if str(c.get("cycle_id")) == str(cycle_id)), None)
    if match is None and len(cycles) == 1:
        match = cycles[0]
    return match


# Starts the note added to a brief when a conflict reruns its agent. A later conflict
# replaces the note instead of stacking another one under it.
CONFLICT_NOTE_MARKER = "\n\nYOUR LAST FINDINGS CONFLICTED WITH ANOTHER AGENT'S. "


def rebrief_for_conflict(cycle: dict, updated_cycle: dict | None, involved_ids, conflict_note) -> list[str]:
    """Re-brief only the agents a conflict involves, and return their ids.

    The team never changes: a conflict about customer reviews used to let the Brain
    re-plan the whole cycle, which dropped the ad and sales-process agents the user
    asked for and added agents that duplicated a later cycle. Agents the conflict
    names take their new brief from the Brain's re-plan when it has one for them,
    otherwise their old brief plus the conflict. If the conflict names no agent in
    this cycle, everyone runs again.
    """
    roster = [cycle["lead_specialist"]] + list(cycle.get("advisory_agents") or [])
    wanted = set(involved_ids or [])
    rerun = [a.get("agent_id") for a in roster if a.get("agent_id") in wanted]
    if not rerun:
        rerun = [a.get("agent_id") for a in roster]

    proposed = []
    if isinstance(updated_cycle, dict):
        proposed = [updated_cycle.get("lead_specialist")] + list(updated_cycle.get("advisory_agents") or [])
        proposed = [a for a in proposed if isinstance(a, dict)]

    note = str(conflict_note)[:1500]
    for agent in roster:
        if agent.get("agent_id") not in rerun:
            continue
        replacement = (
            next((a for a in proposed if a.get("agent_id") == agent.get("agent_id")), None)
            or next((a for a in proposed if a.get("role") and a.get("role") == agent.get("role")), None)
        )
        if replacement and replacement.get("brief"):
            agent["brief"] = replacement["brief"]
        else:
            # Build on the brief without any earlier conflict's note. The brief is saved
            # in the plan, so notes used to pile up there with every conflict.
            base = str(agent.get("brief", "")).split(CONFLICT_NOTE_MARKER, 1)[0]
            agent["brief"] = f"{base}{CONFLICT_NOTE_MARKER}Your earlier findings are kept, so only settle this: {note}"
    return rerun


def merge_with_previous(new: dict, old: dict | None) -> dict:
    """A rerun adds to what an agent found before; it never erases it.

    In pipeline 8 the lead reran only to settle one UK market figure, and its UK-only
    answer replaced the five-country comparison it had already done. Earlier findings
    stay, the rerun's findings are laid over them, and sources are combined. If the
    rerun itself failed, the earlier result stands and the failure is noted on it.
    """
    if not isinstance(new, dict) or not isinstance(old, dict) or old.get("status") != "ok":
        return new
    old_findings = old.get("findings") if isinstance(old.get("findings"), dict) else {}
    if not old_findings:
        return new
    if new.get("status") != "ok":
        kept = dict(old)
        kept["rerun_problem"] = new.get("blocked_reason") or new.get("error") or "The rerun gave no answer."
        return kept
    new_findings = new.get("findings") if isinstance(new.get("findings"), dict) else {}
    merged = dict(new)
    merged["findings"] = {**old_findings, **new_findings}
    for key in ("sources", "unverified_sources"):
        combined = []
        for s in list(old.get(key) or []) + list(new.get(key) or []):
            if s not in combined:
                combined.append(s)
        if combined:
            merged[key] = combined
    return merged


def keep_previous_file(file_path: str, label: str) -> str | None:
    """Move an existing file aside as `<name>_<label>N` with the first free N.

    Saves used to reuse a number, so a rerun after a restart replaced the attempt
    file from the run before it.
    """
    if not os.path.exists(file_path):
        return None
    root, ext = os.path.splitext(file_path)
    n = 1
    while os.path.exists(f"{root}_{label}{n}{ext}"):
        n += 1
    target = f"{root}_{label}{n}{ext}"
    os.replace(file_path, target)
    return target


def _agents_dir(project_name: str) -> str:
    return os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")


# Saved with each agent's research: the brief it was done on.
RESEARCHED_BRIEF_KEY = "researched_brief"


def without_invented_links(result: dict) -> dict:
    """Research saved before links were checked still carries the links its agent made up.

    Pipeline 9's Cycle 1 files do. Resuming must not hand those to the lead again.
    """
    evidence = Evidence([str(s) for s in result.get("sources") or []]
                        + [str(u) for u in result.get("evidence_links") or []])
    rest = {k: v for k, v in result.items() if k not in ("sources", "evidence_links", "recommended_tools")}
    cleaned, removed = strip_unevidenced_links(rest, evidence)
    result.update(cleaned)
    return cap_for_invented_links(result, removed)


def load_saved_cycle_research(plan_id: str, cycle: dict, project_name: str) -> dict:
    """Research this cycle's agents saved before a restart, still valid for the current plan.

    Resuming a cycle nobody approved used to run all of its research again. A saved
    result is reused only if it finished successfully and was done on the brief the
    agent has now. That used to be judged by the agent plan file's age, but the plan
    is saved for changes that leave most briefs alone — a conflict re-briefs only the
    agents involved — so a restart after a conflict re-ran agents whose findings still
    stood. Files saved before the brief was recorded fall back to that age check.
    """
    agents_dir = _agents_dir(project_name)
    plan_file = os.path.join(agents_dir, f"agent_plan_{plan_id}.md")
    plan_time = os.path.getmtime(plan_file) if os.path.exists(plan_file) else 0
    found = {}
    for agent in [cycle.get("lead_specialist")] + list(cycle.get("advisory_agents") or []):
        agent_id = (agent or {}).get("agent_id")
        path = os.path.join(agents_dir, f"research_{agent_id}_{plan_id}.md")
        if not agent_id or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = f.read().split("## Full JSON Payload\n```json\n", 1)[1].rsplit("\n```", 1)[0]
            result = json.loads(payload)
        except (IndexError, ValueError, OSError):
            continue
        if not isinstance(result, dict) or result.get("status") != "ok":
            continue
        if RESEARCHED_BRIEF_KEY in result:
            researched = str(result.pop(RESEARCHED_BRIEF_KEY) or "").strip()
            if researched != str(agent.get("brief") or "").strip():
                continue
        elif os.path.getmtime(path) < plan_time:
            continue
        found[agent_id] = without_invented_links(result)
    return found


def merge_review_sources(final: dict, lead_result: dict, advisory_results: list[dict],
                         extra_evidence=None) -> dict:
    """A lead's review cites only its agents' real sources, and only links their tools returned.

    `extra_evidence` is research the user already approved in earlier cycles: its
    links were checked when they were found.
    """
    known, evidence_texts = [], list(extra_evidence or [])
    for r in [lead_result] + list(advisory_results or []):
        if isinstance(r, dict):
            for s in r.get("sources") or []:
                if s not in known:
                    known.append(s)
            evidence_texts += [str(u) for u in r.get("evidence_links") or []]
    evidence = Evidence(known + evidence_texts)

    invented_links, unverified = [], []
    for s in final.get("sources") or []:
        if s in known:
            continue
        urls = URL_RE.findall(str(s))
        if urls and all(evidence.has(u) for u in urls):
            known.append(s)          # a real link one of its agents' tools returned
        elif urls:
            invented_links.append(s)
        else:
            unverified.append(s)
    final["sources"] = known
    if unverified:
        final["unverified_sources"] = list(final.get("unverified_sources") or []) + unverified

    # The findings, disagreements and notes too: a link none of its agents' tools returned
    # is taken out, however the review came to write it.
    rest = {k: v for k, v in final.items() if k not in ("sources", "recommended_tools")}
    cleaned, removed = strip_unevidenced_links(rest, evidence)
    final.update(cleaned)
    cap_for_invented_links(final, invented_links + removed)
    final["evidence_links"] = evidence.urls
    return final


async def run_lead_review(
    lead_config: dict,
    lead_result: dict,
    advisory_results: list[dict],
    approved_blueprints: list[dict],
    event_logger=None,                  # NEW
    user_brief: str | None = None,
) -> list[dict]:
    """
    Pass 2 — Lead Specialist LLM call reviewing advisory findings and merging them.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    lead_id = lead_config.get("agent_id", "lead")
    lead_role = lead_config.get("role", "Lead Specialist")
    lead_brief = lead_config.get("brief", "")

    prompt = f"""You are the Lead Specialist ({lead_role}, ID: {lead_id}) for this research cycle.
Your task is to review the research findings from your advisory agents and consolidate them into your final authoritative findings.

YOUR BRIEF:
{lead_brief}

{user_brief_block(user_brief)}
YOUR INITIAL FINDINGS:
{json.dumps(lead_result.get("findings", lead_result), indent=2)}

ADVISORY FINDINGS TO REVIEW:
{json.dumps(advisory_results, indent=2)}

APPROVED BLUEPRINTS FROM PRIOR CYCLES (for context):
{json.dumps(approved_blueprints, indent=2)}

Produce the cycle's final findings from ALL of the findings above, and aggregate any tool recommendations. Output a single JSON object.
You write the final answer, but your initial findings carry no more weight than any advisor's: being the lead decides who writes, not whose facts win.

RULES FOR MERGING:
{MERGE_FACT_RULES}
- If an advisor studied a different subject than you did (for example a different company), do not blend their findings into yours as if they were about your subject. Keep them apart and say so under "disagreements".

RULES FOR DISAGREEMENTS:
{DISAGREEMENT_RULES}
- Every candidate, item or recommendation an advisor backed that your final findings leave out goes under "not_adopted", with the evidence that ruled it out. Never drop one silently.

The output format must be JSON matching your original format:
{{
  "agent_id": "{lead_id}",
  "role": "{lead_role}",
  "confidence": 0.0-1.0,
  "findings": {{
    "key": "value",
    ...
  }},
  "sources": ["web_search: a query an agent ran", "https://a-link-an-agent's-tool-returned"],
  "disagreements": [{DISAGREEMENT_SHAPE}],
  "not_adopted": [{{"item": "what was left out", "proposed_by": "agent_id", "reason": "the evidence that ruled it out"}}],
  "recommendation": "one sentence action recommendation",
  "recommended_tools": [
    {{
      "service": "exact_service_name",
      "purpose": "why it is needed",
      "pros": ["pro1"],
      "cons": ["con1"],
      "why": "reason",
      "alternatives": ["alt1"]
    }}
  ]
}}
"""
    config = types.GenerateContentConfig(
        system_instruction=f"You are the {lead_role}. Output valid JSON only.",
        response_mime_type="application/json",
    )

    if event_logger:
        event_logger({
            "event_type": "thinking",
            "source": lead_id,
            "data": {
                "thinking_type": "system_prompt",
                "role": lead_role,
                "content": f"You are the {lead_role}. Output valid JSON only."
            }
        })
        event_logger({
            "event_type": "thinking",
            "source": lead_id,
            "data": {
                "thinking_type": "user_prompt",
                "role": lead_role,
                "content": prompt
            }
        })
        event_logger({
            "event_type": "narrative",
            "source": lead_id,
            "data": {
                "phase": "research",
                "message": f"Lead Specialist {lead_role} ({lead_id}) is reviewing advisor findings...",
                "icon": "⚖️"
            }
        })

    loop = asyncio.get_running_loop()

    # Emit prompt_sent
    if event_logger:
        event_logger({
            "event_type": "prompt_sent",
            "source": lead_id,
            "data": {
                "role": lead_role,
                "content": prompt
            }
        })

    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=config
            )
        )
        # Emit response_received
        if event_logger:
            event_logger({
                "event_type": "response_received",
                "source": lead_id,
                "data": {
                    "role": lead_role,
                    "content": response.text
                }
            })

        final_lead_result = json.loads(response.text)
        final_lead_result["agent_id"] = lead_id
        merge_review_sources(final_lead_result, lead_result, advisory_results,
                             extra_evidence=[approved_blueprints])
        return [final_lead_result]
    except Exception as e:
        print(f"[Lead Review] Error parsing Lead review output: {e}")
        # Fallback: just return lead_result + advisory_results as a list
        return [lead_result] + advisory_results


async def identify_rejected_agents(redirect_note: str, agent_plan: dict) -> list[str]:
    """
    Uses an LLM call to parse the human's redirect note and identify
    which specific agent IDs produced the rejected component.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    
    agent_list = json.dumps([
        {"agent_id": a["agent_id"], "role": a["role"], "brief": a["brief"]}
        for a in agent_plan.get("execution_agents", [])
    ], indent=2)

    prompt = f"""Given this human rejection note and list of execution agents,
    identify which agent IDs need to be re-run.

    REJECTION NOTE: {redirect_note}

    AGENTS:
    {agent_list}

    Return a JSON array of agent_id strings that need re-running.
    Example: ["agent_exec_1", "agent_exec_3"]"""

    config = types.GenerateContentConfig(response_mime_type="application/json")
    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash", 
                contents=prompt, 
                config=config
            )
        )
        result = json.loads(response.text)
        if isinstance(result, list):
            return result
        elif isinstance(result, dict) and "rejected_agent_ids" in result:
            return result["rejected_agent_ids"]
        return [a.get("agent_id") for a in agent_plan.get("execution_agents", [])]
    except Exception as e:
        print(f"[Identify Rejected Agents] Error identifying agents: {e}")
        # Default fallback: re-run all agents
        return [a.get("agent_id") for a in agent_plan.get("execution_agents", [])]


async def run_execution_phase(
    agent_plan: dict,
    blueprint: dict,
    gate_redirect_note: str | None = None,
    agent_ids_to_run: list[str] | None = None,
    event_logger=None,
    project_name: str = "Default Project",
    user_brief: str | None = None,
) -> dict:
    """Spawns all execution agents in parallel."""
    execution_agents = agent_plan.get("execution_agents", [])
    if not execution_agents:
        return {"status": "error", "message": "No execution agents defined."}

    # Filter execution agents if a subset is requested
    if agent_ids_to_run is not None:
        execution_agents = [cfg for cfg in execution_agents if cfg.get("agent_id") in agent_ids_to_run]
        if not execution_agents:
            return {"status": "ok", "agent_results": [], "agent_count": 0}

    # Trigger spawned and running events for active execution agents
    for cfg in execution_agents:
        agent_id = cfg.get("agent_id")
        if event_logger:
            event_logger({"event_type": "spawned", "agent_id": agent_id, "data": cfg})
            event_logger({"event_type": "running", "agent_id": agent_id})

    tasks = [
        run_execution_agent(cfg, blueprint, gate_redirect_note, event_logger=event_logger, project_name=project_name, user_brief=user_brief)
        for cfg in execution_agents
    ]

    # return_exceptions=True — handle per-agent failures gracefully
    print(f"[Multi-Agent] Spawning {len(tasks)} execution agents in parallel...")
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for i, r in enumerate(raw_results):
        agent_id = execution_agents[i].get("agent_id", f"exec_{i}")
        if isinstance(r, Exception):
            results.append({"agent_id": agent_id, "status": "error", "error": str(r)})
            if event_logger:
                event_logger({"event_type": "error", "agent_id": agent_id, "data": str(r)})
        else:
            results.append(r)
            if event_logger:
                event_logger({"event_type": "completed", "agent_id": agent_id, "data": r})

    return {
        "status": "ok",
        "agent_results": results,
        "agent_count": len(results)
    }


def save_agent_plan_file(plan_id: str, agent_plan: dict, project_name: str = "Default Project"):
    if not plan_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"agent_plan_{plan_id}.md")
    
    content = f"# Agent Spawn Plan - Plan ID: {plan_id}\n"
    content += f"**Task Summary:** {agent_plan.get('task_summary', 'N/A')}\n"
    content += f"**Task Type:** {agent_plan.get('task_type', 'N/A')}\n\n"
    content += "## Research Cycles\n"
    for cycle in agent_plan.get('cycles', []):
        content += f"### Cycle {cycle.get('cycle_id')}: {cycle.get('domain', 'N/A')}\n"
        content += f"- **Goal:** {cycle.get('goal', 'N/A')}\n"
        lead = cycle.get('lead_specialist', {})
        content += f"- **Lead Specialist:** {lead.get('role')} (ID: {lead.get('agent_id')})\n"
        content += f"  - Brief: {lead.get('brief')}\n"
        content += f"- **Advisory Agents:**\n"
        for adv in cycle.get('advisory_agents', []):
            content += f"  - {adv.get('role')} (ID: {adv.get('agent_id')}): {adv.get('brief')}\n"
        content += "\n"

    content += "## Execution Agents\n"
    for exec_a in agent_plan.get('execution_agents', []):
        content += f"### {exec_a.get('role')} (ID: {exec_a.get('agent_id')})\n"
        content += f"- **Brief:** {exec_a.get('brief')}\n"
        content += f"- **Required Keys:** {', '.join(exec_a.get('output_spec', {}).get('required_keys', []))}\n"
        content += f"- **Min Word Count:** {exec_a.get('output_spec', {}).get('min_word_count', 0)}\n\n"

    content += "## Full JSON Payload\n```json\n" + json.dumps(agent_plan, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def save_apis_mcps_file(plan_id: str, data: dict, project_name: str = "Default Project"):
    if not plan_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "memory")
    os.makedirs(dir_path, exist_ok=True)
    os.makedirs(os.path.join(dir_path, "high_value"), exist_ok=True)
    os.makedirs(os.path.join(dir_path, "general"), exist_ok=True)
    file_path = os.path.join(dir_path, "apis_mcps.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_apis_mcps_file(plan_id: str, project_name: str = "Default Project") -> dict:
    if not plan_id:
        return {"brain": [], "agents": []}
    file_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "memory", "apis_mcps.json")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = json.load(f)
                if isinstance(content, dict):
                    content.setdefault("brain", [])
                    content.setdefault("agents", [])
                    return content
                elif isinstance(content, list):
                    return {"brain": content, "agents": []}
        except Exception as e:
            print(f"[load_apis_mcps_file] Error reading {file_path}: {e}")
    return {"brain": [], "agents": []}

def save_research_findings_file(plan_id: str, agent_id: str, findings: dict, project_name: str = "Default Project",
                                attempt: int = 0, brief: str | None = None):
    if not plan_id or not agent_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"research_{agent_id}_{plan_id}.md")
    # A rerun — in this run or after a restart — keeps the agent's id, so it used to
    # overwrite what the agent found before. Keep every earlier attempt beside the new one.
    keep_previous_file(file_path, "attempt")

    content = f"# Research Findings - Agent ID: {agent_id} (Plan ID: {plan_id})\n"
    content += f"**Role:** {findings.get('role', 'Researcher')}\n"
    content += f"**Status:** {findings.get('status', 'ok')}\n"
    if findings.get("blocked_reason") or findings.get("error"):
        content += f"**Stopped because:** {findings.get('blocked_reason') or findings.get('error')}\n"
    content += f"**Confidence:** {findings.get('confidence', 'N/A')}\n\n"
    content += "## Findings Details\n"
    
    fds = findings.get("findings", {})
    if isinstance(fds, dict):
        for k, v in fds.items():
            content += f"### {k}\n{v}\n\n"
    else:
        content += f"{fds}\n\n"
        
    content += "## Recommendation\n"
    content += f"{findings.get('recommendation', 'N/A')}\n\n"
    
    content += "## Sources\n"
    for src in findings.get('sources', []):
        content += f"- {src}\n"
    content += "\n"

    if findings.get("unverified_sources"):
        content += "## Unverified sources (claimed, but no tool call this run produced them)\n"
        for src in findings["unverified_sources"]:
            content += f"- {src}\n"
        content += "\n"

    if findings.get("invented_links_removed"):
        content += ("## Links removed\n"
                    f"{findings['invented_links_removed']} link(s) the agent wrote were taken out: "
                    "none of its tools returned them.\n\n")

    # The brief this research was done on, so a restart can tell whether it still stands.
    saved = findings if brief is None else {**findings, RESEARCHED_BRIEF_KEY: brief}
    content += "## Full JSON Payload\n```json\n" + json.dumps(saved, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def save_cycle_blueprint_file(plan_id: str, cycle_id: int, blueprint: dict, project_name: str = "Default Project"):
    if not plan_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"cycle_blueprint_{cycle_id}_{plan_id}.md")
    
    content = f"# Cycle {cycle_id} Blueprint (Plan ID: {plan_id})\n\n"
    content += "## Synthesized Cycle Details\n"
    content += "```json\n" + json.dumps(blueprint, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def _approved_cycles_path(plan_id: str, project_name: str) -> str:
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
    return os.path.join(dir_path, f"approved_cycles_{plan_id}.json")


def load_approved_cycles(plan_id: str, project_name: str = "Default Project") -> list[int]:
    """Cycle ids the user actually approved at the gate.

    A cycle blueprint is written to disk *before* its gate opens, so the file
    existing proves only that the research was synthesized — not that anyone
    signed it off. This record is what says a cycle is done.
    """
    if not plan_id:
        return []
    try:
        with open(_approved_cycles_path(plan_id, project_name), "r", encoding="utf-8") as f:
            data = json.load(f)
        return sorted({int(c) for c in data.get("approved_cycle_ids", [])})
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[Pipeline] Error reading approved cycles record: {e}")
        return []


def mark_cycle_approved(plan_id: str, cycle_id: int, project_name: str = "Default Project"):
    if not plan_id:
        return
    try:
        approved = set(load_approved_cycles(plan_id, project_name))
        approved.add(int(cycle_id))
        path = _approved_cycles_path(plan_id, project_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"approved_cycle_ids": sorted(approved)}, f, indent=2)
    except Exception as e:
        print(f"[Pipeline] Error recording cycle {cycle_id} approval: {e}")


def plan_tool_requirements(agent_plan: dict) -> dict:
    """Every tool the agents in this plan actually declared they need.

    Walks `tools_needed` across research and execution agents — the only place
    an agent states a requirement — and resolves each name onto a real
    connector. Returns the same buckets as classify_requested_tools, plus
    `requested_by` so the gate can say which agent wants each service.
    """
    from agents.tool_executor import classify_requested_tools, _resolve_tool_key

    requests_by_agent: list[tuple[str, str, str]] = []   # (agent_id, role, tool)
    for cycle in (agent_plan.get("cycles") or []):
        agents = [cycle.get("lead_specialist")] + (cycle.get("advisory_agents") or [])
        for a in agents:
            if not a:
                continue
            for t in (a.get("tools_needed") or []):
                requests_by_agent.append((a.get("agent_id", "?"), a.get("role", ""), t))
    for a in (agent_plan.get("execution_agents") or []):
        for t in (a.get("tools_needed") or []):
            requests_by_agent.append((a.get("agent_id", "?"), a.get("role", ""), t))

    buckets = classify_requested_tools([t for _, _, t in requests_by_agent])

    requested_by: dict[str, list[str]] = {}
    for agent_id, role, tool in requests_by_agent:
        canonical = _resolve_tool_key(tool)
        if canonical in buckets["connectable"]:
            who = f"{role} ({agent_id})" if role else agent_id
            requested_by.setdefault(canonical, [])
            if who not in requested_by[canonical]:
                requested_by[canonical].append(who)

    buckets["requested_by"] = requested_by
    return buckets


def derive_completed_stages(plan: dict) -> set[str]:
    """Completed stages for a plan, falling back to the old phase-based rules.

    Pipelines started before stages were recorded have no `completed_stages`, so
    resume them exactly the way the previous code did rather than replaying work
    they had already finished.
    """
    recorded = plan.get("completed_stages")
    if recorded:
        return set(recorded)

    stages: set[str] = set()
    phase = plan.get("phase")
    status = plan.get("status")
    if phase in ("execution", "qa", "deploy", "complete"):
        stages.update({"execution_blueprint", "api_mcp_plugging"})
    if phase in ("qa", "deploy", "complete"):
        stages.add("execution")
    if status == "complete" or phase in ("deploy", "complete"):
        stages.add("final_qa")
    if status == "complete":
        stages.add("deploy")
    return stages


def save_master_blueprint_file(plan_id: str, master_blueprint: dict, project_name: str = "Default Project"):
    if not plan_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Final Plans")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"master_blueprint_{plan_id}.md")
    
    content = f"# Master Research Blueprint (Plan ID: {plan_id})\n\n"
    
    tool_recs = master_blueprint.get("tool_recommendations", [])
    if tool_recs:
        content += "## Tool Recommendations\n"
        for tool in tool_recs:
            content += f"### Tool: {tool.get('service', 'N/A')}\n"
            content += f"- **Purpose:** {tool.get('purpose', 'N/A')}\n"
            content += f"- **Consensus Strength:** {tool.get('agent_consensus', tool.get('consensus_strength', 'N/A'))}\n"
            content += f"- **Recommended By:** {', '.join(tool.get('recommended_by', []))}\n"
            content += f"- **Pros:** {', '.join(tool.get('pros', []))}\n"
            content += f"- **Cons:** {', '.join(tool.get('cons', []))}\n"
            content += f"- **Alternatives:** {', '.join(tool.get('alternatives', []))}\n\n"
            
    content += "## Compiled Blueprint Details\n"
    content += "```json\n" + json.dumps(master_blueprint, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def save_execution_output_file(plan_id: str, agent_id: str, output: dict, project_name: str = "Default Project"):
    if not plan_id or not agent_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"execution_{agent_id}_{plan_id}.md")
    
    content = f"# Execution Deliverable - Agent ID: {agent_id} (Plan ID: {plan_id})\n\n"
    
    for k, v in output.items():
        if k not in ("agent_id", "status"):
            content += f"## {k}\n{v}\n\n"
            
    content += "## Full JSON Payload\n```json\n" + json.dumps(output, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

def save_final_report_file(plan_id: str, report: dict, project_name: str = "Default Project"):
    if not plan_id:
        return
    dir_path = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Final Plans")
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"final_report_{plan_id}.md")
    
    content = f"# Final Pipeline Execution Report (Plan ID: {plan_id})\n\n"
    content += f"**Task:** {report.get('task', 'N/A')}\n\n"
    
    deploy = report.get("deploy_result", {})
    content += "## Deployment Status\n"
    content += f"- **Status:** {deploy.get('status', 'N/A')}\n"
    content += f"- **Message:** {deploy.get('message', 'N/A')}\n"
    if "url" in deploy:
        content += f"- **URL:** {deploy.get('url')}\n"
    content += "\n"
    
    content += "## Full JSON Payload\n```json\n" + json.dumps(report, indent=2) + "\n```\n"
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)

MAX_RETRIES = 3  # Configurable loop guard for all retry loops


async def run_full_pipeline(
    task: str,
    gate_approve_fn,        # async fn(gate_id: str, data: dict) -> {approved, redirect_note}
    event_logger=None,      # callable(event_dict) for observability — Step 11
    plan_id: str | None = None,
    project_name: str = "Default Project",
    force_reexecute: bool = False,   # NEW: when resuming a plan already past execution
                                      # (phase in qa/deploy/complete), re-run execution
                                      # agents fresh instead of replaying stale exec_results.
    brief_path: str | None = None,   # NEW: path to clarified_brief.md when this pipeline
                                      # came through the clarification gate. `task` already
                                      # carries the brief text; this is where the agents can
                                      # re-read it, and where the attached files are listed.
) -> dict:
    """
    Runs the complete multi-agent pipeline with ordered cycles.
    """
    # When the user clarified this job up front, the Brain plans against the brief
    # plus a standing rule about how much freedom the agents still have.
    planning_task = task
    if brief_path:
        planning_task = (
            f"{task}\n\n"
            f"[The full clarified brief for this job, including every attached file, is saved at:\n"
            f"{brief_path}\n\n"
            f"{BRIEF_USAGE_RULE}\n\n"
            f"When you write each agent's brief, carry over the details, decisions and file paths "
            f"that agent actually needs, and repeat this rule to them.]"
        )

    # What every agent is shown as the user's own words. `task` IS the clarified
    # brief when the pipeline came through the intake gate, so agents read it
    # verbatim instead of only the Brain's one-line paraphrase of their slice.
    user_brief = task

    conn = db.get_connection(DB_PATH)
    retry_history = []

    # Wrap the event_logger to inject plan_id automatically
    original_event_logger = event_logger
    def local_logger(event: dict):
        if original_event_logger:
            event["plan_id"] = plan_id
            original_event_logger(event)

    event_logger = local_logger

    try:
        # Check if we are resuming an existing plan
        existing_plan = None
        if plan_id:
            try:
                pipelines = db.get_pipelines(conn)
                for p in pipelines:
                    if p["id"] == plan_id:
                        existing_plan = p
                        break
            except Exception as e:
                print(f"[Pipeline] Error checking for existing plan: {e}")

        agent_plan = None
        approved_blueprints = []
        approved_cycle_ids: list[int] = []
        # Which stages this plan has genuinely finished. Everything else re-runs.
        completed_stages = derive_completed_stages(existing_plan) if existing_plan else set()

        if existing_plan:
            # Check if agent plan exists on disk and load it to support user edits
            plan_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Agents")
            plan_file = os.path.join(plan_dir, f"agent_plan_{plan_id}.md")
            if os.path.exists(plan_file):
                try:
                    with open(plan_file, "r", encoding="utf-8") as f:
                        file_content = f.read()
                    if "```json" in file_content:
                        parts = file_content.split("```json")
                        json_part = parts[-1].split("```")[0].strip()
                        agent_plan = json.loads(json_part)
                        print(f"[Pipeline] Successfully read and updated agent plan from disk: {plan_file}")
                except Exception as e:
                    print(f"[Pipeline] Error reading agent plan from disk: {e}")
                    agent_plan = existing_plan.get("agent_plan")
            else:
                agent_plan = existing_plan.get("agent_plan")
            
            approved_blueprints = existing_plan.get("approved_blueprints", [])
            approved_cycle_ids = load_approved_cycles(plan_id, project_name)

            # Fallback: if approved_blueprints is empty in DB, reconstruct it from cycle
            # blueprint files on disk — but only for cycles the user actually approved.
            # The blueprint file is written before the gate opens, so rebuilding from
            # every file on disk used to mark a cycle the user never signed off as done
            # and skip it on resume.
            if not approved_blueprints:
                if approved_cycle_ids:
                    reconstructable = approved_cycle_ids
                elif existing_plan.get("phase") in ("execution", "qa", "deploy", "complete"):
                    # Pre-dates the approval record, but the plan is past research, so
                    # every cycle blueprint on disk did clear its gate.
                    # No ceiling on cycles: the loop below stops at the first missing file.
                    reconstructable = list(range(1, 1000))
                else:
                    reconstructable = []

                for i in reconstructable:
                    cb_file = os.path.join(plan_dir, f"cycle_blueprint_{i}_{plan_id}.md")
                    if not os.path.exists(cb_file):
                        # Stop at the first gap so blueprints keep lining up with their
                        # cycle numbers (cycle N's blueprint must sit at index N-1).
                        break
                    try:
                        with open(cb_file, "r", encoding="utf-8") as f:
                            cb_content = f.read()
                        if "```json" in cb_content:
                            cb_json_part = cb_content.split("```json")[-1].split("```")[0].strip()
                            cb_data = json.loads(cb_json_part)
                            approved_blueprints.append(cb_data)
                            print(f"[Pipeline] Reconstructed approved blueprint for Cycle {i} from disk.")
                        else:
                            break
                    except Exception as e:
                        print(f"[Pipeline] Error reading cycle blueprint {i} from disk: {e}")
                        break

            # Plans that pre-date the approval record: adopt whatever the old rules
            # counted as approved, so this resume doesn't re-run cleared cycles and
            # later ones don't lose their sign-off.
            if approved_blueprints and not approved_cycle_ids:
                for i in range(1, len(approved_blueprints) + 1):
                    mark_cycle_approved(plan_id, i, project_name)
                approved_cycle_ids = load_approved_cycles(plan_id, project_name)

            print(f"[Pipeline] Resuming existing plan '{plan_id}'. Approved blueprints so far: {len(approved_blueprints)}")
            print(f"[Pipeline] Cycles signed off: {approved_cycle_ids or 'none'}. "
                  f"Stages already finished: {sorted(completed_stages) or 'none'}. Everything else re-runs.")

        if not agent_plan:
            # Phase 1: Brain builds cycle plan
            print("[Pipeline] Phase 1: Central Brain generating multi-cycle agent plan...")
            agent_plan = build_agent_plan(planning_task, event_logger=event_logger)
            if "error" in agent_plan:
                return agent_plan
            save_agent_plan_file(plan_id, agent_plan, project_name)
            init_tools = agent_plan.get("recommended_tools") or []
            for t in init_tools:
                t["recommended_by"] = ["Brain"]
            save_apis_mcps_file(plan_id, {"brain": init_tools, "agents": []}, project_name)
            if event_logger:
                event_logger({"event_type": "agent_plan_compiled", "source": "Brain", "data": agent_plan})
        else:
            mem_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "memory")
            os.makedirs(mem_dir, exist_ok=True)
            os.makedirs(os.path.join(mem_dir, "high_value"), exist_ok=True)
            os.makedirs(os.path.join(mem_dir, "general"), exist_ok=True)
            apis_mcps_file = os.path.join(mem_dir, "apis_mcps.json")
            if not os.path.exists(apis_mcps_file):
                init_tools = agent_plan.get("recommended_tools") or []
                for t in init_tools:
                    t["recommended_by"] = ["Brain"]
                save_apis_mcps_file(plan_id, {"brain": init_tools, "agents": []}, project_name)

        cycles = agent_plan.get("cycles", [])
        # As many cycles as the task needs — one is enough, and there is no ceiling.
        if not cycles:
            return {"error": "Brain planned no research cycles"}

        # Phase 2: Ordered research cycle loop
        for cycle in cycles:
            cycle_id = cycle["cycle_id"]
            domain = cycle.get("domain", f"Cycle {cycle_id}")

            cycle_index = cycle_id - 1
            # A blueprint sitting at this cycle's index isn't enough on its own: when
            # an approval record exists, the cycle also has to appear in it.
            cycle_signed_off = (cycle_id in approved_cycle_ids) if approved_cycle_ids else bool(existing_plan)
            if cycle_index < len(approved_blueprints) and cycle_signed_off:
                print(f"[Pipeline] Cycle {cycle_id} ({domain}) is already approved. Skipping research.")
                continue
            if cycle_index < len(approved_blueprints):
                # Approved-looking blueprint with no sign-off — drop it and re-run.
                approved_blueprints = approved_blueprints[:cycle_index]

            print(f"[Pipeline] Starting Cycle {cycle_id}: {domain}")
            
            # Emit narrative cycle start event
            if event_logger:
                event_logger({"event_type": "narrative", "data": {"phase": "research", "message": f"Starting Cycle {cycle_id}: {domain}...", "icon": "🔄"}})

            # Resuming a cycle nobody approved: reuse the research its agents saved before
            # the restart, and move its old blueprint aside so pages don't show a stale one
            # as this cycle's result while it runs.
            resumed_research = load_saved_cycle_research(plan_id, cycle, project_name) if existing_plan else {}
            keep_previous_file(os.path.join(_agents_dir(project_name), f"cycle_blueprint_{cycle_id}_{plan_id}.md"), "previous")
            if resumed_research and event_logger:
                event_logger({"event_type": "narrative", "data": {
                    "phase": "research", "icon": "♻️",
                    "message": f"Reusing research {len(resumed_research)} agent(s) saved before the restart for Cycle {cycle_id}.",
                }})

            # The flow: before a cycle starts, its briefs are rewritten from what the approved
            # cycles before it actually found, instead of the Brain's guesses from before any
            # research existed. Not when research is being reused: those briefs were refreshed
            # before that research ran, and rewriting them again would leave the reused
            # findings answering briefs the plan no longer shows.
            if approved_blueprints and not resumed_research:
                refresh_cycle_briefs(planning_task, cycle, approved_blueprints, event_logger=event_logger)
                save_agent_plan_file(plan_id, agent_plan, project_name)

            retry_count = 0
            # Findings kept from agents a conflict did not involve, so they don't run again.
            kept_results: dict = dict(resumed_research)
            # What rerun agents had found before, added back to what they find next.
            previous_results: dict = {}

            while retry_count < MAX_RETRIES:
                if event_logger:
                    source_str = f"Cycle {cycle_id}"
                    if retry_count > 0:
                        source_str += f" (Retry {retry_count})"
                    event_logger({"event_type": "running", "source": source_str})
                
                # 2a: Spawn Lead + Advisory agents in parallel (Pass 1)
                all_agents = [cycle["lead_specialist"]] + cycle.get("advisory_agents", [])
                agents_to_run = [a for a in all_agents if a.get("agent_id") not in kept_results]
                research_output = await run_research_phase_for_cycle(
                    agents_to_run, conn, agent_plan.get("task_type", "research"),
                    approved_blueprints=approved_blueprints, event_logger=event_logger,
                    project_name=project_name, user_brief=user_brief
                )
                new_results = [
                    merge_with_previous(r, previous_results.get(r.get("agent_id")))
                    for r in research_output.get("agent_results", [])
                ]
                previous_results = {}
                results_by_id = dict(kept_results)
                results_by_id.update({r.get("agent_id"): r for r in new_results})
                research_output["agent_results"] = [
                    results_by_id[a["agent_id"]] for a in all_agents if a.get("agent_id") in results_by_id
                ]
                
                # Save each research agent's findings and save structured memory
                briefs = {a.get("agent_id"): a.get("brief", "") for a in all_agents}
                for r in new_results:
                    # Partial and failed agents are saved too: what they found, and why they stopped.
                    save_research_findings_file(plan_id, r.get("agent_id"), r, project_name, attempt=retry_count,
                                                brief=briefs.get(r.get("agent_id")))
                    if r.get("status") == "ok":
                        
                        # Save high value memory
                        if "high_value_memory" in r and r["high_value_memory"]:
                            hv_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "memory", "high_value")
                            os.makedirs(hv_dir, exist_ok=True)
                            with open(os.path.join(hv_dir, f"{r['agent_id']}.json"), "w", encoding="utf-8") as f:
                                json.dump(r["high_value_memory"], f, indent=2, ensure_ascii=False)
                        
                        # Save general memory
                        if "general_memory" in r and r["general_memory"]:
                            gen_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "memory", "general")
                            os.makedirs(gen_dir, exist_ok=True)
                            with open(os.path.join(gen_dir, f"{r['agent_id']}.json"), "w", encoding="utf-8") as f:
                                json.dump(r["general_memory"], f, indent=2, ensure_ascii=False)

                # 2a: Lead Specialist review (Pass 2)
                lead_config = cycle["lead_specialist"]
                advisory_results = [r for r in research_output["agent_results"]
                                    if r["agent_id"] != lead_config["agent_id"]]
                lead_result = next(r for r in research_output["agent_results"]
                                   if r["agent_id"] == lead_config["agent_id"])

                authoritative_output = await run_lead_review(
                    lead_config, lead_result, advisory_results, approved_blueprints, event_logger=event_logger,
                    user_brief=user_brief
                )
                
                # Extract and merge tools from authoritative_output into apis_mcps.json
                new_tools = []
                for item in authoritative_output:
                    if isinstance(item, dict) and "recommended_tools" in item:
                        new_tools.extend(item["recommended_tools"])
                if new_tools:
                    existing_data = load_apis_mcps_file(plan_id, project_name)
                    merged_agents = {t["service"].lower(): t for t in existing_data["agents"] if "service" in t}
                    for t in new_tools:
                        service = t.get("service")
                        if service:
                            s_key = service.lower()
                            if "recommended_by" not in t or not t["recommended_by"]:
                                t["recommended_by"] = [f"cycle{cycle_id}"]
                            elif f"cycle{cycle_id}" not in t["recommended_by"]:
                                t["recommended_by"].append(f"cycle{cycle_id}")
                                
                            if s_key in merged_agents:
                                existing = merged_agents[s_key]
                                existing["recommended_by"] = list(set((existing.get("recommended_by") or []) + t["recommended_by"]))
                            else:
                                merged_agents[s_key] = t
                    existing_data["agents"] = list(merged_agents.values())
                    save_apis_mcps_file(plan_id, existing_data, project_name)

                # 2b: Synthesis (per-cycle)
                # Emit narrative synthesis event
                if event_logger:
                    event_logger({"event_type": "narrative", "data": {"phase": "synthesis", "message": f"Synthesizing Cycle {cycle_id} research into blueprint...", "icon": "🔬"}})

                # Every agent's own findings go in beside the lead's review, so what the review
                # dropped or overrode can be seen — not only what it chose to keep.
                synthesis_result = await run_synthesis_agent(
                    authoritative_output, event_logger=event_logger,
                    agent_results=research_output.get("agent_results", []),
                )

                if synthesis_result.get("has_conflicts"):
                    # Route conflicts to Brain for adjudication
                    conflict_note = json.dumps(synthesis_result["conflicts"])
                    print(f"[Pipeline] Conflict in Cycle {cycle_id}. Re-briefing Brain...")
                    retry_history.append(f"Cycle {cycle_id} conflict retry {retry_count + 1} due to findings contradictions.")
                    if event_logger:
                        event_logger({"event_type": "conflict", "source": "synthesis", "data": synthesis_result})
                    involved = [a for c in synthesis_result["conflicts"] for a in (c.get("agents_involved") or [])]
                    updated_cycle = None
                    try:
                        agent_plan_update = build_agent_plan(
                            planning_task,
                            redirect_note=(
                                f"Conflicts in cycle {cycle_id}: {conflict_note}\n\n"
                                "Keep this cycle's team exactly as it is: the same agent_ids and roles. "
                                "Only rewrite the briefs of the agents involved, so they can settle these conflicts. "
                                "Their earlier findings are kept, so each brief only needs to settle its conflict."
                            ),
                            cycle_id=cycle_id, approved_blueprints=approved_blueprints, event_logger=event_logger
                        )
                        updated_cycle = pick_cycle(agent_plan_update.get("cycles", []), cycle_id)
                    except Exception as e:
                        print(f"[Pipeline] Re-briefing for the conflict failed; rerunning with the conflict as a note: {e}")
                    rerun_ids = rebrief_for_conflict(cycle, updated_cycle, involved, conflict_note)
                    # Agents that failed or stopped short get another go too, rather than
                    # their empty result being kept as if it were finished work.
                    rerun_ids += [aid for aid, r in results_by_id.items()
                                  if r.get("status") != "ok" and aid not in rerun_ids]
                    previous_results = {aid: results_by_id[aid] for aid in rerun_ids if aid in results_by_id}
                    kept_results = {aid: r for aid, r in results_by_id.items() if aid not in rerun_ids}
                    save_agent_plan_file(plan_id, agent_plan, project_name)
                    if event_logger:
                        event_logger({"event_type": "narrative", "data": {
                            "phase": "research", "icon": "🔁",
                            "message": f"Cycle {cycle_id} conflict: re-checking with {', '.join(rerun_ids)}; "
                                       f"keeping the other agents' findings.",
                        }})
                    retry_count += 1
                    continue

                # Save cycle blueprint
                save_cycle_blueprint_file(plan_id, cycle_id, synthesis_result.get("blueprint", {}), project_name)

                # Check the research against the user's own brief before asking for
                # approval, so problems are in front of the user when they decide.
                # Advisory only: it informs the gate, it never blocks or re-runs.
                brief_check = await check_research_against_brief(
                    user_brief, cycle, synthesis_result.get("blueprint", {}),
                    research_output.get("agent_results", []),
                    plan_cycles=cycles,
                )
                problem_count = len(brief_check["violations"]) + len(brief_check["failed_agents"])
                if event_logger and problem_count:
                    event_logger({"event_type": "narrative", "data": {"phase": "gate", "message": f"Brief check found {problem_count} problem(s) in Cycle {cycle_id} — review them before approving.", "icon": "⚠️"}})

                # Disagreements the evidence didn't settle are the user's to decide, so say so.
                unresolved = synthesis_result.get("unresolved_disagreements") or 0
                if event_logger and unresolved:
                    event_logger({"event_type": "narrative", "data": {"phase": "gate", "message": f"{unresolved} disagreement(s) in Cycle {cycle_id} were not settled by evidence — open the Cycle {cycle_id} blueprint and decide them before approving.", "icon": "⚖️"}})

                # 2c: Per-cycle approval gate
                print(f"[Pipeline] Waiting for human approval of Cycle {cycle_id} research...")
                if event_logger:
                    event_logger({"event_type": "gate_waiting", "source": f"cycle_{cycle_id}_research", "data": {"cycle": cycle, "brief_check": brief_check, "unresolved_disagreements": unresolved}})
                    event_logger({"event_type": "narrative", "data": {"phase": "gate", "message": f"Waiting for your approval of Cycle {cycle_id} research...", "icon": "🚧"}})

                gate_result = await gate_approve_fn(
                    gate_id=f"cycle_{cycle_id}_research",
                    data={
                        "cycle": cycle,
                        "synthesis": synthesis_result,
                        "approved_so_far": approved_blueprints,
                        "brief_check": brief_check,
                        "unresolved_disagreements": unresolved,
                    }
                )

                if gate_result.get("approved"):
                    if event_logger:
                        event_logger({"event_type": "gate_resolved", "source": f"cycle_{cycle_id}_research", "data": gate_result})
                        event_logger({
                            "event_type": "cycle_approved",
                            "source": f"cycle_{cycle_id}_research",
                            "data": {
                                "cycle_id": cycle_id,
                                "blueprint": synthesis_result.get("blueprint", {})
                            }
                        })
                    approved_blueprints.append(synthesis_result.get("blueprint", {}))
                    # Record the sign-off on disk before moving on, so closing the app
                    # here resumes at the next cycle — and closing it a moment earlier,
                    # at the gate, resumes at this one.
                    mark_cycle_approved(plan_id, cycle_id, project_name)
                    if cycle_id not in approved_cycle_ids:
                        approved_cycle_ids.append(cycle_id)
                    break  # advance to next cycle
                else:
                    # Gate rejected — re-brief specific agents
                    redirect_note = gate_result.get("redirect_note", "")
                    rejected_steps = gate_result.get("rejected_steps")
                    print(f"[Pipeline] Cycle {cycle_id} gate rejected. Re-planning. Note: {redirect_note}")
                    retry_history.append(f"Cycle {cycle_id} rejection retry {retry_count + 1}. Feedback: {redirect_note}")
                    # What the user said here is now part of what they asked for, so every
                    # agent, the brief check and the later cycles see it — not just the Brain.
                    user_brief = await record_gate_change(brief_path, user_brief, f"cycle {cycle_id} research", redirect_note)
                    if event_logger:
                        event_logger({"event_type": "gate_resolved", "source": f"cycle_{cycle_id}_research", "data": gate_result})
                    # Re-plan only this cycle
                    try:
                        agent_plan_update = build_agent_plan(
                            planning_task, redirect_note=redirect_note,
                            cycle_id=cycle_id, approved_blueprints=approved_blueprints,
                            rejected_steps=rejected_steps, event_logger=event_logger
                        )
                        if "error" in agent_plan_update:
                            raise ValueError(agent_plan_update["error"])
                        
                        # Update only this cycle's agents (match by cycle_id)
                        updated_cycles = agent_plan_update.get("cycles", [])
                        matching_cycle = pick_cycle(updated_cycles, cycle_id)
                        if matching_cycle:
                            cycle.update(matching_cycle)
                        
                        save_agent_plan_file(plan_id, agent_plan, project_name)
                        if event_logger:
                            event_logger({"event_type": "agent_plan_compiled", "source": "Brain", "data": agent_plan})
                    except Exception as e:
                        print(f"[Pipeline] Re-planning failed: {e}")
                        try:
                            from jarvis import push_message
                            push_message("system", f"Re-planning failed: {e}. Retrying cycle with existing instructions.")
                        except ImportError:
                            pass
                    kept_results = {}  # a rejected cycle is re-planned, so all of it runs again
                    previous_results = {}
                    # Move the rejected research aside too. A brief can come through the re-plan
                    # unchanged (or the re-plan can fail), and a restart must not reuse it.
                    for agent in all_agents:
                        keep_previous_file(os.path.join(_agents_dir(project_name),
                                                        f"research_{agent.get('agent_id')}_{plan_id}.md"), "attempt")
                    retry_count += 1

            if retry_count >= MAX_RETRIES:
                return {
                    "status": "escalated_to_human",
                    "message": f"Failed after {MAX_RETRIES} retries at Cycle {cycle_id} research loop.",
                    "retry_history": retry_history,
                }

        # Phase 3: Master Blueprint Compilation
        master_blueprint = None
        if existing_plan:
            blueprint_dir = os.path.join(BASE_DIR, "Let Jarvis Handle It", project_name, "Implementation plan", "Final Plans")
            blueprint_file = os.path.join(blueprint_dir, f"master_blueprint_{plan_id}.md")
            if os.path.exists(blueprint_file):
                try:
                    with open(blueprint_file, "r", encoding="utf-8") as f:
                        file_content = f.read()
                    if "```json" in file_content:
                        parts = file_content.split("```json")
                        json_part = parts[-1].split("```")[0].strip()
                        disk_bp = json.loads(json_part)
                        
                        tools_data = load_apis_mcps_file(plan_id, project_name)
                        disk_bp["tool_recommendations"] = tools_data.get("brain", []) + tools_data.get("agents", [])
                        master_blueprint = disk_bp
                        print(f"[Pipeline] Successfully read and updated master blueprint from disk: {blueprint_file}")
                except Exception as e:
                    print(f"[Pipeline] Error reading master blueprint from disk: {e}")
                    master_blueprint = existing_plan.get("master_blueprint")
                    if master_blueprint:
                        tools_data = load_apis_mcps_file(plan_id, project_name)
                        master_blueprint["tool_recommendations"] = tools_data.get("brain", []) + tools_data.get("agents", [])
            else:
                master_blueprint = existing_plan.get("master_blueprint")
                if master_blueprint:
                    tools_data = load_apis_mcps_file(plan_id, project_name)
                    master_blueprint["tool_recommendations"] = tools_data.get("brain", []) + tools_data.get("agents", [])

        if not master_blueprint or not master_blueprint.get("tool_recommendations"):
            print("[Pipeline] Phase 3: Compiling Master Blueprint from all cycles...")
            master_blueprint = await run_master_synthesis(approved_blueprints, event_logger=event_logger)
            tools_data = load_apis_mcps_file(plan_id, project_name)
            combined = tools_data.get("brain", []) + tools_data.get("agents", [])
            master_blueprint["tool_recommendations"] = combined
            save_master_blueprint_file(plan_id, master_blueprint, project_name)
            if event_logger:
                event_logger({"event_type": "blueprint_compiled", "source": "synthesis", "data": master_blueprint})
        else:
            print("[Pipeline] Resuming: Found existing Master Blueprint. Skipping compilation.")
            if master_blueprint:
                tools_data = load_apis_mcps_file(plan_id, project_name)
                master_blueprint["tool_recommendations"] = tools_data.get("brain", []) + tools_data.get("agents", [])

        # Check if the execution blueprint gate was already approved
        skip_exec_gate = "execution_blueprint" in completed_stages

        if not skip_exec_gate:
            # Phase 4: Plan the execution agents from the NOW-COMPLETE research. Phase 1's
            # roster was only a draft written before any research existed. Only runs once
            # (guarded the same way the gate below is) — a resume after this gate was
            # already approved should never silently rewrite an already-reviewed plan.
            print("[Pipeline] Phase 4: Planning execution agents from completed research...")
            agent_plan["execution_agents"] = plan_execution_agents(
                planning_task, agent_plan.get("execution_agents", []), master_blueprint,
                user_brief=user_brief, event_logger=event_logger
            )
            agent_plan["execution_plan_final"] = True
            save_agent_plan_file(plan_id, agent_plan, project_name)
            if event_logger:
                event_logger({"event_type": "agent_plan_compiled", "source": "Brain", "data": agent_plan})

            # Phase 5: Gate — Review Execution Blueprint
            print("[Pipeline] Phase 5: Waiting for human approval of execution blueprint...")
            exec_retry = 0
            while exec_retry < MAX_RETRIES:
                if event_logger:
                    event_logger({"event_type": "gate_waiting", "source": "execution_blueprint", "data": {"master_blueprint": master_blueprint, "execution_agents": agent_plan["execution_agents"]}})
                
                gate2 = await gate_approve_fn(
                    gate_id="execution_blueprint",
                    data={"master_blueprint": master_blueprint, "execution_agents": agent_plan["execution_agents"]}
                )
                if gate2.get("approved"):
                    if event_logger:
                        event_logger({"event_type": "gate_resolved", "source": "execution_blueprint", "data": gate2})
                    break
                redirect_note = gate2.get("redirect_note", "")
                rejected_steps = gate2.get("rejected_steps")
                print(f"[Pipeline] Execution Blueprint gate rejected. Re-planning. Note: {redirect_note}")
                retry_history.append(f"Execution Blueprint rejection retry {exec_retry + 1}. Feedback: {redirect_note}")
                user_brief = await record_gate_change(brief_path, user_brief, "execution blueprint", redirect_note)
                if event_logger:
                    event_logger({"event_type": "gate_resolved", "source": "execution_blueprint", "data": gate2})
                # Re-plan execution only. This used to rebuild the whole plan, replacing
                # the research cycles the user had already approved.
                agent_plan["execution_agents"] = plan_execution_agents(
                    planning_task, agent_plan.get("execution_agents", []), master_blueprint,
                    user_brief=user_brief, redirect_note=redirect_note, rejected_steps=rejected_steps,
                    event_logger=event_logger
                )
                agent_plan["execution_plan_final"] = True
                save_agent_plan_file(plan_id, agent_plan, project_name)
                if event_logger:
                    event_logger({"event_type": "agent_plan_compiled", "source": "Brain", "data": agent_plan})
                exec_retry += 1

            if exec_retry >= MAX_RETRIES:
                return {
                    "status": "escalated_to_human",
                    "message": f"Failed after {MAX_RETRIES} retries at Execution Blueprint gate.",
                    "retry_history": retry_history,
                }

        # What the gate should actually ask about: the services the agents in
        # THIS plan declared they need, resolved onto real connectors.
        #
        # It used to be fed master_blueprint["tool_recommendations"], which is
        # the research agents' `recommended_tools` — the subject matter of the
        # research, not the pipeline's requirements. On a briefing about local
        # LLM runners that meant being asked for API keys for Ollama, llama.cpp
        # and nvidia-smi, none of which any agent asked to use and none of which
        # have keys to give. Research findings stay in the payload as reading
        # material; only genuine requirements can block.
        required_tools = plan_tool_requirements(agent_plan)
        research_suggestions = master_blueprint.get("tool_recommendations", []) or []
        rec_by_service = {
            str(r.get("service", "")).lower(): r
            for r in research_suggestions if isinstance(r, dict)
        }

        # Check if plugging gate was already approved
        from agents.tool_executor import describe_connectable

        skip_plugging_gate = False
        if "api_mcp_plugging" in completed_stages:
            from connectors.api_connector import load_registry
            load_registry()
            # Only skip if everything genuinely needed is connected — and for an
            # MCP server that means it actually starts, not that a file says so.
            unconfigured = [
                svc for svc in required_tools["connectable"]
                if not describe_connectable(svc)["configured"]
            ]
            if not unconfigured:
                skip_plugging_gate = True

        if not skip_plugging_gate:
            # Phase 5.5: API/MCP Plugging Gate
            from connectors.api_connector import load_registry, get_service_status
            load_registry()

            tool_recs = []
            for canonical, raw_names in required_tools["connectable"].items():
                # Carry across whatever the research wrote about this service
                # (purpose, docs, pros/cons) so the gate still reads richly.
                meta = {}
                for name in [canonical] + raw_names:
                    if name.lower() in rec_by_service:
                        meta = dict(rec_by_service[name.lower()])
                        break
                meta.update(describe_connectable(canonical))
                meta.update({
                    "requested_as": raw_names,
                    "required_by_agents": required_tools["requested_by"].get(canonical, []),
                })
                tool_recs.append(meta)

            if required_tools["not_a_service"]:
                print(f"[Pipeline] Not asking about non-connectable requests: {required_tools['not_a_service']}")

            print("[Pipeline] Phase 5.5: Waiting for human confirmation of API/MCP tools...")
            gate_payload = {
                "tool_recommendations": tool_recs,
                # Informational only — never blocks, never asks for a key.
                "research_suggestions": research_suggestions,
                "not_connectable": required_tools["not_a_service"],
                "message": (
                    "These are the services your agents actually need for this pipeline. "
                    "Unconfigured ones need credentials before the agents can use them."
                    if tool_recs else
                    "Your agents don't need any external service for this pipeline. "
                    "Approve to continue."
                ),
                "registry": load_registry(),
            }
            if event_logger:
                event_logger({"event_type": "gate_waiting", "source": "api_mcp_plugging", "data": gate_payload})

            plugging_gate = await gate_approve_fn(
                gate_id="api_mcp_plugging",
                data=gate_payload,
            )
            if not plugging_gate.get("approved"):
                if event_logger:
                    event_logger({"event_type": "gate_resolved", "source": "api_mcp_plugging", "data": plugging_gate})
                return {"status": "blocked", "message": "User did not confirm API/MCP configuration."}

            if event_logger:
                event_logger({"event_type": "gate_resolved", "source": "api_mcp_plugging", "data": plugging_gate})

        # Check if execution phase completed
        skip_execution = False
        exec_results = []
        if "execution" in completed_stages and not force_reexecute:
            skip_execution = True
            exec_results = existing_plan.get("exec_results", [])
            print("[Pipeline] Resuming: Execution deliverables already completed. Skipping execution agents.")
        elif existing_plan and force_reexecute:
            print("[Pipeline] Resuming with force_reexecute=True: re-running execution agents fresh instead of replaying stale exec_results.")
            if event_logger:
                event_logger({"event_type": "narrative", "data": {"phase": "execution", "message": "Force re-executing — discarding stale exec_results and running execution agents fresh...", "icon": "🔁"}})

        if not skip_execution:
            # Phase 6: Execution + Quality Check
            print("[Pipeline] Phase 6: Parallel execution agents...")
            if event_logger:
                event_logger({"event_type": "narrative", "data": {"phase": "execution", "message": "Execution agents producing deliverables...", "icon": "⚡"}})

            exec_output = await run_execution_phase(agent_plan, master_blueprint, event_logger=event_logger, project_name=project_name, user_brief=user_brief)
            exec_results = exec_output.get("agent_results", [])
            for r in exec_results:
                if r.get("status") == "ok":
                    save_execution_output_file(plan_id, r.get("agent_id"), r, project_name)

            if event_logger:
                event_logger({"event_type": "narrative", "data": {"phase": "qa", "message": "Quality checker validating agent outputs...", "icon": "✅"}})

            qa_result = await run_quality_checker(exec_results, agent_plan, master_blueprint, user_brief=user_brief)

            qa_retry = 0
            while not qa_result["all_passed"] and qa_retry < MAX_RETRIES:
                failed_ids = qa_result["failed_agents"]
                print(f"[Pipeline] Quality check failed for: {failed_ids}. Re-running those agents...")
                for res in qa_result["results"]:
                    if not res["passed"]:
                        print(f"  - Agent {res['agent_id']} issues: {res['issues']}")
                
                retry_history.append(f"QA verification failure retry {qa_retry + 1} for agents {failed_ids}.")
                exec_output = await run_execution_phase(
                    agent_plan, master_blueprint, agent_ids_to_run=failed_ids, event_logger=event_logger, project_name=project_name,
                    user_brief=user_brief
                )
                retry_map = {r["agent_id"]: r for r in exec_output["agent_results"]}
                exec_results = [retry_map.get(r["agent_id"], r) for r in exec_results]
                for r in exec_results:
                    if r.get("status") == "ok":
                        save_execution_output_file(plan_id, r.get("agent_id"), r, project_name)
                qa_result = await run_quality_checker(exec_results, agent_plan, master_blueprint, user_brief=user_brief)
                qa_retry += 1

            if not qa_result["all_passed"]:
                if event_logger:
                    # Surface what the agents produced, but flagged as not passing —
                    # a resume must re-run execution rather than treat this as done.
                    event_logger({"event_type": "execution_completed", "source": "execution", "data": exec_results, "qa_passed": False})
                issues_summary = []
                for res in qa_result.get("results", []):
                    if not res.get("passed"):
                        issues_summary.append(f"{res['agent_id']}: {res.get('issues', 'failed specs')}")
                msg = f"Failed after {MAX_RETRIES} retries at Quality Checker verification."
                if issues_summary:
                    msg += " // " + " | ".join(issues_summary)
                return {
                    "status": "escalated_to_human",
                    "message": msg,
                    "retry_history": retry_history,
                }

            if event_logger:
                event_logger({"event_type": "execution_completed", "source": "execution", "data": exec_results, "qa_passed": True})

        # Check if Final QA was approved
        skip_final_qa = "final_qa" in completed_stages and not force_reexecute

        if not skip_final_qa:
            # Phase 7: Gate — Final QA
            print("[Pipeline] Phase 7: Waiting for final human QA gate...")
            final_retry = 0
            while final_retry < MAX_RETRIES:
                if event_logger:
                    event_logger({"event_type": "gate_waiting", "source": "final_qa", "data": exec_results})
                
                gate3 = await gate_approve_fn(
                    gate_id="final_qa",
                    data={"exec_results": exec_results}
                )
                if gate3.get("approved"):
                    if event_logger:
                        event_logger({"event_type": "gate_resolved", "source": "final_qa", "data": gate3})
                    break
                redirect_note = gate3.get("redirect_note", "")
                rejected_steps = gate3.get("rejected_steps")
                print(f"[Pipeline] Final QA gate rejected. Re-running target agents. Note: {redirect_note}")
                retry_history.append(f"Final QA rejection retry {final_retry + 1}. Feedback: {redirect_note}")
                user_brief = await record_gate_change(brief_path, user_brief, "final QA", redirect_note)
                if event_logger:
                    event_logger({"event_type": "gate_resolved", "source": "final_qa", "data": gate3})
                if rejected_steps:
                    rejected_ids = rejected_steps
                else:
                    rejected_ids = await identify_rejected_agents(redirect_note, agent_plan)
                exec_output = await run_execution_phase(
                    agent_plan, master_blueprint,
                    agent_ids_to_run=rejected_ids, gate_redirect_note=redirect_note, event_logger=event_logger,
                    project_name=project_name, user_brief=user_brief
                )
                retry_map = {r["agent_id"]: r for r in exec_output["agent_results"]}
                exec_results = [retry_map.get(r["agent_id"], r) for r in exec_results]
                for r in exec_results:
                    if r.get("status") == "ok":
                        save_execution_output_file(plan_id, r.get("agent_id"), r, project_name)
                final_retry += 1

            if final_retry >= MAX_RETRIES:
                return {
                    "status": "escalated_to_human",
                    "message": f"Failed after {MAX_RETRIES} retries at Final human QA gate.",
                    "retry_history": retry_history,
                }

        # Check if deployment completed
        skip_deploy = "deploy" in completed_stages and not force_reexecute

        if not skip_deploy:
            # Phase 8: Deployment
            print("[Pipeline] Phase 8: Calling deployment agent...")
            if event_logger:
                event_logger({"event_type": "running", "source": "DeploymentAgent"})
            deploy_result = await run_deployment_agent(exec_results, master_blueprint)
            if event_logger:
                event_logger({"event_type": "completed", "source": "DeploymentAgent", "data": deploy_result})

            final_rep = {
                "task": task,
                "master_blueprint": master_blueprint,
                "exec_results": exec_results,
                "deploy_result": deploy_result,
            }
            save_final_report_file(plan_id, final_rep, project_name)

            return {
                "status": "complete",
                "task": task,
                "master_blueprint": master_blueprint,
                "exec_results": exec_results,
                "deploy_result": deploy_result,
            }

        return {
            "status": "complete",
            "task": task,
            "master_blueprint": master_blueprint,
            "exec_results": exec_results,
            "deploy_result": (existing_plan or {}).get("deploy_result", {}),
        }

    finally:
        conn.close()
