#!/usr/bin/env python3
"""The PreToolUse gate: a plain text scan of each call. A pattern guard, not a sandbox.

A printer or sender naming a secret is denied. Anything else naming one, sending text to people
and destructive commands ask, then come the patterns in profile.json. The strongest answer wins.
A subagent cannot show a prompt, so there an ask becomes a deny. What cannot be read asks.
Heredoc bodies and quoted phrases are data unless the command can run text.
"""
import json
import os
import re
import sys
from glob import escape, iglob
from itertools import islice

# Past these the gate asks. A word longer than MAX_PATH cannot be a file.
MAX_COMMAND, MAX_WORDS, MAX_SCANNED, MAX_PATH = 200000, 5000, 20000, 4096
MAX_GLOBS = MAX_HEREDOCS = 64
join = os.path.join

# What can run text it is given: with one of these, nothing in the command counts as data.
RUNNERS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "eval", "source", "xargs",
           "ssh", "watch", "parallel", "coproc", "trap", "$shell", "${shell}", "$bash", "${bash}"}
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
INTERPRETER = re.compile(r"(python[\d.]*|pypy3?|node|deno|bun|perl|ruby|php)$")
INLINE_CODE = re.compile(r"-\w*[ceE]\w*$|-[rp]$|--eval$|--print$|-$")

SQL_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "clickhouse-client", "cockroach",
               "duckdb"}
HTTP = {"curl", "wget", "http", "https", "xh"}
# These print a file or send it elsewhere: naming a secret to them is a deny.
PRINTERS = {"cat", "tac", "nl", "head", "tail", "less", "more", "most", "bat", "batcat", "sort",
            "cut", "tr", "uniq", "strings", "xxd", "od", "hexdump", "base32", "base64", "dd",
            "zcat", "gzcat", "diff", "curl", "wget", "scp", "sftp", "rsync", "nc", "ncat",
            "netcat", "socat"}
# Files these programs use without printing them, or skip. For wget, -E takes no value.
TLS_KEY_FLAGS = {"--key", "--cert", "--cacert", "--ca-certificate"}
KEY_FLAGS = {"ssh": {"-i"}, "scp": {"-i"}, "sftp": {"-i"}, "curl": TLS_KEY_FLAGS | {"-E"},
             "wget": TLS_KEY_FLAGS | {"--certificate", "--private-key"},
             "diff": {"-x", "--exclude"}, "zip": {"-x"},
             "rsync": {"--exclude"}, "tar": {"--exclude"}, "gtar": {"--exclude"},
             "grep": {"--exclude", "--exclude-dir"}}
# Words that may stand before a segment's own program.
LEADERS = {"sudo", "doas", "time", "nice", "ionice", "nohup", "timeout", "env", "command", "exec",
           "stdbuf", "xargs", "if", "then", "else", "elif", "do", "while", "until", "!", "{"}

SSH_PUBLIC = re.compile(r"(\.pub|^known_hosts.*|^authorized_keys|^config)$")
SECRET_NAMES = {".envrc", ".netrc", ".pgpass", ".pypirc", ".git-credentials", ".npmrc", ".my.cnf",
                ".vault-token", "credentials.json", ".credentials.json", "secring.gpg",
                "application_default_credentials.json", "msal_token_cache.json",
                "settings.backup.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".aws",
                ".gnupg", "credentials.toml", "credentials.tfrc.json", "terraform.tfstate.backup"}
SECRET_PATH = re.compile(r"(\.aws/credentials|\.docker/config\.json|\.kube/config|gh/hosts\.yml|"
                         r"gcloud/credentials\.db|\.config/git/credentials|"
                         r"settings\.backup\.json\.\d+)$|"
                         r"\.aws/sso/cache/|\.gnupg/private-keys-v1\.d/")
ENV_FILE = re.compile(r"^(\.env([._-][\w.-]+)?|[\w. -]+\.env"
                      r"(\.(local|bak|backup|old|orig|prod|production|dev|development|staging))?)$")
ENV_SAFE = re.compile(r"[._-](example|sample|template|tpl|dist|defaults)$|"
                      r"^(example|sample|template|defaults?)\.env$")
