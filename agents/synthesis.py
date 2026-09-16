"""
Synthesis Agent — LLM-powered conflict detection and blueprint compression.
Replaces the naive dict-merge approach with structured Gemini calls.
"""
import json
import os
import re
import asyncio
from google import genai
from google.genai import types
from dotenv import load_dotenv

from urllib.parse import urlsplit

from agents.links import URL_RE, Evidence, strip_unevidenced_links

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Every step that merges agents' work into one document. Merging is where facts
# drift: pipeline 7's Cycle 1 blueprint turned the Georgia headings an agent had
# measured into "Lato or Open Sans", and added a "Request a Demo" button and an
# IBM logo that no agent had reported.
MERGE_FACT_RULES = (
    "- Add nothing that is not in the findings you were given: no new facts, names, clients, "
    "buttons, prices, numbers or quotes.\n"
    "- Never change or \"correct\" a specific value (a font, colour, price, count, name or quote). "
    "Where findings disagree on one, keep the value that came from a direct inspection or tool "
    "result over one that was inferred, and record the disagreement under \"disagreements\" "
    "instead of silently picking one.\n"
    "- Never write a link that is not already in the findings you were given."
)

# Pipeline 9's Cycle 1: the lead reviewed its own work and every disagreement went its
# way "as it aligns with the lead specialist's chosen output". Pakistan, which an advisor
# backed with the clearest evidence, was dropped without a word, and "just starting to
# use AI" was redefined as "recent acceleration" so the lead's picks still fit.
DISAGREEMENT_RULES = (
    "- Weigh every agent's findings the same way, the lead's included. Who found something never "
    "decides which side is kept; evidence does.\n"
    "- Keep the better-evidenced side: a fact or figure a tool returned over one that was inferred; a "
    "primary or official source (a statistics office, the IMF, the company's own website) over a "
    "secondary summary; a specific, dated figure over a vague one; the more recent figure for "
    "something that changes. Say which evidence decided it under \"decided_by\".\n"
    "- \"The lead's finding\", \"my own output\", \"it aligns with the lead's output\" and \"internal "
    "consistency\" are not reasons. If the evidence does not settle it, set \"resolution\": "
    "\"unresolved\" and keep both sides for the user to decide.\n"
    "- Name the source that shows it in \"decided_by_source\": a link from the findings, or the exact "
    "search an agent ran. An agent saying it checked, resolved, verified or confirmed something is "
    "not evidence — that is the agent vouching for itself. Cite what the tool returned, or leave it "
    "unresolved.\n"
    "- Apply the user's brief as written. Never redefine one of its criteria to fit a candidate."
)

DISAGREEMENT_SHAPE = (
    '{"description": "what the agents disagree on", '
    '"sides": [{"agent": "agent_id", "value": "what it found", "evidence": "the source or tool result behind it"}], '
    '"resolution": "the agent_id whose value is kept, or unresolved", '
    '"decided_by": "the evidence that settled it", '
    '"decided_by_source": "the link or the exact search it comes from"}'
)

# Reasons that name who said something instead of what shows it. Pipeline 9's
# blueprint settled four disagreements with exactly these words.
_SETTLED_BY_AUTHOR = re.compile(
    r"\balign(?:s|ed|ing)? with (?:the |my |its |our )?(?:lead|own)"
    r"|\bconsisten(?:t|cy) with (?:the |my |its |our )?(?:lead|own)"
    r"|\binternal consistency\b"
    r"|\b(?:chosen|direct|own|final) output\b"
    r"|\b(?:applied|chosen|selected|decided|retained|preferred) by the lead\b",
    re.IGNORECASE,
)

# Everything an agent reported goes in except bookkeeping. Picking a few keys to keep
# hid pipeline 9's AI Adoption Indicator Specialist's verdicts on Ireland, New Zealand
# and Singapore (it wrote them under general_memory), so the conflict check called the
# lead's correct attribution invented and sent Cycle 1 back to research twice.
_BOOKKEEPING_KEYS = ("evidence_links", "researched_brief", "recommended_tools")


def original_findings_json(agent_results) -> str | None:
    """Each agent's own findings, from before the lead's review merged them."""
    if not agent_results:
        return None
    return json.dumps([{k: v for k, v in r.items() if k not in _BOOKKEEPING_KEYS}
                       for r in agent_results if isinstance(r, dict)], indent=2, default=str)


