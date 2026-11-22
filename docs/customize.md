# Customize

Everything you change lives in `~/.harness/profile`. Updates never write there.

## Your rules

Edit `rules/profile.md`, or add more `.md` files in `rules/`. They load in every session, so keep them short and check with `harness weight`.

## Gate patterns

`profile.json` takes Python regular expressions:

```json
{
  "gate": {
    "ask": ["\\bnpm publish\\b", "\\bdocker push\\b"],
    "deny": ["(^|[/\\s])finance-archive(/|\\s|$)"]
  }
}
```

A pattern is searched in:

- the raw shell command, and the words of each segment joined by spaces (a segment is what stands between `;`, `|`, `&` and newlines). The raw command keeps quoted text, so a quoted mention also matches: `echo "finance-archive"` is caught too. Leave out a `^` anchor, so `sudo npm publish` matches
- `Read /path`, `Edit /path`, `Write /path` or `NotebookEdit /path` for file tools, and `Grep <path> <glob>`
- the tool name followed by its JSON input for MCP tools

The folder pattern above catches `cat finance-archive/a.csv`, `cd finance-archive`, `ls finance-archive` and a quoted absolute path. Changes apply on the next tool call. A pattern that does not compile, or a `deny` that is not a list, makes the gate ask on every call, and `harness doctor` shows it.

## Skills and agents

Add `skills/<name>/SKILL.md` or `agents/<name>.md` to the profile and run `harness install` to link them. A profile skill or agent with the same name as a core one replaces it.

## Budgets

```json
{ "budgets": { "loaded_chars": 8000, "hooks": 4 } }
```

If `harness weight` goes over, trim first. Raise a budget only when you know what the extra load buys you.

## Memory

By default the installer leaves memory alone, so Claude Code keeps one memory folder per project. To share one memory across all projects, set this in `profile.json` and run `harness install`:

```json
{ "memory": { "shared": true } }
```

**Shared memory means notes from one project load in every project**, so keep it off if you work for separate clients. With `true`, install points `autoMemoryDirectory` at `~/.harness/profile/memory`, unless you already set that key. Setting it back to `false` and running install removes the key again. Claude Code loads the first 200 lines of `MEMORY.md` (up to 25 KB) and opens notes when it needs them. `harness weight` counts memory only in shared mode, because per-project memory depends on the project.

## Changing the core

Fork the repo and set `HARNESS_REPO` to your fork on the first install:

```bash
curl -fsSL https://raw.githubusercontent.com/B0yko/harness-skeleton/main/install.sh | HARNESS_REPO=https://github.com/you/your-fork.git bash
```

`harness update` then pulls from your fork. If the harness is already installed, the installer refuses a different `HARNESS_REPO` and prints the command that switches the clone:

```bash
git -C ~/.harness/core remote set-url origin https://github.com/you/your-fork.git
harness update
```

Run the tests before you rely on a change to the gate:

```bash
python3 -m unittest discover -s tests
```
