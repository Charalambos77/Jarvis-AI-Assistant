"""
Synthesis Agent — LLM-powered conflict detection and blueprint compression.
Replaces the naive dict-merge approach with structured Gemini calls.
"""
import json
import os
import asyncio
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


async def run_synthesis_agent(authoritative_output: dict | list, event_logger=None) -> dict:
    """
    Takes the Lead Specialist's authoritative cycle output.
    1. Runs LLM conflict detection for internal contradictions.
    2. If no conflicts, compresses into a hyper-dense cycle blueprint.
    
    Returns: {"status": "ok"|"conflict", "blueprint": {...}, "has_conflicts": bool, "conflicts": [...]}
    """
    client = genai.Client(api_key=GEMINI_API_KEY)
    loop = asyncio.get_running_loop()
    
    findings_json = json.dumps(authoritative_output, indent=2)
    
    # 1. Conflict detection
    conflict_prompt = _detect_conflicts_prompt(findings_json)
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
        if conflict_res.get("has_conflicts", False):
            return {
                "status": "conflict",
                "has_conflicts": True,
                "conflicts": conflict_res.get("conflicts", []),
                "message": "Conflicts detected in research."
            }
    except Exception as e:
        print(f"[Synthesis] Error in conflict detection: {e}")
    
    # 2. Compression
    compress_prompt = _compress_blueprint_prompt(findings_json)
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

        blueprint = json.loads(response.text)
        return {
            "status": "ok",
            "has_conflicts": False,
            "blueprint": blueprint
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
        return {
            "status": "ok",
            "has_conflicts": False,
            "blueprint": fallback_blueprint,
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


def _detect_conflicts_prompt(findings_json: str) -> str:
    """System prompt for conflict detection."""
    return f"""Analyze the following research output for internal contradictions.
    
    FINDINGS:
    {findings_json}
    
    Return a JSON object:
    {{
        "has_conflicts": true/false,
        "conflicts": [
            {{
                "description": "what contradicts what",
                "agents_involved": ["agent_id_1", "agent_id_2"],
                "options": [
                    {{"name": "Option A", "pros": "...", "cons": "..."}},
                    {{"name": "Option B", "pros": "...", "cons": "..."}}
                ]
            }}
        ]
    }}
    
    If no contradictions, return {{"has_conflicts": false, "conflicts": []}}"""


def _compress_blueprint_prompt(findings_json: str) -> str:
    """System prompt for blueprint compression."""
    return f"""You are the Synthesis Agent. Compress the following research findings 
    into a single hyper-dense blueprint JSON. Resolve overlapping concepts.
    Keep unique, complementary, high-value strategy details from every source.
    Do NOT arbitrarily discard any agent's unique contributions.
    
    Make sure to preserve and include any 'recommended_tools' arrays from the inputs, grouping them under a top-level "recommended_tools" key in your JSON output.
    
    FINDINGS:
    {findings_json}
    
    Return a single JSON blueprint."""
