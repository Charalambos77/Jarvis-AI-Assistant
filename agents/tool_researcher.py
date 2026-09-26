"""
The research phase for a newly connected service.

A server's own tool descriptions are often one line ("Search issues") and say
nothing about when to use the tool, what its arguments look like, what it
costs, or which tool comes before it. Agents given only that line guess. So
before a new service's tools go up for review, a Tool Researcher reads about
the service on the web and writes a card for each tool:

  * what it does and when to use it, in words an agent can act on;
  * an example of its arguments;
  * its risk (read, write, destructive, costs_money);
  * limits and gotchas (rate limits, pagination, auth scope);
  * which tool usually comes before or after it;

plus a summary of the service and the kinds of task it is for.

The researcher only uses web_search and inspect_website. It never calls the new
service's own tools: nothing on it has been reviewed yet.

What it writes is checked against the real tool list. It can describe tools
but never add one; cards for names the server did not list are dropped. And it
can only raise a tool's risk, never lower it: a tool the name rules call
destructive stays destructive whatever the researcher thinks, because a wrong
"read" would hand every agent a tool nobody reviewed.
"""
import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("JARVIS_TOOL_RESEARCH_MODEL", "gemini-2.5-flash")
# Rounds of tool calls before the researcher is told to write up what it has.
# It is a background job with nobody watching, so it gets a fixed budget
# instead of the Commands page check-ins the pipeline agents have.
MAX_ROUNDS = int(os.getenv("JARVIS_TOOL_RESEARCH_ROUNDS", "6"))
MAX_TOOLS_IN_PROMPT = 60

RISK_ORDER = {"read": 0, "write": 1, "destructive": 2, "costs_money": 2}
RISKS = tuple(RISK_ORDER)


class ResearchFailed(Exception):
    pass


def _tool_lines(tools: dict) -> str:
    lines = []
    for name, tool in list(tools.items())[:MAX_TOOLS_IN_PROMPT]:
        schema = json.dumps(tool.get("parameters") or {}, ensure_ascii=False)[:1200]
        lines.append(f"- {name}\n  server's description: {tool.get('server_description') or tool.get('description') or ''}"
                     f"\n  input schema: {schema}")
    if len(tools) > MAX_TOOLS_IN_PROMPT:
        lines.append(f"- ... and {len(tools) - MAX_TOOLS_IN_PROMPT} more, not shown: leave them out of your answer.")
    return "\n".join(lines)


def build_prompt(service: str, kind: str, tools: dict) -> str:
    return f"""You are Jarvis's Tool Researcher. The user just connected a service, and before its tools are
offered to Jarvis's agents you must find out how the service and each of its tools really work.

SERVICE: {service} ({kind.upper()} server)
ITS TOOLS, exactly as the server listed them:
{_tool_lines(tools)}

HOW TO WORK:
- Use web_search to find the service's official documentation, its README or its API reference, and
  read what each tool does, what its arguments mean, its limits (rate limits, pagination, size limits,
  permissions) and anything that costs money (paid calls, credits, charges).
- Use inspect_website when a documentation page needs reading directly.
- Do not call the service's own tools. You don't have them, and nothing on it is reviewed yet.
- If the web has nothing on a tool, describe it from its name, description and schema, and say so in
  its "limits".

Then answer with ONE JSON object and nothing else, no markdown fences:
{{
  "summary": "one or two sentences: what this service is and what Jarvis can do with it",
  "use_for": ["kinds of task in a pipeline that should use it", "..."],
  "tools": {{
    "<tool name exactly as listed>": {{
      "what": "what it does, one sentence",
      "when": "when an agent should use it, and when not",
      "example_args": {{"...": "arguments that fit its input schema"}},
      "risk": "read | write | destructive | costs_money",
      "limits": "limits and gotchas, or empty",
      "goes_with": "which tool usually comes before or after it, or empty"
    }}
  }},
  "sources": ["only links your tools actually returned"]
}}

RISK means: read = only looks things up; write = creates or changes something that can be changed back;
destructive = deletes, sends, publishes, posts, deploys, or anything that can't be taken back;
costs_money = spends money or paid credits. When unsure, pick the riskier one.
Only write tools that are in the list above. Never invent a tool, an argument or a link."""


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ResearchFailed("the researcher did not return JSON")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise ResearchFailed(f"the researcher's JSON did not parse: {e}")
    if not isinstance(data, dict):
        raise ResearchFailed("the researcher's answer was not a JSON object")
    return data


