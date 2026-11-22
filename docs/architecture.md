# Architecture

## Layout on disk

```text
~/.harness/core/          git clone of this repo, replaced by updates
~/.harness/profile/       your rules, memory, skills, budgets and task state
~/.harness/managed.json   the manifest: everything the installer created
~/.claude/                your config, plus links into the two folders above
~/.local/bin/harness      link to tools/harness.py
```

The split means an update cannot touch your data, and your edits cannot conflict with an update.

## Install

`harness install` plans first and writes afterwards:

1. Work out every link it needs and read `settings.json`.
2. Refuse if any path holds something that is not ours, naming it. Nothing has been written at this point.
3. Run the gate's self-test on the `python3` from `PATH`, the one the hook will use.
4. Write the manifest first, so even a hard kill leaves a record uninstall can follow.
5. Create the profile (built beside its place and renamed, so it is whole or absent), then the links.
6. Back up `settings.json` (mode 0600) and write the merged version last. A `~/.harness` the installer creates is 0700; an existing one keeps its mode.

If a first install fails, what it wrote is taken out again. After a hard kill, the next `install` finishes the job and `uninstall` still works. A lock on the home folder keeps parallel runs from colliding. A second run finds nothing to do and writes nothing. A write-protected or hard-linked `settings.json` is refused rather than replaced.

The hook command is `/usr/bin/env python3 -I -S <gate>`, with no pinned interpreter, so it survives a removed venv or a Homebrew upgrade. If no `python3` is on `PATH`, `onFailure: block` still blocks the call, and `harness doctor` reports a Python below 3.9 as a failure.

Uninstall reads the manifest. It removes a link only if it still points where the installer left it. It takes our hooks and our memory setting out of `settings.json`. If what remains equals the backup, the backup's bytes go back, so formatting survives too. If you added keys since, they stay. The manifest goes last, so an interrupted uninstall can simply run again.

Uninstall never deletes your files. `~/.harness` stays whole, with the core clone and your profile, and uninstall prints the one command that removes it. Installing again picks both up. If the folder was deleted by hand, the hooks are still in `settings.json` and block every gated call; the block message and the README name the fix, which is to run the install command again.

## The gate

`core/hooks/gate.py` is one command registered on `Bash`, `Monitor`, `Read`, `Edit`, `Write`, `NotebookEdit`, `Grep` and all MCP tools. It runs these checks and returns the strongest answer (deny, then ask, then silence):

| Check | Answer | Catches |
|---|---|---|
| credentials | deny | a printer or sender in a segment that names a secret: `cat`, `tac`, `nl`, `head`, `tail`, `less`, `more`, `most`, `bat`, `sort`, `cut`, `tr`, `uniq`, `strings`, `xxd`, `od`, `hexdump`, `base32`, `base64`, `dd`, `zcat`, `diff`, `curl`, `wget`, `scp`, `sftp`, `rsync`, `nc`, `ncat`, `netcat`, `socat`, `git show`, `git cat-file`, `git diff`, `git blame`, `git log -p`. Also Read, Edit, Write, NotebookEdit or Grep on a secret |
| credentials | ask | anything else naming a secret: `grep`, `rg`, `sed` and `awk` (the name may be the pattern), `cp .env.example .env`, `node --env-file=.env`, `git rm --cached .env`, `find . -name .env`, `f=.env; cat $f`. Also a printer or sender followed by `$(...)`, because the gate cannot see which file that is |
| consent | ask | `gh` commands that create, comment on, review or edit an issue, PR, release or gist (also through `gh api`), chat webhooks (a known host or a `$..WEBHOOK..` variable in a command with an HTTP client), mail commands, MCP tools that send, post, reply, publish, DM or notify, or that create, update, save or forward an issue, PR, ticket, message, comment or review |
| destructive | ask | `rm` with recursive and force, `git push --force` (also `--mirror` or a `+ref`), `git reset --hard`, `git clean -f`, `DROP TABLE`, `DROP DATABASE`, `DROP SCHEMA`, `terraform destroy`, `kubectl delete` |
| profile | either | your patterns in `profile.json`, matched against the raw command and each segment's words |

### How the scan works

The gate does not parse shell. It reads the command as text in five steps.

1. Remove data. Heredoc bodies, comments and quoted phrases are taken out, so `git commit -m "rm -rf"` passes. If the command can run text, nothing is removed and the whole raw command is scanned. That covers a shell (`bash`, `/bin/sh`, but not `deploy.sh`), `eval`, `source`, `xargs`, `ssh`, `watch`, `parallel`, `coproc`, `find -exec`, and an interpreter given inline code (`-c`, `-e`, `-p`) or reading stdin. A double-quoted string with `$(` in it stays. So does a quoted word without spaces (`cat ".env"`) and a quoted path with spaces (`cat "/my dir/.env"`).
2. Split. The text is cut into segments at `;`, `|`, `&` and newlines, and each segment into words at whitespace, quotes, parentheses, `<`, `>`, `=`, `,`, `:`, `@` and `$(`. URLs are dropped before the credential check, so `curl https://example.com/Release.key` names no local file. `file://` URLs stay.
3. Credentials. Each segment is checked with the table above.
4. Destructive and consent. Regexes run on each segment's words. The SQL, webhook and GraphQL checks need quoted text, so they read the raw command.
5. Profile patterns. Yours run last, on the raw command and on each segment's words.

