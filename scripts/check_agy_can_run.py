"""One real build, to confirm an approved command actually runs.

Everything else about the gate is covered by the other two test scripts without
launching anything. This is the one check that needs the real CLI: it asks it to
run a harmless `echo`, which stops at the Commands page like any other command.

    1. Start Jarvis as usual.
    2. Open http://127.0.0.1:5000/commands.html and keep it in front of you.
    3. Run:  python scripts/check_agy_can_run.py
    4. The echo appears on the page. Press Allow once.

If it prints "the command ran", the whole chain works. If it comes back refused,
the message says which permission was missing.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connectors import antigravity

PROJECT = "Zz Command Check"

if not antigravity.is_available():
    print("The Antigravity CLI is not installed, so there is nothing to check.")
    sys.exit(1)

print("Asking the CLI to run one command. Watch the Commands page.\n")
result = antigravity.run(
    "Run the shell command `echo it-works` and report exactly what it printed. "
    "Do not create, edit or delete any files.",
    PROJECT,
    timeout=300,
)

refused = result.get("refused")
print(json.dumps(result, indent=2)[:2000])
print()
if refused:
    print("REFUSED — the command did not run. The CLI said:")
    for line in refused:
        print("  " + line)
    sys.exit(1)
print("The command ran. Approving on the page is now enough to let a build "
      "install and build things.")
