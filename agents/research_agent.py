"""
Research Agent — runs a single focused research task asynchronously.

Like execution_agent.py, this agent gets real function-calling tools bound
automatically from its `tools_needed` brief (via agents/tool_executor.py)
and must actually call them to gather real information — a "arXiv Search
Specialist" with tools_needed=["arxiv_api"] gets a real arxiv_search
function now, instead of silently getting zero tools and having to
rationalize why it "can't reach arXiv".
"""
import asyncio
import json
import os
import re
from google import genai
from google.genai import types
from dotenv import load_dotenv

from agents.tool_executor import get_tools_for_execution_agent, run_tool
from agents.user_brief import user_brief_block
from agents.links import URL_RE, Evidence, strip_unevidenced_links, cap_for_invented_links
from agents import tool_review, tool_requests

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Sent once to an agent that answers without calling a single tool. Pipeline 7's
# Cycle 2 lead did exactly that and still reported confidence 1.0.
NO_TOOLS_NUDGE = (
    "You answered without calling any of your tools, so none of that is evidence yet. "
    "Call your tools now to check it, then give your final JSON answer. If none of your tools "
    "fit, call request_tool to ask the user for one."
)

# The links an agent's tools returned travel with its result, so the lead's review and
# the blueprint can tell a real link from one somebody wrote.
EVIDENCE_LINK_LIMIT = 300


def _normalise(text) -> str:
    """Lowercase words only, so `web_search (query: X)` and `web_search: X` compare equal."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(text).lower()).split())


def describe_call(tool: str, args: dict) -> str:
    """How a tool call is listed as a source: `web_search: <query>`, `inspect_website: <url>`."""
    for key in ("query", "url", "q", "search_query", "title", "relative_path", "path"):
        value = (args or {}).get(key)
        if value:
            return f"{tool}: {value}"
    return tool


def verify_sources(claimed, calls: list[dict], tool_texts: list[str],
                   evidence: Evidence | None = None) -> tuple[list[str], list[str], list[str]]:
    """(sources, unverified, invented links): sources are the tool calls this agent really made.

    A claimed source survives only if it is one of those calls, or a link that
    appeared in what the tools returned. A search or document it names but never
    ran is kept apart as unverified — pipeline 7's Cycle 2 lead listed searches
    that another agent ran a cycle earlier, as if they were its own. A link no tool
    returned is not kept at all: pipeline 9's lead made up nine.
    """
    sources = []
    for call in calls:
        described = describe_call(call.get("tool", ""), call.get("args") or {})
        if described not in sources:
            sources.append(described)
    evidence = evidence or Evidence(sources + list(tool_texts))
    # What each call was about (its query, URL...), so a source written in another
    # format still matches the call that produced it. Very short values match too much.
    call_values = [
        _normalise(v) for call in calls for v in (call.get("args") or {}).values()
        if isinstance(v, str) and len(_normalise(v)) >= 8
    ]
    unverified, invented = [], []
    for item in claimed or []:
        item = str(item).strip()
        if not item or item in sources:
            continue
        urls = URL_RE.findall(item)
        if urls:
            if all(evidence.has(u) for u in urls):
                sources.append(item)
            elif item not in invented:
                invented.append(item)
            continue
        if any(value in _normalise(item) for value in call_values):
            continue      # the same call, written another way — already listed
        if item not in unverified:
            unverified.append(item)
    return sources, unverified, invented


async def run_research_agent(
    agent_config: dict,
    memory_context: str | None = None,
    prior_context: str | None = None,   # approved blueprints from prior cycles
    on_chunk_callback=None,
    event_logger=None,                   # NEW
    project_name: str = "Default Project",
    user_brief: str | None = None,       # the user's clarified brief, verbatim
) -> dict:
    """
    Runs a single research agent asynchronously.

    agent_config keys: agent_id, role, brief, tools_needed, memory_query
    memory_context: pre-fetched memory patterns to inject into the prompt
    Returns a dict with agent_id + structured findings.
    """
    agent_id = agent_config.get("agent_id", "unknown")
    role = agent_config.get("role", "Researcher")
    brief = agent_config.get("brief", "")
    tools_needed = agent_config.get("tools_needed", ["google_search"])

    declarations, handlers, unavailable = get_tools_for_execution_agent(
        tools_needed, project_name, context=f"{role}: {brief}"[:400]
    )
    declarations = list(declarations) + [tool_requests.DECLARATION]

    unavailable_note = ""
    if unavailable:
        unavailable_note = (
            "\nTOOLS YOU ASKED FOR BUT ARE NOT ACTUALLY CONNECTED YET:\n"
            + "\n".join(f"- {u}" for u in unavailable)
            + "\nDo NOT claim to have used these or fabricate results for them. Say plainly in your "
              "findings/recommendation that this part is blocked and why.\n"
        )

    system_prompt = f"""