KEY_SUFFIXES = (".key", ".p12", ".pfx", ".ppk", ".tfstate")
# Not files unless one exists here: `process.env` in code, a jq field such as `.data.key`.
FIELD = re.compile(r"(process|import\.meta|deno|bun)\.env(\.\w+)*$|\.(\w+\.)*key$|\.(\w+\.)+env$|"
                   r"\.[\w.]*(\[\d*\][\w.]*)+$", re.I)
PEM_PUBLIC = re.compile(r"(^|[^a-z0-9])(ca|pub|public)([^a-z0-9]|$)|cert|chain")
GLOB_CHARS = re.compile(r"[*?\[]")

WEBHOOK = re.compile(r"hooks\.slack\.com|slack\.com/api/chat\.|discord(app)?\.com/api/webhooks|"
                     r"api\.telegram\.org/bot|webhook\.office\.com|office\.com/webhook|"
                     r"chat\.googleapis\.com|smtps?://|\$\{?\w*WEBHOOK", re.I)
GH_SEND = re.compile(r"\b(issue|pr) (create|comment|review|edit)\b|"
                     r"\b(release|gist) (create|edit)\b|"
                     r"^(?=.*\b(issue|pr) (close|reopen|merge)\b)"
                     r"(?=.* (-[cbF]\S*|--comment|--body(?:-file)?)(?: |$))")
GH_API = re.compile(r" api(?: |$)")
GH_SEND_PATH = re.compile(r"[ /](comments|reviews|replies|releases|gists)\b|"
                          r"[ /](issues|pulls|discussions)(/\d+)?/?(?: |$)")
GH_MUTATION = re.compile(r"comment|review|issue|discussion|pull_?request|release", re.I)
DROP = re.compile(r"\bdrop\s+(table|database|schema)\b", re.I)
MCP_SEND = {"send", "post", "comment", "reply", "publish", "forward", "dm", "notify"}
MCP_WRITE = {"create", "update", "add", "edit", "submit", "open", "save"}
MCP_OBJECT = {"issue", "pull", "pr", "ticket", "message", "comment", "review", "email", "mail"}
MCP_READ = {"get", "list", "read", "search", "fetch", "find", "query", "describe", "view", "count"}


def has(names):
    """A lookahead from the start of the text for one of these names as a whole word."""
    return r"(?=(?:.*[ /])?(?:%s)(?: |$))" % names


MAIL = re.compile(r"(?:^|(?:sudo|doas|env|exec|nohup|time|command|xargs|then|do) )(?:\S*/)?"
                  r"(?:mailx?|s-nail)(?: |$)|(?:^|[ /])(?:sendmail|mutt|neomutt|msmtp|swaks)"
                  r"(?: |$)|^" + has("git") + has("send-email"))
GIT_PRINTS = re.compile("^" + has("git") + "(?:" + has("show|cat-file|diff|blame|annotate")
                        + "|" + has("log") + has("-p|-u|--patch") + ")")
# Anchored lookaheads: each scans the text once, so no input can make them slow.
DESTRUCTIVE = {name: re.compile("^" + rx) for name, rx in {
    "rm -rf": has("rm") + r"(?=.* (-\w*[rR]\w*|--recursive)\b)(?=.* (-\w*f\w*|--force)\b)",
    "git push --force": has("git") + has("push") + r"(?=.* (-\w*f\w*|--force|--mirror|\+(?!%)\S+))",
    "git reset --hard": has("git") + has("reset") + r"(?=.* --hard\b)",
    "git clean -f": has("git") + has("clean") + r"(?=.* (-\w*f\w*|--force)\b)",
    "terraform destroy": has("terraform|tofu") + has("-?destroy"),
    "kubectl delete": has("kubectl") + has("delete")}.items()}

# Self-test table, also fed to the real hook command by `harness doctor`. Never run.
SAMPLES = [
    ("Bash", {"command": "git status"}, None),
    ("Bash", {"command": 'git commit -m "rm -rf"'}, None),
    ("Read", {"file_path": "/tmp/p/.env"}, "deny"),
    ("Bash", {"command": "cat ~/.ssh/id_rsa"}, "deny"), ("mcp__chat__send_message", {}, "ask"),
    ("Bash", {"command": "gh issue comment 1 --body hello"}, "ask"),
    ("Bash", {"command": "bash -c 'git push --force'"}, "ask"),
]

