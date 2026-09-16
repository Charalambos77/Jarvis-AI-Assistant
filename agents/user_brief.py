"""
The user's own brief, as every agent should see it.

The Brain writes each agent a one-line brief of its own. On its own that is a
paraphrase, and paraphrases drift: "competitors of a custom-software studio in
native-English countries" became "research direct competitors", and the agents
filled the gap with Salesforce, SAP and India. So every agent also gets the
user's brief verbatim, with a rule that it wins wherever the two disagree.
"""

# A clarified brief is a few thousand characters; one started inside a section
# also carries the section's knowledge digest. This only bounds a runaway one.
MAX_BRIEF_CHARS = 20000


def clip_brief(user_brief: str | None) -> str:
    """The brief, stripped, and cut down if it is unreasonably long."""
    text = (user_brief or "").strip()
    if len(text) > MAX_BRIEF_CHARS:
        text = text[:MAX_BRIEF_CHARS] + "\n[... brief truncated ...]"
    return text


def user_brief_block(user_brief: str | None) -> str:
    """The brief as a prompt section, or an empty string when there is none."""
    text = clip_brief(user_brief)
    if not text:
        return ""
    return (
        "THE USER'S BRIEF — what the user actually asked for, in their own words:\n"
        f"{text}\n\n"
        "Your own brief is one slice of this job, written by the planner. Where the two differ, "
        "the user's brief wins. Every constraint in it — what kind of thing qualifies, which "
        "places, how many, how things are ranked, what to leave out — applies to your work "
        "exactly as written. Never swap in a broader, easier or more familiar reading of the "
        "user's words. If you cannot meet one of these constraints, say so plainly instead of "
        "quietly relaxing it.\n"
        "If the brief ends with changes the user asked for at a review gate, read each one as a "
        "change TO the brief, not as a replacement FOR it: it alters only what it names, and every "
        "other requirement above still applies. Where a change replaces a requirement, follow the "
        "new version and say so. Where it says something is unclear, ask rather than pick a "
        "reading that suits you.\n"
    )
