"""An empty model reply must never reach the user as a blank JARVIS bubble.

Reproduces what the screenshot showed: Gemini returns a response with no text
and no function calls, coordinator returned "", and /ask pushed that empty
string into the chat and spoke it as silence.
"""
import sys

import jarvis
import coordinator

app = jarvis.app.test_client()


def check(label, cond):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        sys.exit(1)


class FakeCandidate:
    def __init__(self, finish_reason=None):
        self.finish_reason = finish_reason
        self.safety_ratings = []


class FakeResponse:
    """A response carrying no usable text, like the one that produced the blank."""
    text = None
    function_calls = []

    def __init__(self, finish_reason=None, block_reason=None):
        self.candidates = [FakeCandidate(finish_reason)]
        self.prompt_feedback = type("F", (), {"block_reason": block_reason})()


# ---- the describer names the actual reason ---------------------------------
msg = coordinator.describe_empty_model_reply(FakeResponse(finish_reason="MAX_TOKENS"))
check("ran-out-of-room is reported", "room" in msg.lower() and msg.strip() != "")

msg = coordinator.describe_empty_model_reply(FakeResponse(block_reason="SAFETY"))
check("safety block is reported", "safety" in msg.lower())

msg = coordinator.describe_empty_model_reply(FakeResponse())
check("unknown empty reply still says something", len(msg.strip()) > 10)

msg = coordinator.describe_empty_model_reply(object())
check("a malformed response object does not crash", len(msg.strip()) > 10)

# ---- /ask never pushes a blank message -------------------------------------
before = len(jarvis.CONVO)
jarvis.handle_request = lambda text: ""
jarvis.speak = lambda text: None
r = app.post("/ask", json={"text": "Start a pipeline to do something"})
reply = r.get_json()["reply"]
check("/ask substitutes a real message for an empty one", reply.strip() != "")

pushed = jarvis.CONVO[before:]
jarvis_lines = [m for m in pushed if m["role"] == "ai"]
check("the chat bubble is not blank", jarvis_lines and jarvis_lines[-1]["text"].strip() != "")

jarvis.handle_request = lambda text: "   "
r = app.post("/ask", json={"text": "again"})
check("whitespace-only replies are caught too", r.get_json()["reply"].strip() != "")

print("\nAll checks passed.")

# ---- the window must not hide mid-request ----------------------------------
# Substring matching used to treat any sentence containing "exit" or "quit" as a
# goodbye, hiding the window and swallowing the request.
STOP_WORDS = [
    "end the conversation", "end conversation", "goodbye",
    "go to sleep", "exit", "quit", "stop listening",
]

real_goodbyes = ["goodbye", "go to sleep", "exit", "quit", "stop listening",
                 "okay goodbye jarvis", "end the conversation"]
for phrase in real_goodbyes:
    check(f"still stops on: {phrase!r}", jarvis.is_stop_command(phrase, STOP_WORDS))

not_goodbyes = [
    "start a pipeline to make me a research briefing on small language models",
    "that is quite good actually",
    "the script exits early on line ten",
    "add a task to quit smoking next month",
    "put it in a google doc and exit the draft cleanly when finished",
]
for phrase in not_goodbyes:
    check(f"keeps listening through: {phrase[:44]!r}", not jarvis.is_stop_command(phrase, STOP_WORDS))

print("\nAll checks passed.")
