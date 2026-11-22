"""Shared test helpers. Every run gets a temp HOME, and CLAUDE_CONFIG_DIR never leaks in."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID"):
    os.environ.pop(name, None)
sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, "tools", "harness.py")
GATE = os.path.join(REPO, "core", "hooks", "gate.py")
SESSION_START = os.path.join(REPO, "core", "hooks", "session_start.py")
sys.path.insert(0, os.path.dirname(GATE))


def tree(root):
    """Every path under root with its type, mode and content hash (or link target)."""
    out = {}
    for folder, subdirs, files in os.walk(root):
        for name in subdirs + files:
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, root)
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                out[rel] = "link:" + os.readlink(path)
            elif stat.S_ISDIR(info.st_mode):
                out[rel] = "dir"
            else:
                with open(path, "rb") as f:
                    out[rel] = "%o:%s" % (stat.S_IMODE(info.st_mode), hashlib.sha256(f.read()).hexdigest())
    return out


class HomeCase(unittest.TestCase):
    """A test with its own temp HOME and a PATH holding a stub `claude`."""

    claude_version = "2.1.300"

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(self.home)
        os.makedirs(self.stubs)
        self.config = os.path.join(self.home, ".claude")
        self.settings = os.path.join(self.config, "settings.json")
        self.profile = os.path.join(self.home, ".harness", "profile")
        self.stub("claude", "#!/bin/sh\necho '%s (Claude Code)'\n" % self.claude_version)
        self.stub("python3", '#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)

    def stub(self, name, text):
        path = os.path.join(self.stubs, name)
        with open(path, "w") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID")}
        env.update(HOME=self.home, PATH=self.stubs + ":/usr/bin:/bin", PYTHONDONTWRITEBYTECODE="1")
        env.update(extra)
        return env

    def harness(self, *args, script=HARNESS, **extra):
        return subprocess.run([sys.executable, script] + list(args), env=self.env(**extra), cwd=self.tmp,
                              capture_output=True, text=True, timeout=60)

    def install(self, *args, **extra):
        run = self.harness("install", *args, **extra)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return run

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read_settings(self):
        with open(self.settings, encoding="utf-8") as f:
            return json.load(f)


class GitCase(HomeCase):
    """A HomeCase that installs through install.sh from a throwaway git copy of the tree."""

    def git(self, path, *args):
        identity = ["-c", "user.name=test", "-c", "user.email=test@example.com"]
        return subprocess.run(["git", "-C", path] + identity + list(args), check=True, capture_output=True, text=True,
                              env=self.env())

    def source(self):
        path = os.path.join(self.tmp, "source")
        shutil.copytree(REPO, path, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "test"]):
            self.git(path, *args)
        return path

    def script(self, source, **extra):
        return subprocess.run(["bash", os.path.join(REPO, "install.sh")], env=self.env(HARNESS_REPO=source, **extra),
                              cwd=self.tmp, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    def cli(self, *args):
        return subprocess.run([os.path.join(self.home, ".local", "bin", "harness")] + list(args), env=self.env(),
                              capture_output=True, text=True, timeout=60)
