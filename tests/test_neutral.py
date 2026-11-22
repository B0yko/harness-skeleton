import os
import re
import unittest

from support import REPO

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
HOME_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+")
SKIP = {".git", "__pycache__"}


def files():
    for folder, subdirs, names in os.walk(REPO):
        subdirs[:] = [d for d in subdirs if d not in SKIP]
        for name in names:
            yield os.path.join(folder, name)


class Neutral(unittest.TestCase):
    def test_no_real_email_or_home_path(self):
        problems = []
        for path in files():
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            rel = os.path.relpath(path, REPO)
            problems += ["%s: email %s" % (rel, m.group(0)) for m in EMAIL.finditer(text)
                         if m.group(1).lower() != "example.com"]
            problems += ["%s: home path %s" % (rel, m.group(0)) for m in HOME_PATH.finditer(text)]
        self.assertEqual(problems, [])

    def test_agents_name_no_model(self):
        folder = os.path.join(REPO, "core", "agents")
        for name in os.listdir(folder):
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                self.assertIn("model: inherit", f.read(), name)


if __name__ == "__main__":
    unittest.main()