# An agent telling you it checked something is the claim, not the evidence for it.
# Pipeline 9's blueprint kept Malaysia as "English-speaking" because the AI Adoption
# Indicator Specialist had mentioned "resolved ... English official language data
# inconsistencies" — while its own search said English is official only in two states.
_SELF_VOUCHING = re.compile(
    r"\b(?:agent|specialist|researcher|analyst|lead|it)\b[^.]{0,60}?"
    r"\b(?:mention(?:s|ed)?|statement|claim(?:s|ed)?|assert(?:s|ed|ion)?|says?|said|notes?|noted|"
    r"reports? that|confirm(?:s|ed)?|resolved|verified|checked|thorough)\b",
    re.IGNORECASE,
)

# Tool names an agent's sources are written with: "web_search: <query>".
_SOURCE_PREFIXES = ("web_search", "inspect_website", "arxiv_search", "read_file", "google_search")


def known_sources(*groups) -> list[str]:
    """Every source string the agents and the blueprint list, as written."""
    found = []
    for group in groups:
        for item in (group if isinstance(group, list) else [group]):
            if isinstance(item, dict):
                for source in item.get("sources") or []:
                    text = " ".join(str(source).split())
                    if text and text not in found:
                        found.append(text)
    return found


def points_at_evidence(text: str, evidence: Evidence | None, sources: list[str] | None) -> bool:
    """True when a reason names something a tool really returned, not just an agent's word."""
    lowered = " ".join((text or "").lower().split())
    if not lowered:
        return False
    if evidence:
        for url in URL_RE.findall(text or ""):
            if evidence.has(url):
                return True
        for url in evidence.urls:
            host = (urlsplit(url).hostname or "").lower()
            host = host[4:] if host.startswith("www.") else host
            if len(host) >= 5 and host in lowered:
                return True
    for source in sources or []:
        written = " ".join(str(source).lower().split())
        head, _, rest = written.partition(":")
        core = rest.strip() if head in _SOURCE_PREFIXES and rest.strip() else written
        if len(core) >= 8 and core in lowered:
            return True
    return False


def evidence_of(*groups) -> Evidence:
    """The links agents' tools really returned: their sources and their evidence links.

    Only those fields count. A link written into findings text is exactly the thing
    being checked, so it can't vouch for itself.
    """
    texts = []
    for group in groups:
        for r in (group if isinstance(group, list) else [group]):
            if isinstance(r, dict):
                texts += [str(s) for s in r.get("sources") or []]
                texts += [str(u) for u in r.get("evidence_links") or []]
    return Evidence(texts)


def audit_disagreements(blueprint, evidence: Evidence | None = None,
                        sources: list[str] | None = None) -> int:
    """Mark disagreements nothing outside an agent's own word settled as unresolved.

    Settled means pointing at something a tool returned. Who said it is not a reason,
    and neither is an agent's report of its own diligence. Returns how many are
    unresolved, so the gate can say so.
    """
    items = blueprint.get("disagreements") if isinstance(blueprint, dict) else None
    if not isinstance(items, list):
        return 0
    sources = list(sources or []) + known_sources(blueprint)
    unresolved = 0
    for d in items:
        if not isinstance(d, dict):
            continue
        if str(d.get("resolution") or "").strip().lower() == "unresolved":
            unresolved += 1
            continue
        reason = " ".join(str(d.get(k) or "") for k in ("decided_by", "resolution_note")).strip()
        cited = " ".join(str(d.get(k) or "") for k in ("decided_by_source", "decided_by", "resolution_note")).strip()
        problem = None
        if not reason:
            problem = "No evidence was given for how this was settled."
        elif _SETTLED_BY_AUTHOR.search(reason):
            problem = "It was settled by who said it, not by evidence."
        elif not points_at_evidence(cited, evidence, sources):
            problem = ("It rests on what an agent said about its own work, which is not evidence."
                       if _SELF_VOUCHING.search(cited)
                       else "It names no source any tool returned.")
        if problem:
            d["resolution"] = "unresolved"
            d["flag"] = f"{problem} Decide this one at the gate."
            unresolved += 1
    return unresolved


