#!/usr/bin/env python3
"""SessionStart hook: reminds the session of its bound task. Prints nothing when it has none."""
import json
import os
import re
import sys

MAX_CHARS = 1000


def context(event, profile_dir):
    session = event.get("session_id", "")
    if not re.match(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$", session):
        return None
    state = os.path.join(profile_dir, "state")
    binding = os.path.join(state, "sessions", session)
    if not os.path.isfile(binding):
        return None
    with open(binding, encoding="utf-8") as f:
        task_id = f.read().strip()
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", task_id):
        return None
    with open(os.path.join(state, "tasks", task_id + ".json"), encoding="utf-8") as f:
        task = json.load(f)
    text = "Bound task: %s (%s), status %s, version %s.\nNext step: %s" % (
        task_id, task.get("title") or "untitled", task.get("status"), task.get("version"),
        task.get("next_step") or "not set")
    return text[:MAX_CHARS]


def main(argv):
    try:
        profile_dir = argv[argv.index("--profile") + 1]
        text = context(json.load(sys.stdin), profile_dir)
    except Exception:      # a reminder must never break a session
        return 0
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
