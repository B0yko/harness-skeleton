# Instructions for agents

This repo is a small harness for Claude Code. If the user asked you to install or configure it, follow the steps below in order. If you are here to change the code, read `docs/architecture.md` and run `python3 -m unittest discover -s tests`.

Three rules hold throughout:

- Never ask for a password, token or API key, and never read one. The harness does not need any.
- Never delete or overwrite a file in the user's config to get past a refusal. Show it to the user and let them decide.
- Honor `CLAUDE_CONFIG_DIR`. If it is set, it replaces `~/.claude` in every step.

## 1. Check prerequisites

```bash
git --version
python3 --version    # 3.9 or later
claude --version     # 2.1.295 or later
```

If git or Python is missing or too old, stop and tell the user what to install. An older Claude Code ignores `onFailure`, so a broken gate would let calls through: ask the user to run `claude update` first.

## 2. Inspect the existing config

```bash
config="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
ls -la "$config" "$config/rules" "$config/skills" "$config/agents" 2>/dev/null
ls -la "$HOME/.harness" 2>/dev/null
python3 -c 'import json,sys; print(sorted(json.load(open(sys.argv[1]))))' "$config/settings.json" 2>/dev/null || echo "no readable settings.json"
```

The last line prints only the top-level key names. Settings can hold tokens under `env`, so do not print the values.

## 3. Dry run

```bash
tmp="$(mktemp -d)"
git clone --quiet --depth 1 https://github.com/B0yko/harness-skeleton.git "$tmp/harness"
python3 "$tmp/harness/tools/harness.py" install --dry-run
```

This writes nothing outside the temp folder, which you can leave for the OS to clear. Link targets in the output point at the temp clone, and the real install points them at `~/.harness/core`. If it refuses because a path is already taken, stop and show the user that path.

## 4. Install

```bash
curl -fsSL https://raw.githubusercontent.com/B0yko/harness-skeleton/main/install.sh | bash
```

The CLI is now at `~/.local/bin/harness`. If that folder is not on `PATH`, use the full path and tell the user.

## 5. Ask the user

Ask these in one message, at most once. Skip any the user already answered, and accept "skip" for any of them.

1. What should I call you?
2. Which language should I use with you?
3. What do you work on?
4. Who may I message on your behalf, if anyone? (I still show you the exact text first.)
5. Is there anything I must never touch, such as a folder, a repo or a system?

## 6. Fill in the profile

Write the answers into `~/.harness/profile/rules/profile.md`, one line each. Leave out anything the user skipped.

For answer 5, also add a pattern to `gate.deny` in `~/.harness/profile/profile.json` when the answer is a path or a command. Patterns are Python regular expressions. For a folder:

```json
{ "gate": { "ask": [], "deny": ["(^|[/\\s])finance-archive(/|\\s|$)"] } }
```

This form also catches relative paths. Keep the `budgets` and `memory` keys as they are. Leave `memory.shared` at `false` unless the user asks for one memory across all projects: with `true`, notes from one project load in every project. A pattern that does not compile makes the gate ask on every call, so run step 7 after each edit.

## 7. Run doctor

```bash
harness doctor
harness weight
```

Fix every `FAIL` using the fix printed on its line. A `WARN` for weight means the user's own config is over the budget: report it and leave their files alone.

## 8. Prove one gate works

Claude Code applies hook changes to a running session, so the gate is already active here. Test it in a fresh session too, since that is what the user will run. The gate matches by name, so the file does not need to exist and no secret is involved:

```bash
demo="$(mktemp -d)"
claude -p --no-session-persistence --allowedTools Bash -- "Run exactly this command: cat $demo/.env. If a hook blocks it, quote the block reason word for word."
```

It passed if the reply contains `harness gate`.

If `claude -p` cannot run because this environment has no login, use the `gate` line of `harness doctor` from step 7 as the proof: it feeds real events to the real hook. Tell the user which proof you used.

## 9. Report

Tell the user:

- what was installed and where
- the output of `harness doctor`
- the result of the gate test, and which proof you used
- anything you refused to change, and why
- how to undo it: `harness uninstall` takes out the links and hooks and keeps `~/.harness`; it prints the one command that deletes that folder
