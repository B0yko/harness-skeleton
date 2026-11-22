#!/usr/bin/env python3
"""harness: install, check and remove the skeleton, keep task checkpoints. Python 3.9+, stdlib."""
import argparse
import contextlib
import copy
import fcntl
import glob
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
from decimal import Decimal

sys.dont_write_bytecode = True
join = os.path.join
REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
GATE = join(REPO, "core", "hooks", "gate.py")
TEMPLATE = join(REPO, "profile-template")
MIN_CLAUDE = (2, 1, 295)
MEMORY_KEY = "autoMemoryDirectory"
TMP = ".harness-tmp"              # a fixed name, so the next run finds what an interrupted one left
TASK_KEYS = {"id", "title", "status", "next_step", "evidence", "version"}
STATUSES = ("TODO", "RUNNING", "BLOCKED", "DONE", "CANCELLED")
TASK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")


class Refused(Exception):
    """A clear no: printed as is, exit 1, nothing changed."""


class Paths:
    def __init__(self):
        self.home = os.path.expanduser("~")
        self.root = join(self.home, ".harness")
        self.clone = join(self.root, "core")
        self.profile = join(self.root, "profile")
        self.manifest = join(self.root, "managed.json")
        self.backup = join(self.root, "settings.backup.json")
        self.tasks = join(self.profile, "state", "tasks")
        self.sessions = join(self.profile, "state", "sessions")
        self.bin = join(self.home, ".local", "bin", "harness")
        config = os.environ.get("CLAUDE_CONFIG_DIR") or join(self.home, ".claude")
        self.config = os.path.abspath(os.path.expanduser(config))
        self.settings = join(self.config, "settings.json")


def run(*command, timeout=30, **kw):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, **kw)


def read_json(path, **options):
    if not os.path.lexists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f, **options)


def exact_number(text):
    """A JSON number a rewrite keeps. 1e999 or a long decimal would change, so refuse them."""
    if math.isfinite(float(text)) and Decimal(text) == Decimal(repr(float(text))):
        return float(text)
    raise Refused("Your settings hold the number %s, which would change when saved. Replace it "
                  "with a plain number and run again." % text)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def dumps(data, **kw):
    text = json.dumps(data, indent=2, ensure_ascii=False, **kw) + "\n"
    return text.encode("utf-8", "backslashreplace")     # a lone surrogate stays a JSON escape


def clear(path):
    if os.path.lexists(path):
        shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.unlink(path)