Secret files are SSH private keys, `.env` files, `.netrc`, cloud and Claude Code credential files and similar. `.key`, `.p12`, `.pfx` and `.ppk` are always secret. A `.pem` is secret unless its name marks it public (`ca`, `cert`, `chain`, `pub`) and does not contain `key`.

`process.env`, `process.env.NODE_ENV` and a jq path such as `.data.key` or `.items[].key` are read as code unless a file of that name exists in the working directory. `~/.claude/settings.json` is not treated as a secret, because agents read and edit it in normal work, so do not keep tokens there if that matters to you.

Three things pass although they name a secret. A key used without being printed: `-i` for `ssh`, `scp` and `sftp`, `--key`, `--cert`, `--cacert` and `--ca-certificate` for `curl` and `wget`, and `-E` for `curl`, when that program is the first one in the segment (`sudo`, `time` or a `VAR=value` in front is fine). An exclusion: `--exclude=.env`, `--exclude .env` for `rsync`, `tar`, `diff` and `grep` (not after `--`), a git pathspec such as `':(exclude).env'` or `':!.env'`, and `-x .env` for `diff` and `zip`. The search text of `git log --grep=.env`.

An unquoted glob is expanded against the call's real working directory and answered by what it matches, so `du -sh *` passes in a clean folder. It follows the shell on dotfiles: `*` skips `.env`, so `grep foo *` passes beside one, while `cat .*` is denied. A glob whose own text names a secret (`*.env`) counts even with no match.

### The trade-off

The scan prefers a false question to a missed call. When it cannot tell data from code, it treats the text as code and you get one extra prompt. These ordinary commands ask for that reason:

- `git diff $(git merge-base main HEAD)`: a printer followed by `$(...)`
- `echo rm -rf build`: the words look like the command
- `python3 -m app.env`: a dotted module name looks like a `.env` file
- `zip -r a.zip . -x '*.git*' '.env'`: only the first value after `-x` counts as an exclusion
- `grep -rn "rm -rf" . | xargs wc -l`: `xargs` runs text, so the quoted pattern counts

How failure is handled:

- Input it cannot read, or an error inside the gate, answers `ask`.
- In a subagent every `ask` becomes `deny`, because nobody would see the prompt. The reason carries the exact call to hand back.
- If the hook cannot start or times out, `onFailure: block` makes Claude Code block the call. This needs Claude Code 2.1.295 or later, which `harness doctor` checks.
- If no `python3` is on `PATH`, every gated call is blocked until one is.
- A command over 200,000 characters, or with more than 5,000 distinct words or segments, answers `ask`.

### What the gate does not catch

It reads the command text and cannot know what a program will do. Each of these gets through:

- a script file that reads a key: `python3 read_key.py`
- a path built at run time: `f=.en; cat ${f}v`
- an abbreviated long flag: `rm --rec --for t`, `git reset --har`
- a command name kept in a variable: `R=rm; $R -rf target`, `${SHELL:-sh} -c 'rm -rf target'`
- a name written with escapes: `cat $'\x2eenv'`
- encoded commands: `echo cm0gLXJmIHg= | base64 -d | sh`
- a renamed binary: `cp /bin/rm r; ./r -rf x`
- brace expansion: `cat {.env,a}`
- a directory change before a read: `cd ~/.config/gh && cat hosts.yml`, or `cd sub && cat 'my dir/.env'`
- text a program runs by itself: `awk 'BEGIN{system("rm -rf x")}'`, `env -S 'rm -rf x'`, a git alias set with `git -c`, `git rebase -x`, `npx -c`, `su -c`, `tmux new`
- a `#` inside a parameter expansion, read as a comment: `echo ${x:- #}; rm -rf x`
- a name split by a shell variable: `cat${IFS}.env`
- a GraphQL mutation kept in a file: `gh api graphql -F query=@m.graphql`
- `grep -r TOKEN .` through Bash, which reads a `.env` inside the directory it is given
- `shopt -s dotglob; cat *` or `setopt globdots`, after which `*` also matches dotfiles
- a `Grep` over a whole directory, which can reach a secret file that ignore rules do not exclude (`Glob` is not gated at all, because it returns names only)
- a program that sends text over the network by itself
- the PowerShell tool, an opt-in tool on macOS and Linux that the hook does not match

Use it to stop an agent's honest mistakes. For untrusted code, use OS permissions, a container or Claude Code's sandbox.

## Weight

`harness weight` adds up what each session loads before any work starts: the config dir's `CLAUDE.md` (without following `@` imports), every rule file, the first 200 lines of a shared `MEMORY.md`, and the name and description of each skill and agent. Per-project memory is not counted, because its size depends on the project. It also counts hooks. Limits live in `profile.json`.

## Tasks

A task is one JSON file in `~/.harness/profile/state/tasks/`. Writes take a file lock and replace the file atomically. `--expect N` makes a write conditional on the version you read, and a mismatch exits 3. A binding belongs to one session (`CLAUDE_CODE_SESSION_ID`): when that session is resumed, the SessionStart hook reminds it of the task and next step. A new session runs `harness task list` and binds.