You are a highly specialized {role} agent in the Jarvis multi-agent system.

YOUR BRIEF:
{brief}

{user_brief_block(user_brief)}
{"RELEVANT PAST PATTERNS FROM MEMORY:\n" + memory_context if memory_context else ""}

{"APPROVED RESEARCH FROM PRIOR CYCLES (use as established context):\n" + prior_context if prior_context else ""}

TOOLS: You have real tools available (write_file, read_file, list_deliverables, and any research
connectors listed below such as arxiv_search / web_search). USE them to gather real information —
do not invent findings, sources, or data. If your brief calls for searching arXiv or the web, you
MUST actually call the corresponding tool before answering.
web_search lists the pages it drew on under "sources" (each with its real link) and, under
"citations", which page backs which sentence of its summary.
To see what a real website looks like or how it is built — fonts, colours, layout, imagery, calls
to action — call inspect_website with its URL. web_search only returns what others wrote about it.
If you need a tool you don't have, or a better one for this job, call request_tool with its name and
why. The user approves or rejects it on the Commands page and the call returns their answer. Don't
decide on your own that no tool could help — ask.
{unavailable_note}
CRITICAL RULES:
1. Focus ONLY on your brief. Do not go beyond it.
2. Actually call your tools to gather real evidence before answering. Do not just describe what you would search for.
3. Your FINAL response (after tool calls are done) must be a JSON object only, no markdown fences.
4. Include an "agent_id" field set to "{agent_id}" in your output.
5. Include a "confidence" field from 0.0 to 1.0 rating how certain you are.
6. Every claim must be backed by real evidence returned from a tool call, or explicitly marked as your own reasoning.
7. If your research reveals specific APIs, services, or tools that would be valuable for executing this task, include them in your output under "recommended_tools".
8. Save critical discoveries, formulas, constants, or key technical specifications under "high_value_memory" as a key-value dictionary.
9. Save general notes, minor facts, background logs, or broad summaries under "general_memory" as a key-value dictionary.
10. If a needed tool is unavailable (see above), set "status": "partial" and explain what's missing in "blocked_reason" — never fabricate as if you had real access.
11. Approved research from prior cycles is context, not proof. If your own tool evidence contradicts it (e.g. a button, font or client it names is not on the page you inspected), report what you found and list the contradiction under "contradicts_prior_research". Never repeat a prior claim your own evidence disproves.
12. Never say something is not publicly available unless searches you actually ran failed to find it. List those searches under "searches_tried". Many things that look private have public sources — look for them before giving up.
13. LINKS: only write a link that one of your tools returned — a URL from web_search's "sources", a page you inspected, a link in a tool result. Put it next to the fact it supports. Never invent, guess, shorten or build a URL: no made-up domains, no search-result addresses, no placeholder links. If you have a fact but no link for it, name where it came from in words. Links no tool returned are removed from your answer and lower your confidence.

