"""What the user changes when they reject a gate, read as a change to their brief.

A rejection note used to reach only the Brain's re-plan while every agent still read
the original brief. Recording the note as "the newest instruction wins" fixes that and
breaks something else: the note becomes the brief, and requirements the user never
mentioned quietly disappear.

So the note is read against the brief instead: what it replaces, what it adds, what it
cancels, what it leaves standing, and what it does not settle. Everything it does not
touch still applies exactly as it did.
"""
import asyncio
import json
import os

from google import genai
from google.genai import types
from dotenv import load_dotenv

from agents.user_brief import clip_brief

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

CHANGES_HEADING = "## Changes the user asked for at review gates"
CHANGES_RULE = ("Each change below alters only what it names. Every other requirement in the brief "
                "above still applies exactly as written.")
UNINTERPRETED_NOTE = ("This change has not been broken down yet. Apply it to exactly what it names, "
                      "and keep every other requirement in the brief.")

_FIELDS = ("replaces", "adds", "drops", "still_stands", "unclear")


def _prompt(brief: str, gate_id: str, note: str) -> str:
    return f"""The user rejected the "{gate_id}" gate and said what they want changed.
Work out what their words change about the brief, and what they leave alone.

THE BRIEF AS IT STANDS (it already includes any earlier changes):
{brief}

WHAT THE USER SAID WHEN THEY REJECTED IT:
{note}

Their note is a change TO this brief, never a replacement FOR it. A requirement the note
does not touch still stands, even if the note is about the same subject.

Rules:
- Use the brief's own words for anything you say is replaced or dropped.
- "replaces": a requirement the note changes. Give the old requirement ("was") and what it
  becomes ("now"). Only when the note really does change that requirement.
- "adds": something the note asks for that the brief did not ask for.
- "drops": a requirement the note cancels outright. If the note does not cancel it, it is not here.
- "still_stands": requirements a reader might wrongly assume this note cancels, but which remain
  in force. This is the list that stops the note from being read as the whole job.
- "unclear": anything the note could mean in more than one way, or that now conflicts with a
  requirement in the brief. Say what the conflict is. Never resolve it by guessing.
- Never invent a requirement the user did not state, and never restate the whole brief.

Return a JSON object:
{{
  "replaces": [{{"was": "the requirement as the brief puts it", "now": "what it becomes"}}],
  "adds": ["..."],
  "drops": ["..."],
  "still_stands": ["..."],
  "unclear": ["..."]
}}"""


async def interpret_change(user_brief: str | None, gate_id: str, note: str) -> dict:
    """Read one rejection note against the brief. Returns {"interpreted": bool, ...lists}."""
    reading = {"interpreted": False, **{f: [] for f in _FIELDS}}
    brief = clip_brief(user_brief)
    if not brief or not GEMINI_API_KEY:
        return reading

    config = types.GenerateContentConfig(
        system_instruction="You record what a user's change does to their brief. Output valid JSON only.",
        response_mime_type="application/json",
    )
    loop = asyncio.get_running_loop()
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model="gemini-2.5-flash", contents=_prompt(brief, gate_id, note), config=config
            )
        )
        parsed = json.loads(response.text)
        if not isinstance(parsed, dict):
            raise ValueError("the reading was not a JSON object")
    except Exception as e:
        print(f"[Gate change] Could not work out what the {gate_id} change does to the brief: {e}")
        return reading

    for field in _FIELDS:
        value = parsed.get(field)
        if not isinstance(value, list):
            continue
        if field == "replaces":
            reading[field] = [
                {"was": " ".join(str(v.get("was", "")).split()), "now": " ".join(str(v.get("now", "")).split())}
                for v in value if isinstance(v, dict) and str(v.get("now", "")).strip()
            ]
        else:
            reading[field] = [" ".join(str(v).split()) for v in value if str(v).strip()]
    reading["interpreted"] = True
    return reading


def render_change(gate_id: str, note: str, reading: dict) -> str:
    """One change, as it is written into the brief."""
    lines = [f"### At the {gate_id} gate the user said:", f"> {note}", ""]
    if not reading.get("interpreted"):
        lines.append(UNINTERPRETED_NOTE)
        return "\n".join(lines) + "\n"

    for change in reading.get("replaces") or []:
        lines.append(f"- Replaces: \"{change['was']}\" is now: {change['now']}")
    for item in reading.get("adds") or []:
        lines.append(f"- Adds: {item}")
    for item in reading.get("drops") or []:
        lines.append(f"- No longer wanted: {item}")
    for item in reading.get("still_stands") or []:
        lines.append(f"- Unchanged by this, still required: {item}")
    for item in reading.get("unclear") or []:
        lines.append(f"- Unclear, ask the user rather than assume: {item}")
    if len(lines) == 3:
        lines.append(UNINTERPRETED_NOTE)
    return "\n".join(lines) + "\n"


async def record_gate_change(brief_path: str | None, user_brief: str | None,
                             gate_id: str, note: str) -> str:
    """Write a rejection note, and what it changes, into the brief. Returns the new brief."""
    note = " ".join((note or "").split())
    if not note:
        return user_brief or ""

    reading = await interpret_change(user_brief, gate_id, note)
    entry = render_change(gate_id, note, reading)

    def with_entry(text: str) -> str:
        text = (text or "").rstrip()
        if CHANGES_HEADING in text:
            return f"{text}\n\n{entry}"
        return f"{text}\n\n{CHANGES_HEADING}\n{CHANGES_RULE}\n\n{entry}"

    if brief_path:
        # On disk too: a resumed pipeline re-reads the brief from there.
        try:
            with open(brief_path, "r", encoding="utf-8") as f:
                on_disk = f.read()
            with open(brief_path, "w", encoding="utf-8") as f:
                f.write(with_entry(on_disk))
        except OSError as e:
            print(f"[Gate change] Could not record the change in the brief file: {e}")
    return with_entry(user_brief)