@contextlib.contextmanager
def locked(folder):
    """One writer at a time. A lock on the folder itself leaves no file behind."""
    fd = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def write_atomic(path, data, mode=None):
    """Replace a file in one step. A symlink stays a symlink; the mode stays unless one is given.
    The temp name is fixed, so the next run clears what an interrupted one left."""
    path = os.path.realpath(path)
    tmp = path + TMP
    clear(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        os.chmod(tmp, mode)
    elif os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def check_writable(path):
    """Refuse to replace a file the user protected, or to split a hard link from its other names."""
    if os.path.lexists(path) and not os.access(path, os.W_OK):
        raise Refused("%s is write-protected and was left alone. Run `chmod u+w` on it." % path)
    if os.path.lexists(path) and os.stat(path).st_nlink > 1:
        raise Refused("%s has other hard links, which a write would detach. Use a symlink." % path)


def missing_dirs(paths):
    out = []
    for path in paths:
        chain = []
        while not os.path.isdir(path):
            chain.append(path)
            path = os.path.dirname(path)
        out += [folder for folder in reversed(chain) if folder not in out]
    return out


def make_dirs(path, mode=0o777):
    for folder in missing_dirs([path]):
        os.mkdir(folder, mode)


def link(path, target):
    tmp = path + TMP
    clear(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, path)


def load_settings(path):
    if not os.path.lexists(path):
        return {}
    try:
        data = read_json(path, parse_float=exact_number, parse_constant=exact_number)
    except (OSError, ValueError, ArithmeticError) as exc:
        raise Refused("%s is not readable JSON (%s). Fix it and run again." % (path, exc))
    hooks = data.get("hooks", {}) if isinstance(data, dict) else None
    if not isinstance(hooks, dict) or not all(isinstance(v, list) for v in hooks.values()):
        raise Refused("%s does not have the expected shape (an object, with hooks by event). "
                      "Fix it first." % path)
    return data


def handlers(settings):
    for groups in settings.get("hooks", {}).values():
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            for handler in inner if isinstance(inner, list) else ():
                if isinstance(handler, dict):
                    yield handler


def remove_hooks(settings, commands, prune=False):
    """A copy without these commands. What that empties goes too; prune also drops what was."""
    out = copy.deepcopy(settings)
    hooks = out.get("hooks", {})
    emptied = False
    for event in list(hooks):
        kept, removed = [], False
        for group in hooks[event]:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(inner, list):
                left = [h for h in inner
                        if not (isinstance(h, dict) and h.get("command") in commands)]
                removed = removed or len(left) != len(inner)
                if (inner or prune) and not left:
                    continue
                group["hooks"] = left
            kept.append(group)
        hooks[event] = kept
        if not kept and (removed or prune):
            del hooks[event]
            emptied = True
    if "hooks" in out and not hooks and (emptied or prune):
        del out["hooks"]
    return out


def canonical(data):
    """JSON text for comparing: unlike Python, it tells 1 from true."""
    return json.dumps(remove_hooks(data, (), prune=True), sort_keys=True)


def our_hooks(p):
    with open(join(REPO, "core", "settings.json"), encoding="utf-8") as f:
        hooks = json.load(f)["hooks"]
    for handler in handlers({"hooks": hooks}):
        for key, value in (("{core}", REPO), ("{profile}", p.profile)):
            handler["command"] = handler["command"].replace(key, shlex.quote(value))
    return hooks


def hooks_by_shape(p, settings):
    """Commands of exactly the shape we write, also from a release the manifest forgot.
    Anchored at both ends: a hook of the user's that only mentions our path is theirs."""
    roots = "|".join(re.escape(shlex.quote(root))
                     for root in {REPO, p.clone, os.path.realpath(p.clone)})
    ours = re.compile(
        r"/usr/bin/env python3(?: -[A-Za-z]+)* (?:%s)/core/hooks/(?:gate|session_start)\.py"
        r" --profile %s(?: \|\| \{ rc=\$\?; \[ -e (?:%s)/core/hooks/gate\.py \] \|\| "
        r"echo '[^']*' >&2; exit \$rc; \})?" % (roots, re.escape(shlex.quote(p.profile)), roots))
    return {h["command"] for h in handlers(settings)
            if isinstance(h.get("command"), str) and ours.fullmatch(h["command"])}


def points_into_root(p, path):
    """Whether a symlink's target lies under this ~/.harness, even if it is gone."""
    target = os.readlink(path)
    return any(target.startswith(root + os.sep) for root in (p.root, os.path.realpath(p.root)))


def known_targets(manifest, path):
    """What a link of ours may point at: an interrupted run may not have replaced it yet."""
    return {manifest.get("links", {}).get(path),
            *manifest.get("previous_links", {}).get(path, ())}


def shared_memory(p):
    """True only if the profile opts in to one memory folder for every project."""
    try:
        data = read_json(join(p.profile if os.path.isdir(p.profile) else TEMPLATE, "profile.json"))
    except (OSError, ValueError):
        return False          # the gate self-test refuses a broken profile with a clear message
    memory = data.get("memory") if isinstance(data, dict) else None
    return isinstance(memory, dict) and memory.get("shared") is True


def merge_settings(current, ours, old, memory_dir, shared):
    """(new settings, the memory value we own). Keeps all else, and where existing hooks sit."""
    wanted = [h for groups in ours.values() for group in groups for h in group["hooks"]]
    present = list(handlers(current))
    stale = set(old.get("hook_commands", [])) - {h["command"] for h in wanted}
    stale |= {h["command"] for h in wanted if h not in present}
    out = remove_hooks(current, stale) if stale else copy.deepcopy(current)
    for event, groups in ours.items():
        for group in groups:
            if not all(h in present for h in group["hooks"]):
                out.setdefault("hooks", {}).setdefault(event, []).append(group)
    memory = old.get("memory_value")
    if "links" not in old and old.get("hook_commands") and out.get(MEMORY_KEY) == memory_dir:
        memory = memory_dir            # our hooks are there but their record was lost
    if shared and MEMORY_KEY not in out:
        out[MEMORY_KEY] = memory = memory_dir
    elif not shared and memory:
        if out.get(MEMORY_KEY) == memory:
            del out[MEMORY_KEY]
        memory = None
    return out, memory


def read_manifest(p):
    try:
        data = read_json(p.manifest)
        keys = ("config", "dirs", "hook_commands", "settings_existed")
        if data is None or (isinstance(data, dict) and isinstance(data.get("links"), dict)
                            and all(k in data for k in keys)):
            return data
    except ValueError:
        pass
    raise Refused("%s is corrupt. Restore it, or remove it and clean up by hand." % p.manifest)


def plan_links(p):
    source = p.profile if os.path.isdir(p.profile) else TEMPLATE
    links = {join(p.config, "rules", "harness-core"): join(REPO, "core", "rules"),
             join(p.config, "rules", "harness-profile"): join(p.profile, "rules"),
             p.bin: join(REPO, "tools", "harness.py")}
    core = join(REPO, "core")
    for top, final in ((core, core), (source, p.profile)):      # the profile wins a name
        for kind, wanted in (("skills", "SKILL.md"), ("agents", None)):
            folder = join(top, kind)
            for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else ():
                if os.path.isfile(join(folder, name, wanted)) if wanted else name.endswith(".md"):
                    links[join(p.config, kind, name)] = join(final, kind, name)
    return links


def conflicts(p, links, old):
    found = [path for path in (p.root, p.profile)
             if os.path.lexists(path) and not os.path.isdir(path)]
    for path, target in links.items():
        parent = os.path.dirname(path)
        while not os.path.lexists(parent) and parent != os.path.dirname(parent):
            parent = os.path.dirname(parent)
        if not os.path.isdir(parent):
            found.append(parent)
        elif os.path.lexists(path) and not (os.path.islink(path) and (
                os.readlink(path) in known_targets(old, path) | {target}
                or (not old and points_into_root(p, path)))):     # the record of it was lost
            found.append(path)
    return sorted(set(found))


def self_test(profile):
    test = run("/usr/bin/env", "python3", "-I", "-S", GATE, "--self-test", "--profile", profile)
    if test.returncode:
        raise Refused("The gate self-test failed on python3 from PATH, so no hook was written.\n"
                      + test.stderr.strip())


def build_profile(p):
    """Copy the template beside its place and rename it, so a profile is whole or absent."""
    tmp = p.profile + TMP
    clear(tmp)
    shutil.copytree(TEMPLATE, tmp)
    os.rename(tmp, p.profile)


def print_plan(p, links, todo, stale, changes_settings):
    created = "" if os.path.isdir(p.profile) else " (would be created)"
    print("dry run, nothing written\nconfig:   %s\nprofile:  %s%s\nmanifest: %s\ncli:      %s" % (
        p.config, p.profile, created, p.manifest, p.bin))
    for path in todo:
        print("would link %s -> %s" % (path, links[path]))
    for path in stale:
        print("would remove stale link %s" % path)
    print("would %s %s" % ("update" if changes_settings else "leave", p.settings))


def save_manifest(p, manifest):
    data = dumps(manifest, sort_keys=True)
    if not os.path.exists(p.manifest) or data != read_bytes(p.manifest):
        write_atomic(p.manifest, data)


def apply_install(p, manifest, old, todo, stale, current, merged):
    """The writes. Until the settings are saved the manifest lists old and new hooks, so even a
    hard kill leaves a record uninstall can follow."""
    changes = []
    make_dirs(p.root, 0o700)
    for path in stale:
        if os.path.islink(path) and os.readlink(path) in known_targets(old, path):
            os.unlink(path)
            changes.append("removed stale link " + path)
    ours = sorted(set(manifest["hook_commands"]) | set(old.get("hook_commands", [])))
    if todo or merged != current:
        earlier = {path: sorted(filter(None, known_targets(old, path)))
                   for path in {*old.get("links", {}), *old.get("previous_links", {})}}
        save_manifest(p, dict(manifest, hook_commands=ours, previous_links=earlier,
                              memory_value=manifest["memory_value"] or old.get("memory_value")))
    if not os.path.isdir(p.profile):
        build_profile(p)
        changes.append("created " + p.profile)
    for path in todo:
        make_dirs(os.path.dirname(path))
        link(path, manifest["links"][path])
        changes.append("linked %s" % path)
    if merged != current:
        untouched = not any(h.get("command") in ours for h in handlers(current))
        if untouched and os.path.lexists(p.settings) and not os.path.lexists(p.backup):
            write_atomic(p.backup, read_bytes(p.settings),
                         mode=os.stat(p.settings).st_mode & 0o600)
            changes.append("backed up settings to " + p.backup)
        write_atomic(p.settings, dumps(merged))     # the hooks go in last
        changes.append("updated " + p.settings)
    save_manifest(p, manifest)
    return changes


def install(p, dry_run):
    old = read_manifest(p) or {}
    if old and old.get("config") != p.config:
        raise Refused("Already installed for %s. Run `harness uninstall` first." % old["config"])
    links = plan_links(p)
    bad = conflicts(p, links, old)
    if bad:
        raise Refused("Refusing to replace what is not ours, nothing was changed:\n  "
                      + "\n  ".join(bad))
    current = load_settings(p.settings)
    ours = our_hooks(p)
    leftover = hooks_by_shape(p, current)
    rest = remove_hooks(current, leftover)
    if rest.get(MEMORY_KEY) == join(p.profile, "memory"):
        del rest[MEMORY_KEY]
    only_ours = bool(leftover) and not rest
    known = dict(old, hook_commands=sorted(leftover.union(old.get("hook_commands", []))))
    merged, memory = merge_settings(current, ours, known, join(p.profile, "memory"),
                                    shared_memory(p))
    if merged != current:
        check_writable(p.settings)
    todo = [path for path, target in links.items()
            if not (os.path.islink(path) and os.readlink(path) == target)]
    stale = [path for path in old.get("links", {}) if path not in links]
    if dry_run:
        print_plan(p, links, todo, stale, merged != current)
        return 0
    made_profile = not os.path.isdir(p.profile)
    self_test(p.profile if not made_profile else TEMPLATE)
    dirs = list(old.get("dirs", []))
    dirs += [d for d in missing_dirs([p.root] + [os.path.dirname(path) for path in todo])
             if d not in dirs]
    commands = sorted(h["command"] for g in ours.values() for x in g for h in x["hooks"])
    manifest = {"version": 1, "config": p.config, "links": links, "dirs": dirs,
                "hook_commands": commands, "memory_value": memory,
                "settings_existed": old.get("settings_existed",
                                            os.path.lexists(p.settings) and not only_ours)}
    try:
        changes = apply_install(p, manifest, known, todo, stale, current, merged)
    except BaseException:
        if not old:                   # a first install that failed leaves nothing behind
            with contextlib.suppress(OSError, ValueError, Refused):
                remove_installed(p, manifest)
                if made_profile:
                    clear(p.profile)
                    os.rmdir(p.root)
        raise
    print("\n".join(changes) if changes else "Already installed, nothing changed.")
    return 0


def cmd_install(args):
    p = Paths()
    with locked(p.home):
        return install(p, args.dry_run)


def keep_backup(p):
    """The settings from before install, under a name no later install reuses."""
    n = 1
    while os.path.lexists("%s.%d" % (p.backup, n)):
        n += 1
    os.rename(p.backup, "%s.%d" % (p.backup, n))
    print("Kept your settings from before install at %s.%d" % (p.backup, n))


def restore_settings(p, manifest):
    """Put the settings back. True if the backup holds something the file no longer does."""
    settings = join(manifest["config"], "settings.json")
    clear(os.path.realpath(settings) + TMP)
    has_backup = os.path.isfile(p.backup)
    if not os.path.lexists(settings):
        if has_backup:                # it went missing, so the backup is the last copy
            make_dirs(manifest["config"])
            write_atomic(settings, read_bytes(p.backup))
        return False
    current = load_settings(settings)
    clean = remove_hooks(current, set(manifest["hook_commands"]) | hooks_by_shape(p, current))
    if manifest.get("memory_value") and clean.get(MEMORY_KEY) == manifest["memory_value"]:
        del clean[MEMORY_KEY]
    backup = load_settings(p.backup) if has_backup else {}
    same = has_backup and canonical(clean) == canonical(backup)
    data = read_bytes(p.backup) if same else None if clean == current else dumps(clean)
    if not same and not manifest["settings_existed"] and not clean:
        check_writable(settings)
        os.unlink(settings)
    elif data is not None and data != read_bytes(settings):
        check_writable(settings)
        write_atomic(settings, data)
    return not same and any(json.dumps(clean.get(key), sort_keys=True)
                            != json.dumps(value, sort_keys=True) for key, value in backup.items())


def remove_installed(p, manifest):
    """Take out what the manifest lists. It goes last, so an interrupted run can be repeated."""
    keep = restore_settings(p, manifest)
    for path, target in manifest["links"].items():
        clear(path + TMP)
        if os.path.islink(path) and os.readlink(path) in known_targets(manifest, path):
            os.unlink(path)
        elif os.path.lexists(path):
            print("Kept %s: it changed after install." % path)
    remove_links_into_root(p, manifest["config"], dangling_only=True)
    if keep:
        keep_backup(p)
    for path in (p.backup, p.backup + TMP, p.profile + TMP):
        clear(path)
    for folder in reversed(manifest["dirs"]):
        with contextlib.suppress(OSError):
            os.rmdir(folder)
    clear(p.manifest)


def remove_links_into_root(p, config, dangling_only):
    """Links into this ~/.harness that no manifest lists, because the record of them was lost."""
    found = [join(config, kind, name) for kind in ("rules", "skills", "agents")
             if os.path.isdir(join(config, kind)) for name in os.listdir(join(config, kind))]
    for path in found + [p.bin]:
        if os.path.islink(path) and points_into_root(p, path) and (
                not dangling_only or not os.path.exists(path)):
            os.unlink(path)


def remove_orphans(p):
    """With the manifest gone, what is ours can still be told by what it runs or points at."""
    current = load_settings(p.settings)
    clean = remove_hooks(current, hooks_by_shape(p, current))
    if clean.get(MEMORY_KEY) == join(p.profile, "memory"):
        del clean[MEMORY_KEY]
    if clean != current:
        check_writable(p.settings)
        write_atomic(p.settings, dumps(clean))
        print("Removed the hooks from %s." % p.settings)
    remove_links_into_root(p, p.config, dangling_only=False)


def cmd_uninstall(_args):
    p = Paths()
    with locked(p.home):
        manifest = read_manifest(p)
        if manifest:
            remove_installed(p, manifest)
        else:
            remove_orphans(p)
        clear(p.manifest + TMP)
        clear(p.profile + TMP)
        with contextlib.suppress(OSError):
            os.rmdir(p.root)
    print("Uninstalled." if manifest else "Nothing to uninstall.")
    if os.path.isdir(p.root):
        print("Kept %s (the core clone and your profile). To delete it: rm -rf %s" % (
            p.root, shlex.quote(p.root)))
    return 0


def cmd_update(_args):
    if not os.path.isdir(join(REPO, ".git")):
        raise Refused("%s is not a git clone, so it cannot update." % REPO)
    if run("git", "-C", REPO, "symbolic-ref", "-q", "HEAD").returncode:
        raise Refused("%s is on a detached commit, so there is nothing to pull. Run `git -C %s "
                      "checkout main`, then `harness update`." % (REPO, shlex.quote(REPO)))
    if subprocess.run(["git", "-C", REPO, "pull", "--ff-only"]).returncode:
        raise Refused("git pull --ff-only failed in %s. Resolve it there and run again." % REPO)
    os.execv(sys.executable, [sys.executable, join(REPO, "tools", "harness.py"), "install"])


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def header_chars(path):
    """Characters of the name and description in a markdown file's YAML header."""
    header = re.match(r"\s*---\n(.*?)\n---", read_text(path), re.S)
    fields = re.finditer(r"^(name|description):.*(\n[ \t].*)*", header.group(1) if header else "",
                         re.M)
    return len("\n".join(m.group(0) for m in fields))


def read_budgets(p):
    path = join(p.profile, "profile.json")
    data = read_json(path if os.path.isfile(path) else join(TEMPLATE, "profile.json"))
    budgets = data.get("budgets", {}) if isinstance(data, dict) else None
    if not isinstance(budgets, dict) or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in budgets.values()):
        raise Refused("%s: `budgets` must be an object of numbers." % path)
    return budgets