def _run_model(prompt: str) -> str:
    """The research loop itself: the model with web_search and inspect_website."""
    from google import genai
    from google.genai import types
    from agents.tool_executor import web_search_impl, inspect_website_impl, REGISTRY_TOOLS, ALWAYS_ON_TOOLS

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ResearchFailed("GEMINI_API_KEY is not set")

    declarations = [REGISTRY_TOOLS["google_search"]["declaration"], ALWAYS_ON_TOOLS["inspect_website"]["declaration"]]
    handlers = {"web_search": web_search_impl, "inspect_website": inspect_website_impl}
    client = genai.Client(api_key=api_key)
    chat = client.chats.create(model=MODEL, config=types.GenerateContentConfig(
        system_instruction=prompt,
        tools=[{"function_declarations": declarations}],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    ))

    message = "Research the service and its tools now, then give the JSON."
    for round_no in range(MAX_ROUNDS + 1):
        response = chat.send_message(message)
        if not response.function_calls:
            return response.text or ""
        parts = []
        for fc in response.function_calls:
            args = dict(fc.args) if fc.args else {}
            handler = handlers.get(fc.name)
            try:
                result = handler(project_name="__tool_research__", agent_id="tool_researcher", **args) \
                    if handler else {"status": "error", "error": f"Unknown tool {fc.name}."}
            except Exception as e:
                result = {"status": "error", "error": str(e)}
            text = json.dumps(result, default=str, ensure_ascii=False)[:20000]
            parts.append(types.Part.from_function_response(name=fc.name, response={"result": text}))
        if round_no == MAX_ROUNDS - 1:
            parts.append(types.Part.from_text(
                text="That is all the research time there is. Do not call more tools; give the JSON now."))
        message = parts
    raise ResearchFailed("the researcher kept calling tools and never gave its answer")


def _text(value, limit: int) -> str:
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value or "").strip()[:limit]


def validate(raw: dict, tools: dict) -> dict:
    """Keep only what fits the real tool list; risk can go up, never down.

    Returns {"summary", "use_for", "sources", "cards": {tool: card}}.
    """
    cards = {}
    for name, card in (raw.get("tools") or {}).items():
        if name not in tools or not isinstance(card, dict):
            continue      # a tool the server never listed is not a tool
        heuristic = tools[name].get("risk", "write")
        claimed = card.get("risk") if card.get("risk") in RISKS else heuristic
        if RISK_ORDER[claimed] <= RISK_ORDER.get(heuristic, 1):
            claimed = heuristic   # not higher than the name rules said: their label stands
        example = card.get("example_args")
        cards[name] = {
            "what": _text(card.get("what"), 400),
            "when": _text(card.get("when"), 500),
            "example_args": example if isinstance(example, dict) else {},
            "risk": claimed,
            "limits": _text(card.get("limits"), 400),
            "goes_with": _text(card.get("goes_with"), 200),
        }
    use_for = raw.get("use_for") if isinstance(raw.get("use_for"), list) else []
    sources = raw.get("sources") if isinstance(raw.get("sources"), list) else []
    return {
        "summary": _text(raw.get("summary"), 600),
        "use_for": [_text(u, 120) for u in use_for if u][:8],
        "sources": [s for s in (_text(s, 300) for s in sources) if s.startswith(("http://", "https://"))][:12],
        "cards": cards,
    }


def research(service: str, kind: str, tools: dict) -> dict:
    """Research the given tools (name -> catalogue entry) of one service. Raises ResearchFailed."""
    if not tools:
        return {"summary": "", "use_for": [], "sources": [], "cards": {}}
    raw = _parse_json(_run_model(build_prompt(service, kind, tools)))
    return validate(raw, tools)


def card_description(card: dict, fallback: str) -> str:
    """The description agents see for a researched tool."""
    parts = [card.get("what") or fallback]
    if card.get("when"):
        parts.append(f"When to use: {card['when']}")
    if card.get("example_args"):
        parts.append(f"Example arguments: {json.dumps(card['example_args'], ensure_ascii=False)[:300]}")
    if card.get("limits"):
        parts.append(f"Limits: {card['limits']}")
    if card.get("goes_with"):
        parts.append(f"Goes with: {card['goes_with']}")
    return " ".join(p.rstrip() if p.rstrip().endswith(".") else p.rstrip() + "." for p in parts)