def _finish_blueprint(blueprint, evidence: Evidence,
                      sources: list[str] | None = None) -> tuple[dict, int]:
    """Take out links no agent's tool returned, and flag disagreements nobody really settled."""
    if not isinstance(blueprint, dict):
        return blueprint, 0
    blueprint, removed = strip_unevidenced_links(blueprint, evidence)
    if removed:
        blueprint["invented_links_removed"] = len(removed)
    return blueprint, audit_disagreements(blueprint, evidence, sources)


async def run_synthesis_agent(authoritative_output: dict | list, event_logger=None,
                              agent_results: list[dict] | None = None) -> dict:
    """
    Takes the Lead Specialist's authoritative cycle output.
    1. Runs LLM conflict detection for internal contradictions — and, given each
       agent's original findings, for what the lead's review dropped or overrode.
    2. If no conflicts, compresses into a hyper-dense cycle blueprint.

    Returns: {"status": "ok"|"conflict", "blueprint": {...}, "has_conflicts": bool, "conflicts": [...],
              "unresolved_disagreements": int}
    """
    client = genai.Client(api_key=GEMINI_API_KEY)
    loop = asyncio.get_running_loop()

    findings_json = json.dumps(authoritative_output, indent=2)
    originals_json = original_findings_json(agent_results)
    evidence = evidence_of(authoritative_output, agent_results or [])
    cited_sources = known_sources(authoritative_output, agent_results or [])

    # Disagreements the findings can settle themselves. They go into the blueprint
    # with both sides shown instead of sending the whole cycle back to research.
    disagreements = []

    # 1. Conflict detection
    conflict_prompt = _detect_conflicts_prompt(findings_json, originals_json)
    conflict_config = types.GenerateContentConfig(
        system_instruction="You are a Conflict Detector. Output valid JSON only.",
        response_mime_type="application/json",
    )

    if event_logger:
        event_logger({
            "event_type": "thinking",
            "source": "SynthesisAgent",
            "data": {
                "thinking_type": "system_prompt",
                "role": "Synthesis Conflict Detector",
                "content": "You are a Conflict Detector. Output valid JSON only."
            }
        })
        event_logger({
            "event_type": "thinking",
            "source": "SynthesisAgent",
            "data": {
                "thinking_type": "user_prompt",
                "role": "Synthesis Conflict Detector",
                "content": conflict_prompt
            }
        })
        event_logger({
            "event_type": "prompt_sent",
            "source": "SynthesisAgent",
            "data": {
                "role": "Synthesis Conflict Detector",
                "content": conflict_prompt
            }
        })

    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=conflict_prompt,
                config=conflict_config
            )
        )
        if event_logger:
            event_logger({
                "event_type": "response_received",
                "source": "SynthesisAgent",
                "data": {
                    "role": "Synthesis Conflict Detector",
                    "content": response.text
                }
            })

        conflict_res = json.loads(response.text)
        blocking, disagreements = split_conflicts(conflict_res.get("conflicts"))
        if blocking:
            return {
                "status": "conflict",
                "has_conflicts": True,
                "conflicts": blocking,
                "reconcilable": disagreements,
                "message": "Conflicts detected in research."
            }
    except Exception as e:
        print(f"[Synthesis] Error in conflict detection: {e}")

    # 2. Compression
    compress_prompt = _compress_blueprint_prompt(findings_json, disagreements, originals_json)
    compress_config = types.GenerateContentConfig(
        system_instruction="You are a Synthesis Agent. Output valid JSON only.",
        response_mime_type="application/json",
    )

    if event_logger:
        event_logger({
            "event_type": "thinking",
            "source": "SynthesisAgent",
            "data": {
                "thinking_type": "system_prompt",
                "role": "Synthesis Compactor",
                "content": "You are a Synthesis Agent. Output valid JSON only."
            }
        })
        event_logger({
            "event_type": "thinking",
            "source": "SynthesisAgent",
            "data": {
                "thinking_type": "user_prompt",
                "role": "Synthesis Compactor",
                "content": compress_prompt
            }
        })
        event_logger({
            "event_type": "prompt_sent",
            "source": "SynthesisAgent",
            "data": {
                "role": "Synthesis Compactor",
                "content": compress_prompt
            }
        })

    try:
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=compress_prompt,
                config=compress_config
            )
        )
        if event_logger:
            event_logger({
                "event_type": "response_received",
                "source": "SynthesisAgent",
                "data": {
                    "role": "Synthesis Compactor",
                    "content": response.text
                }
            })

        blueprint, unresolved = _finish_blueprint(json.loads(response.text), evidence, cited_sources)
        return {
            "status": "ok",
            "has_conflicts": False,
            "blueprint": blueprint,
            "reconciled_conflicts": disagreements,
            "unresolved_disagreements": unresolved,
        }
    except Exception as e:
        print(f"[Synthesis] Error in blueprint compression: {e}")
        # Fallback: if it's a list, merge all dicts, otherwise return as-is
        fallback_blueprint = {}
        if isinstance(authoritative_output, list):
            for item in authoritative_output:
                fallback_blueprint.update(item.get("findings", item))
        else:
            fallback_blueprint = authoritative_output.get("findings", authoritative_output)
        fallback_blueprint, unresolved = _finish_blueprint(fallback_blueprint, evidence, cited_sources)
        return {
            "status": "ok",
            "has_conflicts": False,
            "blueprint": fallback_blueprint,
            "unresolved_disagreements": unresolved,
            "error": str(e)
        }