def measure(p):
    settings = load_settings(p.settings)
    memory_dir = settings.get(MEMORY_KEY)     # without it, memory is per project: no fixed load
    memory = ""
    if memory_dir:
        text = read_text(join(os.path.expanduser(memory_dir), "MEMORY.md"))
        memory = "".join(text.splitlines(True)[:200])[:25000]
    skills, agents = join(p.config, "skills"), join(p.config, "agents")
    rules = glob.glob(join(glob.escape(p.config), "rules", "**", "*.md"), recursive=True)
    skill_names = sorted(os.listdir(skills)) if os.path.isdir(skills) else []
    agent_names = sorted(os.listdir(agents)) if os.path.isdir(agents) else []
    parts = {"claude_md": len(read_text(join(p.config, "CLAUDE.md"))),
             "rules": sum(len(read_text(f)) for f in rules),      # follows the linked folders
             "memory": len(memory),
             "skills": sum(header_chars(join(skills, n, "SKILL.md")) for n in skill_names),
             "agents": sum(header_chars(join(agents, n)) for n in agent_names
                           if n.endswith(".md"))}
    budgets = read_budgets(p)
    result = {"loaded_chars": sum(parts.values()), "hooks": sum(1 for _ in handlers(settings)),
              "parts": parts, "budgets": budgets}
    result["over"] = [k for k in ("loaded_chars", "hooks")
                      if k in budgets and result[k] > budgets[k]]
    return result