# What matters in code, and inside a double-quoted string that holds code.
IN_CODE = re.compile(r"""\\.\#?|\$'(?:[^'\\]|\\.)*'|'[^']*'|"(?:[^"\\$]|\\.|\$(?!\())*"
    |(?<![^\s;&|(])\#[^\n]*|(?<!<)<<-?[ \t]*(?:'[^'\n]*'|"[^"\n]*"|\\?[\w.+-]+)(?=[ \t\n;|&<>)]|$)
    |\$\(\(?|\(\(?|\)\)?|["'\n]""", re.S | re.X)
IN_STRING = re.compile(r"""\\.|\$\(\(?|\"""", re.S)
BACKTICKS = re.compile(r"`([^`]*)`")
OPENER = re.compile(r"""^<<-?[ \t]*\\?|['"]""")
SEGMENT = re.compile(r"[;|&\n]")
ASSIGNMENTS = re.compile(r"\s*(?:[A-Za-z_]\w*=\S*\s+)+")
WORD_BREAK = re.compile(r"""[\s'"`()<>=,:@]+|\$\(""")
# Not a local file: a URL (file:// is one) and the value of --exclude= or git log --grep=.
NOT_A_FILE = re.compile(r"""(?i)(?<![a-z0-9+.-])(?!file:)[a-z][a-z0-9+.-]*://[^\s$`'"<>]*|"""
                        r"""(?:--exclude=|--grep=|(?<![\w.-]):(?:\(exclude\)|!|\^))"""
                        r"""(?:"[^"$`]*"|'[^']*'|[^\s"'$`;|&<>]*)|"""
                        r"""(?<![\w.-])[\w.-]+\.(?:env|key)(?==)""")   # the name in --set a.env=b


def base(name):
    return os.path.basename(name).lower()


def split_words(text):
    return [w for w in WORD_BREAK.split(text) if w]


def phrase(text, cwd):
    """A quoted string: a word stays, a path stays as one word, any other phrase is data."""
    if not re.search(r"\s", text):
        return text
    is_path = text.startswith(("/", "~", "./", "../", "$")) or os.path.lexists(join(cwd, text))
    return re.sub(r"\s+", "_", text) if is_path else ""


def skip_bodies(s, i, openers, out):
    """Step over the heredoc bodies starting at s[i]. Returns (next index, all closed)."""
    for opener in openers:
        tabs = r"\t*" if opener.startswith("<<-") else ""
        end = re.compile("^%s%s$" % (tabs, re.escape(OPENER.sub("", opener))), re.M).search(s, i)
        if not end:
            return i, False
        if not re.search(r"""['"\\]""", opener) and "$(" in s[i:end.start()]:
            out.append(s[i:end.start()])        # an unquoted delimiter: the body runs its $(...)
        i = min(end.end() + 1, len(s))
    return i, True


def remove_data(s, cwd):
    """Heredoc bodies, comments and quoted phrases out. Returns (text, whether all closed)."""
    out, stack, openers, i, arithmetic, heredocs = [], [], [], 0, 0, MAX_HEREDOCS
    while True:
        in_string = stack[-1:] == ['"']
        m = (IN_STRING if in_string else IN_CODE).search(s, i)
        if not m:
            return "".join(out) + s[i:], not stack
        out.append(s[i:m.start()])
        i, token = m.end(), m.group()
        if token[0] == "\\":
            out.append(token[1:].replace("\n", ""))
        elif token == '"' and in_string:
            stack.pop()
        elif token in ("'", '"'):               # a string that holds code, or never closes
            stack.append('"')
        elif token[0] in "'\"" or token.startswith("$'"):
            value = phrase(token.lstrip("$")[1:-1], cwd)
            around = s[m.start() - 1:m.start()] + s[i:i + 1]
            alone = not around.strip(" \t\n'\"")
            out.append(value or ("_" if alone else ""))    # a dropped word keeps its place
        elif token == "\n":
            out.append(token)
            i, closed = skip_bodies(s, i, openers, out)
            heredocs, openers = heredocs if closed else 0, []
        elif token.startswith("<<") and heredocs and not arithmetic:
            heredocs -= 1
            openers.append(token)
            out.append("<<")
        elif token[0] != "#":
            if token.endswith("("):
                arithmetic += token.endswith("((")
                stack += ["("] * token.count("(")
            elif token[0] == ")":
                arithmetic -= bool(arithmetic) and token == "))"
                for _ in token:
                    if stack[-1:] == ["("]:
                        stack.pop()
            out.append(token)