async def run_master_synthesis(approved_blueprints: list[dict], event_logger=None) -> dict:
    """
    Takes N approved cycle blueprints and produces one unified Master Research Blueprint.
    This is the document that feeds into the Execution Plan.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)
    loop = asyncio.get_running_loop()

    blueprints_json = json.dumps(approved_blueprints, indent=2)
    prompt = f"""You are the Synthesis Agent. Compress the following approved research blueprints
into a single unified Master Research Blueprint JSON. Resolve overlapping concepts.
Keep unique, complementary, high-value strategy details from every source.
Do NOT arbitrarily discard any agent's unique contributions.
{MERGE_FACT_RULES}

Additionally, aggregate all 'recommended_tools' from every cycle blueprint.
Merge recommendations from multiple agents for the same service.
Include the tool aggregation at the top-level "tool_recommendations" key.
Each tool in "tool_recommendations" must have these exact keys:
  - "service": the exact name of the tool/service/API
  - "agent_consensus": consensus strength (must be one of: "strong", "mixed", "weak")
  - "recommended_by": list of agent IDs recommending it
  - "purpose": description of why it is needed
  - "pros": list of advantages
  - "cons": list of disadvantages
  - "alternatives": list of alternative options

BLUEPRINTS:
{blueprints_json}

Return a single flat JSON blueprint containing "tool_recommendations" and the unified findings."""

    config = types.GenerateContentConfig(
        system_instruction="You are a Master Synthesis Orchestrator. Output valid JSON only.",
        response_mime_type="application/json",
    )

    if event_logger:
        event_logger({
            "event_type": "thinking",
            "source": "MasterSynthesisAgent",
            "data": {
                "thinking_type": "system_prompt",
                "role": "Master Synthesis",
                "content": "You are a Master Synthesis Orchestrator. Output valid JSON only."
            }
        })
        event_logger({
            "event_type": "thinking",
            "source": "MasterSynthesisAgent",
            "data": {
                "thinking_type": "user_prompt",
                "role": "Master Synthesis",
                "content": prompt
            }
        })
        event_logger({
            "event_type": "prompt_sent",
            "source": "MasterSynthesisAgent",
            "data": {
                "role": "Master Synthesis",
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
        if event_logger:
            event_logger({
                "event_type": "response_received",
                "source": "MasterSynthesisAgent",
                "data": {
                    "role": "Master Synthesis",
                    "content": response.text
                }
            })

        return json.loads(response.text)
    except Exception as e:
        print(f"[Master Synthesis] Error compiling blueprint: {e}")
        merged = {}
        for bp in approved_blueprints:
            merged.update(bp)
        return merged


def split_conflicts(conflicts) -> tuple[list[dict], list[dict]]:
    """(needs new research, can be settled from the findings already in hand).

    Every conflict used to send the whole cycle back to research. In pipeline 7 that
    happened because reviews were "mostly positive, with some complaints", which is
    not a contradiction at all, and the rerun threw away four agents' work.
    """
    blocking, reconcilable = [], []
    for c in conflicts or []:
        if not isinstance(c, dict):
            continue
        # A review that dropped or overrode an agent's finding is the user's call at the
        # gate. New research can't change what the review did: pipeline 9's Cycle 1 reran
        # all three agents over exactly that and came back with the same review problems.
        if str(c.get("kind") or "").strip().lower() == "review":
            reconcilable.append(c)
            continue
        flag = c.get("needs_new_research")
        if flag is True or str(flag).strip().lower() == "true":
            blocking.append(c)
        else:
            reconcilable.append(c)
    return blocking, reconcilable


def _detect_conflicts_prompt(findings_json: str, originals_json: str | None = None) -> str:
    """System prompt for conflict detection."""
    originals_block = ""
    if originals_json:
        originals_block = f"""