def installed_paths():
    """Paths and manifest, with the config folder this install really used."""
    p = Paths()
    manifest = read_manifest(p)
    if manifest:
        p.config, p.settings = manifest["config"], join(manifest["config"], "settings.json")
    return p, manifest


def cmd_weight(args):
    w = measure(installed_paths()[0])
    if args.json:
        print(json.dumps(w, indent=2))
    else:
        for key in ("loaded_chars", "hooks"):
            print("%-13s %6d / %s%s" % (key, w[key], w["budgets"].get(key, "no budget"),
                                        "  OVER" if key in w["over"] else ""))
        print("  " + "  ".join("%s %d" % item for item in w["parts"].items()))
    return 1 if w["over"] else 0


def check_gate(settings, manifest):
    """(state, detail, fix) for the real hook command: its self-test, then one real event."""
    fix = "run `harness install`"
    if settings.get("disableAllHooks") is True:
        return "FAIL", "disableAllHooks is true, so Claude Code runs no gate", (
            "remove disableAllHooks from settings.json")
    gates = [h for h in handlers(settings) if h.get("command") in manifest.get("hook_commands", [])
             and "gate.py" in h["command"]]
    if not gates:
        return "FAIL", "the gate hook is not in settings.json", fix
    if gates[0].get("onFailure") != "block":
        return "FAIL", "the gate hook lacks onFailure: block", fix
    command = gates[0]["command"]
    try:
        test = run("/bin/sh", "-c", command.replace(" --profile ", " --self-test --profile ", 1))
        if test.returncode == 3:
            return "FAIL", test.stderr.strip(), "correct gate.ask and gate.deny in profile.json"
        if test.returncode:
            detail = "the hook's self-test exited %d: %s" % (test.returncode, test.stderr.strip())
            return "FAIL", detail, fix
        event = dict(tool_name="Bash", tool_input={"command": "git push --force"}, cwd="/tmp",
                     agent_id="doctor")
        hook = run("/bin/sh", "-c", command, input=json.dumps(event), timeout=10)
        answer = json.loads(hook.stdout)["hookSpecificOutput"].get("permissionDecision")
        if hook.returncode or answer != "deny":
            return "FAIL", "an ask inside a subagent did not become a deny", fix
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return "FAIL", "the hook command failed: %s" % exc, fix
    return "OK", "self-test passed, a subagent's ask becomes a deny", ""