def runs_text(words, segment):
    """Whether this segment can run text it was given, so that no part of it is data."""
    names = set(map(base, words))
    if words[:1] == ["."] or names & RUNNERS or ("find" in names and names & FIND_EXEC):
        return True
    only_flags = all(w.startswith("-") or w == "_" for w in words[1:])   # no script: reads stdin
    reads_code = only_flags or re.search("<[<(]", segment) or any(map(INLINE_CODE.match, words))
    return reads_code and any(map(INTERPRETER.match, names))


def secret_path(path):
    p = path.lower().rstrip("/")
    name = p.rsplit("/", 1)[-1]
    if ".ssh" in p.split("/"):
        return name == ".ssh" or not SSH_PUBLIC.search(name)
    if ENV_FILE.match(name):
        return not ENV_SAFE.search(name)
    if name.endswith(".pem"):
        return "key" in name or not PEM_PUBLIC.search(name[:-4])
    return (name in SECRET_NAMES or name.endswith(KEY_SUFFIXES)
            or bool(SECRET_PATH.search(p + "/" if p.endswith("cache") else p)))


def secret_file(path, cwd):
    """The path itself, or what a symlink there points at."""
    return secret_path(path) or secret_path(os.path.realpath(join(cwd, os.path.expanduser(path))))


def pattern_is_secret(pattern):
    """A glob whose own text is a secret name: `*.env`, `.env*`, `~/.ssh/*`."""
    return any(secret_path(re.sub(r"\*+|\?|\[[^\[\]]*\]", fill, pattern)) for fill in ("", "x"))


def glob_hits_secret(pattern, cwd):
    """Whether a glob really matches a secret. As in the shell, `*` skips dotfiles."""
    full = join(escape(cwd), os.path.expanduser(pattern))
    matches = list(islice(iglob(full), MAX_SCANNED + 1))
    return len(matches) > MAX_SCANNED or any(secret_file(match, cwd) for match in matches)


def names_secret(text, cwd):
    """Whether a word names a secret: by its text, a link it is, or what its glob matches."""
    part = text[2:] if re.match(r"-[^-]", text) else text       # -o.netrc
    if not part or part.startswith("-") or len(part) > MAX_PATH:
        return False
    if FIELD.match(part) and not os.path.lexists(join(cwd, part)):
        return False
    if GLOB_CHARS.search(part):
        return pattern_is_secret(part) or ("$" not in part and glob_hits_secret(part, cwd))
    return secret_path(part) or ("$" not in part and secret_file(part, cwd))


def key_values(words, skip=0):
    """Indexes of values the segment's own program uses or skips without printing them."""
    known = PRINTERS.union(KEY_FLAGS)
    first = base(words[skip]) if skip < len(words) else ""
    if first not in known and first not in LEADERS:
        return set()                   # `cp ssh -i .env x`: the program here is cp
    start = next((i for i in range(skip, len(words)) if base(words[i]) in known), None)
    if start is None:
        return set()
    flags = KEY_FLAGS.get(base(words[start]), ())
    stop = words.index("--", start) if "--" in words[start:] else len(words)
    own = range(start, stop)
    return {i + 1 for i in own if words[i] in flags and words[i - 1] not in flags} | {
        i for i in own if words[i][:2] in flags and words[i] not in flags}


def prints_files(words):
    return bool(PRINTERS.intersection(map(base, words)) or GIT_PRINTS.match(" ".join(words)))


def credential_hits(local, segment, secret):
    prefix = ASSIGNMENTS.match(NOT_A_FILE.sub(" ", segment))    # X=ssh rsync runs rsync
    keys = key_values(local, len(split_words(prefix.group())) if prefix else 0)
    named = [w for i, w in enumerate(local) if w in secret and i not in keys]
    if named:
        level = "deny" if prints_files(local) else "ask"
        return [(level, "credentials: this touches a secret file (%s)" % named[0])]
    before = re.match(r"(.*)\$\((?!\()", segment, re.S)      # $((...)) is arithmetic
    if before and prints_files(split_words(before.group(1))):
        return [("ask", "credentials: a printer or sender reads a file named by $(...)")]
    return []


