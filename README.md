# harness-skeleton

A small harness for [Claude Code](https://code.claude.com/docs): shared rules, a safety gate on tool calls, task checkpoints, memory and a health check. It installs next to your existing `~/.claude`, and one command takes it out again.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/B0yko/harness-skeleton/main/install.sh | bash
```

Or paste this into your agent, which will also set up your profile:

```text
Install the harness from https://github.com/B0yko/harness-skeleton. Read AGENTS.md in that repo and follow it step by step.
```

It runs on macOS and Linux; Windows is not supported. You need git, Python 3.9+ and Claude Code 2.1.295+. There is nothing else to install. To see what the installer would touch, clone the repo and run `python3 tools/harness.py install --dry-run`.

## What's in the tree

```text
install.sh            clones to ~/.harness/core and runs the installer
core/rules/core.md    the rules every session loads
core/agents/          worker and reviewer subagents
core/skills/          task and memory skills
core/hooks/gate.py    the PreToolUse gate
core/hooks/session_start.py   reminds a session of its bound task
core/settings.json    the hooks to merge into your settings
profile-template/     copied once to ~/.harness/profile, then yours
tools/harness.py      the CLI
tests/                unittest suite, no network, temp HOME only
docs/                 architecture and customization
```

## How it works

- Core and profile: `~/.harness/core` is a git clone you can update, and `~/.harness/profile` holds your data and is never overwritten.
- Gate: one PreToolUse hook scans each command as text. It denies a printer or sender (`cat`, `curl` and others) that names a credential file, asks before any other command that names one, and asks before sending text to people or running destructive commands. Heredoc bodies and quoted phrases count as data unless the command can run them. When the scan cannot tell, it asks, so you will see an occasional false question.
- Fail closed: the hook is registered with `onFailure: block`, so a gate that crashes or cannot start blocks the call.
- Subagents: a subagent cannot show a prompt, so where a session would be asked, the subagent is denied and told to hand the call back.
- Weight budget: `harness weight` counts what every session loads and fails when it goes over the limit in `profile.json`.
- Task checkpoints: `harness task` keeps versioned JSON files, and refuses `DONE` without evidence.
- Memory: one `MEMORY.md` index plus a small note per topic. Claude Code's per-project memory stays as it is unless you opt in to one shared memory.
- Doctor: `harness doctor` prints one line per check, with the fix for anything broken.

The gate is a pattern guard and not a sandbox. It catches the common forms of a dangerous call, and a determined process can get past it (a script file that reads a key, for example). Keep real isolation in the OS, containers or Claude Code's own sandbox. [docs/architecture.md](docs/architecture.md) lists what it does not catch.

## What's left out

MCP loaders, browser control, sync between machines, scheduled reviews, model routing and connectors. They depend on your accounts and infrastructure, and they would make this too long to read in one sitting. Add the ones you need in your profile.

## Update and uninstall

```bash
harness update      # git pull --ff-only in the core, then install
harness uninstall   # removes the links, hooks and CLI, keeps ~/.harness
rm -rf ~/.harness   # only if you also want the core clone and your profile gone
```

If you did not change your config after installing and `~/.harness` is still there, uninstall puts `settings.json` back byte for byte. Keys you added later stay. If you removed something from it after installing, uninstall leaves that out and keeps your pre-install copy as `~/.harness/settings.backup.json.1`. Uninstall never deletes your files: it prints the `rm -rf` line above for you to run.

If `~/.harness` was deleted without uninstalling, the hooks stay and block every gated tool call. To recover, run the install command above in a terminal, then `harness uninstall` if you wanted it gone.

## Customize

Edit `~/.harness/profile`: your rules in `rules/profile.md`, extra gate patterns and budgets in `profile.json`, your own skills in `skills/`. Run `harness install` after adding a skill or agent. [docs/customize.md](docs/customize.md) has the details.

## License

[Apache-2.0](LICENSE)