def check_python():
    """The hooks run the first python3 on PATH, so that is the one that counts."""
    code = ("import sys; print('%d.%d.%d' % sys.version_info[:3]); "
            "sys.exit(sys.version_info < (3, 9))")
    try:
        found = run("/usr/bin/env", "python3", "-ISc", code, timeout=10)
    except (OSError, subprocess.SubprocessError):
        found = None
    if found and found.returncode == 0:
        return "OK", found.stdout.strip(), ""
    version = found.stdout.strip() if found else ""
    detail = version + " is below 3.9" if version else "no working python3 on PATH"
    return "FAIL", detail, "install Python 3.9 or later and put it first on PATH"


def check_claude():
    path = shutil.which("claude")
    wanted = ".".join(map(str, MIN_CLAUDE))
    if not path:
        return "WARN", "claude not found", "install Claude Code %s or later" % wanted
    try:
        text = run(path, "--version", timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not found:
        return "WARN", "could not read the claude version", "run `claude --version`"
    if tuple(int(x) for x in found.groups()) < MIN_CLAUDE:
        detail = "claude %s lacks onFailure: a broken gate fails open" % found.group(0)
        return "FAIL", detail, "run `claude update`"
    return "OK", "claude " + found.group(0), ""


def cmd_doctor(_args):
    p, manifest = installed_paths()
    rows = [("python",) + check_python(), ("claude",) + check_claude()]
    if manifest and Paths().config != p.config:
        rows.append(("config", "WARN", "installed for %s, but this shell uses %s" % (
            p.config, Paths().config), "sessions there have no gate: set CLAUDE_CONFIG_DIR"))
    if not manifest:
        rows.append(("install", "FAIL", "no manifest at " + p.manifest, "run `harness install`"))
    else:
        broken = [path for path, target in manifest["links"].items() if not (
            os.path.islink(path) and os.readlink(path) == target and os.path.exists(path))]
        detail = "broken: " + ", ".join(broken) if broken else "%d links" % len(manifest["links"])
        rows.append(("links", "FAIL" if broken else "OK", detail,
                     "run `harness install`" if broken else ""))
        try:
            settings = load_settings(p.settings)
            rows.append(("settings", "OK", "valid JSON", ""))
            rows.append(("gate",) + check_gate(settings, manifest))
            w = measure(p)
            detail = "%d chars, %d hooks" % (w["loaded_chars"], w["hooks"])
            rows.append(("weight", "WARN", detail + ", over: " + ", ".join(w["over"]),
                         "trim what loads, or raise budgets in profile.json") if w["over"]
                        else ("weight", "OK", detail, ""))
        except (Refused, OSError, ValueError) as exc:
            rows.append(("settings", "FAIL", str(exc),
                         "repair the file, then run `harness install`"))
    if os.path.dirname(p.bin) not in os.environ.get("PATH", "").split(os.pathsep):
        rows.append(("path", "WARN", "~/.local/bin is not on PATH",
                     'add `export PATH="$HOME/.local/bin:$PATH"`'))
    for name, state, detail, fix in rows:
        print("%-4s %-9s %s%s" % (state, name, detail, "  (fix: %s)" % fix if fix else ""))
    return 1 if any(row[1] == "FAIL" for row in rows) else 0


def task_file(p, task_id):
    if not TASK_ID.match(task_id or ""):
        raise Refused("Bad task id %r: use lowercase letters, digits and hyphens." % task_id)
    return join(p.tasks, task_id + ".json")


def load_task(p, task_id):
    path = task_file(p, task_id)
    if not os.path.isfile(path):
        raise Refused("No task %s." % task_id)
    task = read_json(path)
    if not (isinstance(task, dict) and TASK_KEYS <= set(task)
            and isinstance(task["version"], int) and isinstance(task["evidence"], list)):
        raise Refused("%s is not a task file: it needs %s." % (path, ", ".join(sorted(TASK_KEYS))))
    return task


def session_binding(p):
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not SESSION_ID.match(session):
        raise Refused("CLAUDE_CODE_SESSION_ID is not set. Run this inside a Claude Code session.")
    return join(p.sessions, session)


def task_update(p, args):
    task = load_task(p, args.id)
    if args.expect is not None and args.expect != task["version"]:
        print("Conflict: %s is at version %d, not %d." % (args.id, task["version"], args.expect),
              file=sys.stderr)
        return None
    if not (args.title or args.status or args.evidence or args.next is not None):
        raise Refused("Nothing to set.")
    task.update(title=args.title or task["title"], status=args.status or task["status"],
                next_step=task["next_step"] if args.next is None else args.next,
                version=task["version"] + 1,
                evidence=task["evidence"] + [e.strip() for e in args.evidence if e.strip()])
    if task["status"] == "DONE" and not task["evidence"]:
        raise Refused("DONE needs evidence: add --evidence with a test output, a file or a URL.")
    return task


def cmd_task(args):
    p = Paths()
    if args.action == "list":
        files = os.listdir(p.tasks) if os.path.isdir(p.tasks) else []
        for name in sorted(n[:-5] for n in files if n.endswith(".json")):
            task = load_task(p, name)
            print("%-24s %-10s v%-3d %s" % (task["id"], task["status"], task["version"],
                                            task["title"]))
    elif args.action == "show":
        task_id = args.id or read_text(session_binding(p)).strip()
        if not task_id:
            raise Refused("This session has no bound task.")
        print(json.dumps(load_task(p, task_id), indent=2, ensure_ascii=False))
    elif args.action == "bind":
        load_task(p, args.id)
        os.makedirs(p.sessions, exist_ok=True)
        write_atomic(session_binding(p), (args.id + "\n").encode())
        print("Bound this session to %s." % args.id)
    else:
        path = task_file(p, args.id)
        os.makedirs(p.tasks, exist_ok=True)
        with locked(p.tasks):
            if args.action == "new" and os.path.exists(path):
                raise Refused("Task %s already exists." % args.id)
            task = task_update(p, args) if args.action == "set" else {
                "id": args.id, "title": args.title or "", "status": "TODO",
                "next_step": args.next or "", "evidence": [], "version": 1}
            if task is None:
                return 3
            write_atomic(path, dumps(task))
        print(json.dumps(task, indent=2, ensure_ascii=False))
    return 0


def parser():
    ap = argparse.ArgumentParser(prog="harness", description="A small harness for Claude Code.")
    sub = ap.add_subparsers(dest="command", required=True)
    commands = {}
    for name, run, text in (
            ("install", cmd_install, "compose the Claude config from core and profile"),
            ("uninstall", cmd_uninstall, "remove links and hooks, keep ~/.harness"),
            ("update", cmd_update, "git pull --ff-only, then install"),
            ("doctor", cmd_doctor, "check everything, one line each"),
            ("weight", cmd_weight, "what every session loads, against the budgets")):
        commands[name] = sub.add_parser(name, help=text)
        commands[name].set_defaults(run=run)
    commands["install"].add_argument("--dry-run", action="store_true",
                                     help="print the targets and change nothing")
    commands["weight"].add_argument("--json", action="store_true")
    task_parser = sub.add_parser("task", help="versioned task checkpoints")
    task = task_parser.add_subparsers(dest="action", required=True)
    actions = {name: task.add_parser(name) for name in ("new", "bind", "show", "list", "set")}
    for name, action in actions.items():
        action.set_defaults(run=cmd_task)
        if name != "list":
            action.add_argument("id", nargs="?" if name == "show" else None)
        if name in ("new", "set"):
            action.add_argument("--title")
            action.add_argument("--next", help="the next step")
    actions["set"].add_argument("--status", choices=STATUSES)
    actions["set"].add_argument("--evidence", action="append", default=[], help="repeatable")
    actions["set"].add_argument("--expect", type=int, help="exit 3 unless at this version")
    return ap


def main():
    args = parser().parse_args()
    try:
        return args.run(args)
    except (Refused, OSError, ValueError, RecursionError, subprocess.SubprocessError) as exc:
        print(exc if isinstance(exc, Refused) else "harness: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("harness: interrupted. Run it again to finish.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