def gh_sends(joined, command):
    gh = re.search(r"(?:^|[ /])gh( .*)", joined)
    if not gh:
        return False
    rest = re.sub(r" (?:-R ?|--repo )\S+", "", gh.group(1))
    if not GH_API.match(rest):
        return bool(GH_SEND.search(rest.strip()))
    method = re.search(r" (?:-X ?|--method )(\w+)", rest)
    writes = (method.group(1).upper() in ("POST", "PUT", "PATCH", "DELETE") if method
              else re.search(r" (-[fF]|--(raw-)?field\b|--input\b)", rest))
    mutation = " graphql" in rest and "mutation" in command.lower() and GH_MUTATION.search(command)
    return bool(writes and (GH_SEND_PATH.search(rest) or mutation))


def command_hits(words, command):
    """Destructive and consent checks on one segment's words."""
    joined = " ".join(words)
    found = [("ask", "destructive: " + n) for n, rx in DESTRUCTIVE.items() if rx.match(joined)]
    if gh_sends(joined, command):
        found.append(("ask", "consent: this sends text through GitHub. Check the exact text"))
    if MAIL.search(joined):
        found.append(("ask", "consent: this sends mail. Check the exact text first"))
    return found


def check_shell(command, cwd):
    if len(command) > MAX_COMMAND:
        return [("ask", "the command is too long for the gate to check")], {command}
    source = BACKTICKS.sub(r"$(\1)", command)   # one rule then covers $(...) and backticks
    text, closed = remove_data(source, cwd)
    found = [] if closed else [("ask", "the gate could not read this command (an open quote)")]
    segments = list(dict.fromkeys(SEGMENT.split(text)))[:MAX_WORDS + 1]
    if any(runs_text(split_words(s), s) for s in segments):
        whole = re.sub(r"\\(.)", r"\1", source.replace("\\\n", ""), flags=re.S)   # nothing is data
        bare = re.sub(r"""['"]""", "", whole)      # `cat .e"n"v` names the same file
        segments = list(dict.fromkeys(SEGMENT.split(whole) + SEGMENT.split(bare)))[:MAX_WORDS + 1]
    parsed = [(s, split_words(s), split_words(NOT_A_FILE.sub(" ", s))) for s in segments]
    distinct = {w for _, _, local in parsed for w in local}
    globs = sum(1 for w in distinct if GLOB_CHARS.search(w))
    if max(len(segments), len(distinct)) > MAX_WORDS or globs > MAX_GLOBS:
        return found + [("ask", "the command has too much in it to check")], {command}
    secret = {w for w in distinct if names_secret(w, cwd)}
    subjects = {command} | {" ".join(words) for _, words, _ in parsed}
    for segment, words, local in parsed:
        found += credential_hits(local, segment, secret) + command_hits(words, command)
    programs = set(map(base, distinct))
    if programs & SQL_CLIENTS and DROP.search(command):
        found.append(("ask", "destructive: DROP TABLE or DATABASE"))
    if programs & HTTP and WEBHOOK.search(command):
        found.append(("ask", "consent: this posts to a webhook or mail server. Check the text"))
    return found, subjects