THE FINDINGS ABOVE ARE THE LEAD SPECIALIST'S REVIEW OF WHAT EACH AGENT FOUND:
{originals_json}

Something an agent backed with evidence that the review dropped, overrode or contradicted
without an evidence-based reason is a conflict too: for example a candidate an advisor
recommended that is missing from the review with no reason given, or an advisor's figure
replaced by the lead's own. Who found something is never a reason. Name the agents on both sides.
Mark these "kind": "review" and "needs_new_research": false. New research cannot change what the
review did; the user decides them at the gate.
"""
    return f"""Analyze the following research output for contradictions.

A conflict is two findings that make incompatible claims about the same specific fact: for
example one agent says the headings use Georgia and another says Lato, or one says projects
start at $5,000 and another says $50,000.

These are NOT conflicts, so do not report them:
- mixed evidence: mostly positive reviews alongside some complaints, or a company's own claims
  next to customers' criticism of them. Both are true at once and belong side by side.
- findings about different things, at different levels of detail, or with different emphasis.
- a general claim next to a more nuanced version of it.
- estimates or forecasts from different sources (research firms, reports, years) that differ because
  each measures a different scope or period. Market-size forecasts nearly always differ this way;
  they belong side by side with their sources, not in another round of research.

For each real conflict set "needs_new_research": true ONLY if the findings below cannot settle
it and an agent has to go back and check a source. If the findings already show which side is
better supported (for example a direct website inspection against an inference), set it false.

FINDINGS:
{findings_json}
{originals_block}
Return a JSON object:
{{
    "has_conflicts": true/false,
    "conflicts": [
        {{
            "description": "what contradicts what",
            "kind": "contradiction, or review when the lead's review dropped or overrode a finding",
            "agents_involved": ["agent_id_1", "agent_id_2"],
            "needs_new_research": true/false,
            "options": [
                {{"name": "Option A", "pros": "...", "cons": "..."}},
                {{"name": "Option B", "pros": "...", "cons": "..."}}
            ]
        }}
    ]
}}

If there are no contradictions, return {{"has_conflicts": false, "conflicts": []}}"""


def _compress_blueprint_prompt(findings_json: str, disagreements: list | None = None,
                               originals_json: str | None = None) -> str:
    """System prompt for blueprint compression."""
    disagreement_block = ""
    if disagreements:
        disagreement_block = (
            "\nDISAGREEMENTS IN THESE FINDINGS: record each one under \"disagreements\" with both sides "
            "and which agent said what. Never drop a side.\n" + json.dumps(disagreements, indent=2) + "\n"
        )
    originals_block = ""
    if originals_json:
        originals_block = (
            "\nWHAT EACH AGENT FOUND BEFORE THE LEAD'S REVIEW (check each side of a disagreement against "
            "these, not against how the review describes them):\n" + originals_json + "\n"
        )
    return f"""You are the Synthesis Agent. Compress the following research findings
into a single hyper-dense blueprint JSON. Resolve overlapping concepts.
Keep unique, complementary, high-value strategy details from every source.
Do NOT arbitrarily discard any agent's unique contributions.
{MERGE_FACT_RULES}

SETTLING DISAGREEMENTS:
{DISAGREEMENT_RULES}
Write each disagreement under "disagreements" as {DISAGREEMENT_SHAPE}
Keep the review's "not_adopted" list: what was left out, who proposed it, and why.
{disagreement_block}{originals_block}
Make sure to preserve and include any 'recommended_tools' arrays from the inputs, grouping them under a top-level "recommended_tools" key in your JSON output.

FINDINGS:
{findings_json}

Return a single JSON blueprint."""