Output format:
{{
  "agent_id": "{agent_id}",
  "role": "{role}",
  "status": "ok",
  "confidence": 0.0-1.0,
  "findings": {{
    "key": "value",
    ...
  }},
  "sources": ["web_search: the query you ran", "https://a-page-one-of-your-tools-returned"],
  "recommendation": "one sentence action recommendation",
  "recommended_tools": [
    {{
      "service": "youtube_api",
      "purpose": "why it is needed",
      "doc_url": "official developer website or documentation URL",
      "pros": ["pro1", "pro2"],
      "cons": ["con1", "con2"],
      "why": "specific reason",
      "alternatives": ["alt1"],
      "connection_methods": [
        {{
          "method_id": "api_key",
          "label": "API Key (Simple)",
          "fields": [{{"name": "api_key", "label": "API Key", "type": "password"}}]
        }},
        {{
          "method_id": "oauth",
          "label": "OAuth 2.0 Client",
          "fields": [
            {{"name": "client_id", "label": "Client ID", "type": "text"}},
            {{"name": "client_secret", "label": "Client Secret", "type": "password"}}
          ]
        }}
      ]
    }}
  ],
  "high_value_memory": {{
    "critical_constant_or_key_formula": "value"
  }},
  "general_memory": {{
    "background_notes_or_context": "value"
  }}
}}
"""

    if event_logger:
        event_logger({
            "event_type": "thinking",
            "source": agent_id,
            "data": {
                "thinking_type": "system_prompt",
                "role": role,
                "content": system_prompt
            }
        })
        event_logger({
            "event_type": "narrative",
            "source": agent_id,
            "data": {
                "phase": "research",
                "message": f"{role} ({agent_id}) is now investigating: {brief[:100]}...",
                "icon": "🔍"
            }
        })

    client = genai.Client(api_key=GEMINI_API_KEY)

    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[{"function_declarations": declarations}] if declarations else None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True) if declarations else None,
    )

    try:
        loop = asyncio.get_running_loop()

        if event_logger:
            event_logger({
                "event_type": "prompt_sent",
                "source": agent_id,
                "data": {
                    "role": role,
                    "content": f"Execute your research brief now. Task context: {brief}"
                }
            })

        chat = client.chats.create(model="gemini-2.5-flash", config=config)
        current_message = f"Execute your research brief now. Task context: {brief}"

        final_text = None
        calls, tool_texts, nudged = [], [], False
        # No cap on tool calls: every tool_review.REVIEW_EVERY rounds of them the user decides.
        since_review, stopped_after = 0, None
        chat_config = None   # set once an approved tool request adds a tool mid-run
        while True:
            response = await loop.run_in_executor(
                None, lambda m=current_message, c=chat_config: (
                    chat.send_message(m, config=c) if c is not None else chat.send_message(m))
            )

            function_calls = response.function_calls
            if not function_calls:
                if declarations and not calls and not nudged:
                    nudged = True
                    current_message = NO_TOOLS_NUDGE
                    continue
                final_text = response.text or ""
                if on_chunk_callback:
                    loop.call_soon_threadsafe(on_chunk_callback, agent_id, final_text)
                break

            tool_response_parts = []
            for fc in function_calls:
                tool_args = dict(fc.args) if fc.args else {}
                if event_logger:
                    event_logger({
                        "event_type": "narrative",
                        "source": agent_id,
                        "data": {
                            "phase": "research",
                            "message": f"{role} ({agent_id}) is calling tool `{fc.name}`...",
                            "icon": "🛠️"
                        }
                    })
                if fc.name == tool_requests.REQUEST_TOOL_NAME:
                    result, tools_changed = await tool_requests.handle(
                        tool_args, agent_config=agent_config, kind="research", project_name=project_name,
                        declarations=declarations, handlers=handlers, event_logger=event_logger)
                    if tools_changed:
                        chat_config = types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            tools=[{"function_declarations": declarations}],
                            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                        )
                else:
                    result = run_tool(handlers, project_name, agent_id, fc.name, tool_args)
                    calls.append({"tool": fc.name, "args": tool_args})
                    tool_texts.append(json.dumps(result, default=str, ensure_ascii=False)[:50000])
                if event_logger:
                    event_logger({
                        "event_type": "tool_result",
                        "source": agent_id,
                        "data": {"tool": fc.name, "args": tool_args, "result": result}
                    })
                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response=result if isinstance(result, dict) else {"result": result}
                    )
                )
            current_message = tool_response_parts
            # Rounds, not calls: a model often asks for several searches at once, and
            # counting each one paused an agent after its very first round.
            since_review += 1
            if since_review >= tool_review.REVIEW_EVERY:
                since_review = 0
                review = await tool_review.checkpoint(agent_id, role, "research", brief, calls, event_logger)
                if review["decision"] != "continue":
                    stopped_after = len(calls)
                    final_text = await tool_review.finish_without_tools(loop, chat, tool_response_parts, review)
                    break
                current_message = tool_review.with_review_note(tool_response_parts, review)

        if final_text is None:
            final_text = json.dumps({
                "agent_id": agent_id, "role": role, "status": "partial", "confidence": 0.0,
                "findings": {}, "sources": [], "recommendation": "",
                "blocked_reason": "Stopped by the user, and gave no final answer.",
            })
            if event_logger:
                event_logger({"event_type": "error", "source": agent_id, "data": "Stopped by the user, and gave no final answer."})

        if event_logger:
            event_logger({
                "event_type": "response_received",
                "source": agent_id,
                "data": {"role": role, "content": final_text}
            })

        cleaned = final_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.strip()
        if not cleaned:
            cleaned = "{}"

        result = json.loads(cleaned)
        result["agent_id"] = agent_id  # ensure it's always set
        result.setdefault("status", "ok")

        # What counts as evidence for a link: this agent's own calls, what they returned,
        # and the research the user already approved in earlier cycles.
        evidence = Evidence([describe_call(c.get("tool", ""), c.get("args") or {}) for c in calls]
                            + list(tool_texts) + ([prior_context] if prior_context else []))
        claimed = result.get("sources") if isinstance(result.get("sources"), list) else []
        sources, unverified, invented = verify_sources(claimed, calls, tool_texts, evidence)
        result["sources"] = sources
        result["tool_calls_made"] = len(calls)
        if stopped_after is not None:
            result["stopped_by_user_after_tool_calls"] = stopped_after
        if unverified:
            result["unverified_sources"] = unverified
        if len(unverified) > len(sources):
            # Most of what it cites came from nowhere its own tools went.
            try:
                result["confidence"] = min(float(result.get("confidence", 0.6)), 0.6)
            except (TypeError, ValueError):
                result["confidence"] = 0.6
            result["confidence_note"] = "Capped: most of the sources it cited were not produced by its own tool calls."
        # Links anywhere else in the answer (findings, recommendation, notes) must also be
        # ones a tool returned; the rest are taken out, not left for the lead to repeat.
        rest = {k: v for k, v in result.items() if k not in ("sources", "recommended_tools")}
        cleaned_rest, removed = strip_unevidenced_links(rest, evidence)
        result.update(cleaned_rest)
        cap_for_invented_links(result, invented + removed)
        result["evidence_links"] = evidence.urls[:EVIDENCE_LINK_LIMIT]
        if declarations and not calls:
            # Nothing it says came from a tool, whatever confidence it reports.
            result["no_tool_calls"] = True
            try:
                result["confidence"] = min(float(result.get("confidence", 0.5)), 0.5)
            except (TypeError, ValueError):
                result["confidence"] = 0.5
        return result
    except Exception as e:
        return {
            "agent_id": agent_id,
            "status": "error",
            "error": str(e),
            "findings": {}
        }