def mcp_sends(tool):
    _, server, name = (tool.split("__", 2) + ["", ""])[:3]
    if name.lower().startswith(server.lower() + "_"):
        name = name[len(server) + 1:]      # mcp__github__github_list_comments reads
    words = [w.lower().rstrip("s") for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", name)]
    found = set(words)
    return bool(words) and words[0] not in MCP_READ and bool(
        found & MCP_SEND or (found & MCP_WRITE and found & MCP_OBJECT))


def check_tool(tool, tool_input, cwd):
    """Answers and pattern subjects for one call. Raises ValueError on input of the wrong shape."""
    if tool in ("Bash", "Monitor"):
        if not isinstance(tool_input.get("command"), str):
            raise ValueError("no command in the tool input")
        return check_shell(tool_input["command"], cwd)
    if tool in ("Read", "Edit", "Write", "NotebookEdit", "Grep"):
        keys = [k for k in ("file_path", "notebook_path", "path") if k in tool_input]
        path = tool_input[keys[0]] if keys else "" if tool == "Grep" else None
        pattern = tool_input.get("glob", "")
        if not isinstance(path, str) or not isinstance(pattern, str):
            raise ValueError("no usable path in the tool input")
        globs = [p for p in re.split("[{},]", pattern) if p]
        hit = (path and secret_file(path, cwd)) or any(map(pattern_is_secret, globs))
        found = [("deny", "credentials: this touches a secret file (%s)" % (path or pattern))]
        return found if hit else [], [("%s %s %s" % (tool, path, pattern)).strip()]
    if tool.startswith("mcp__"):
        reason = "consent: %s sends text to other people. Check the exact text first" % tool
        subject = "%s %s" % (tool, json.dumps(tool_input, ensure_ascii=False))
        return [("ask", reason)] if mcp_sends(tool) else [], [subject]
    return [], []


def load_patterns(profile_dir):
    """Compiled gate.ask and gate.deny from profile.json. A broken profile raises: the gate asks."""
    if not profile_dir:
        return {"ask": [], "deny": []}
    with open(join(profile_dir, "profile.json"), encoding="utf-8") as f:
        gate = json.load(f).get("gate", {})
    lists = {level: gate.get(level, []) for level in ("ask", "deny")}
    if not all(isinstance(v, list) and all(isinstance(p, str) for p in v) for v in lists.values()):
        raise ValueError("gate.ask and gate.deny must each be a list of regular expressions")
    return {level: [re.compile(p) for p in values] for level, values in lists.items()}


def decide(event, patterns):
    """(level, reason) for one hook event; level is deny, ask or None."""
    tool, tool_input = event.get("tool_name"), event.get("tool_input")
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        raise ValueError("no tool_name or tool_input")
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else ""
    found, subjects = check_tool(tool, tool_input, cwd)
    found += [(level, "profile: matches the pattern %s" % rx.pattern) for level in ("deny", "ask")
              for rx in patterns[level] if any(rx.search(s) for s in subjects)]
    for level in ("deny", "ask"):
        reasons = list(dict.fromkeys(r for lv, r in found if lv == level))
        if reasons:
            return level, "; ".join(reasons)
    return None, ""


def answer(level, reason, event):
    if level == "ask" and event.get("agent_id"):
        call = {"tool": event.get("tool_name"), "input": event.get("tool_input")}
        level, reason = "deny", "%s. A subagent cannot ask: hand this exact call back to the " \
            "session: %s" % (reason, json.dumps(call, ensure_ascii=False))
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": level,
        "permissionDecisionReason": "harness gate: " + reason}}))


def self_test(profile_dir):
    """Built-in checks run without the user's patterns. Exit 3 names a bad profile."""
    for tool, tool_input, want in SAMPLES:
        event = {"tool_name": tool, "tool_input": tool_input, "cwd": "/tmp"}
        got = decide(event, {"ask": [], "deny": []})[0]
        if got != want:
            print("gate self-test failed: %s %s gave %s" % (tool, tool_input, got), file=sys.stderr)
            return 1
    try:
        load_patterns(profile_dir)
    except (OSError, ValueError, AttributeError, re.error) as exc:
        print("gate patterns in profile.json are broken: %s" % exc, file=sys.stderr)
        return 3
    return 0


def main(argv):
    profile_dir = argv[argv.index("--profile") + 1] if "--profile" in argv else None
    if "--self-test" in argv:
        return self_test(profile_dir)
    raw, event = "", {}
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        event = json.loads(raw)
        level, reason = decide(event, load_patterns(profile_dir))
    except Exception as exc:               # fail closed: a broken gate never waves a call through
        if not (event and isinstance(event, dict)):  # unreadable, but it may be from a subagent
            event = {"agent_id": "unknown"} if '"agent_id"' in raw else {}
        level = "ask"
        reason = "the gate could not check this call (%s: %s)" % (type(exc).__name__, exc)
    if level:
        answer(level, reason, event)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
