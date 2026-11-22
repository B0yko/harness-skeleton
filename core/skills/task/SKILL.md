---
name: task
description: Track work that spans sessions in a versioned checkpoint. Use to start, resume, update or close a task.
---

# Task checkpoints

Checkpoints are JSON files in `~/.harness/profile/state/tasks/`. Each has a version, status, next step and evidence.

```bash
harness task new fix-login --title "Fix the login redirect" --next "Reproduce it"
harness task bind fix-login      # binds this session only; resuming it brings the reminder back
harness task show                # the bound task
harness task list
harness task set fix-login --expect 1 --status RUNNING --next "Write the failing test"
harness task set fix-login --expect 2 --status DONE --evidence "tests/test_login.py: 14 passed"
```

- Statuses: `TODO`, `RUNNING`, `BLOCKED`, `DONE`, `CANCELLED`.
- Pass `--expect` with the version you read. Exit 3 means someone else wrote first: read it again, then retry.
- `DONE` is refused without evidence. Evidence is something a reader can check: test output, a file, a URL.
- Update the checkpoint after each significant step, so a fresh session can continue from it.
- A new session has no binding: run `harness task list`, then `harness task bind <id>`.
