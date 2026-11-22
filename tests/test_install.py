import json
import os
import shutil
import subprocess
import unittest

from support import HARNESS, REPO, GitCase, HomeCase, tree

USER_SETTINGS = '{ "model":"custom",\n\n  "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [\n' \
                '{"type": "command", "command": "true"}]}]}, "env": {"A": "1"} }\n'


class Install(HomeCase):
    def populate(self):
        self.write(self.settings, USER_SETTINGS)
        self.write(os.path.join(self.config, "CLAUDE.md"), "my rules\n")
        self.write(os.path.join(self.config, "skills", "mine", "SKILL.md"), "---\nname: mine\ndescription: x\n---\n")
        self.write(os.path.join(self.config, "rules", "own.md"), "own rule\n")

    def test_fresh_install_composes_the_config(self):
        self.install()
        for rel in ("rules/harness-core", "rules/harness-profile", "skills/task", "skills/memory",
                    "agents/worker.md", "agents/reviewer.md"):
            self.assertTrue(os.path.islink(os.path.join(self.config, rel)), rel)
        self.assertTrue(os.path.islink(os.path.join(self.home, ".local", "bin", "harness")))
        self.assertTrue(os.path.isfile(os.path.join(self.profile, "profile.json")))
        settings = self.read_settings()
        self.assertNotIn("autoMemoryDirectory", settings)
        gate = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(gate["matcher"], "^(Bash|Monitor|Read|Edit|Write|NotebookEdit|Grep)$|^mcp__")
        self.assertEqual(gate["hooks"][0]["onFailure"], "block")
        for groups in settings["hooks"].values():
            self.assertTrue(groups[0]["hooks"][0]["command"].startswith("/usr/bin/env python3 -I -S "))

    def test_dry_run_writes_nothing_and_names_targets(self):
        self.populate()
        before = tree(self.home)
        run = self.install("--dry-run")
        self.assertEqual(tree(self.home), before)
        self.assertIn(self.config, run.stdout)

    def test_second_run_changes_nothing(self):
        self.populate()
        self.install()
        before = tree(self.home)
        run = self.install()
        self.assertEqual(tree(self.home), before)
        self.assertIn("nothing changed", run.stdout)

    def test_honors_claude_config_dir(self):
        other = os.path.join(self.tmp, "other-config")
        self.install(CLAUDE_CONFIG_DIR=other)
        self.assertTrue(os.path.islink(os.path.join(other, "skills", "task")))
        self.assertFalse(os.path.exists(self.config))

    def test_refuses_a_user_file_and_leaves_nothing(self):
        self.populate()
        self.write(os.path.join(self.config, "skills", "task", "SKILL.md"), "the user's own\n")
        before = tree(self.home)
        run = self.harness("install")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(os.path.join(self.config, "skills", "task"), run.stderr)
        self.assertEqual(tree(self.home), before)

    def test_refuses_broken_settings_and_leaves_nothing(self):
        self.write(self.settings, "{not json")
        before = tree(self.home)
        run = self.harness("install")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("settings.json", run.stderr)
        self.assertEqual(tree(self.home), before)

    def test_failed_self_test_writes_no_hook(self):
        copy = os.path.join(self.tmp, "copy")
        shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        path = os.path.join(copy, "core", "hooks", "gate.py")
        with open(path) as f:
            text = f.read()
        with open(path, "w") as f:
            f.write(text.replace('if __name__ == "__main__":', 'sys.exit(1)\nif __name__ == "__main__":'))
        self.populate()
        before = tree(self.home)
        run = self.harness("install", script=os.path.join(copy, "tools", "harness.py"))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("self-test", run.stderr)
        self.assertEqual(tree(self.home), before)

    def test_keeps_user_keys_hooks_and_memory_setting(self):
        self.populate()
        settings = json.loads(USER_SETTINGS)
        settings["autoMemoryDirectory"] = "~/my-notes"
        self.write(self.settings, json.dumps(settings))
        self.install()
        merged = self.read_settings()
        self.assertEqual(merged["model"], "custom")
        self.assertEqual(merged["env"], {"A": "1"})
        self.assertEqual(merged["autoMemoryDirectory"], "~/my-notes")
        self.assertEqual(merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "true")
        self.assertEqual(len(merged["hooks"]["PreToolUse"]), 2)

    def test_uninstall_restores_the_config_byte_for_byte(self):
        self.populate()
        before = tree(self.config)
        self.install()
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertEqual(tree(self.config), before)
        self.assertTrue(os.path.isfile(os.path.join(self.profile, "profile.json")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".harness", "managed.json")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local")))

    def test_uninstall_keeps_the_folder_and_prints_how_to_remove_it(self):
        self.install()
        run = self.harness("uninstall")
        self.assertEqual(run.returncode, 0)
        root = os.path.join(self.home, ".harness")
        self.assertIn("rm -rf " + root, run.stdout)
        self.assertEqual(sorted(tree(self.home))[0], ".harness")
        shutil.rmtree(root)
        self.assertEqual(tree(self.home), {})

    def test_uninstall_keeps_keys_added_later(self):
        self.populate()
        self.install()
        settings = self.read_settings()
        settings["addedLater"] = True
        self.write(self.settings, json.dumps(settings))
        self.assertEqual(self.harness("uninstall").returncode, 0)
        after = self.read_settings()
        self.assertTrue(after["addedLater"])
        self.assertEqual(after["model"], "custom")
        self.assertNotIn("autoMemoryDirectory", after)
        self.assertEqual([g["hooks"][0]["command"] for g in after["hooks"]["PreToolUse"]], ["true"])
        self.assertNotIn("SessionStart", after["hooks"])

    def test_uninstall_keeps_a_link_the_user_replaced(self):
        self.install()
        path = os.path.join(self.config, "skills", "task")
        os.unlink(path)
        self.write(os.path.join(path, "SKILL.md"), "mine now\n")
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(path, "SKILL.md")))

    def test_profile_is_never_overwritten(self):
        self.install()
        rules = os.path.join(self.profile, "rules", "profile.md")
        self.write(rules, "edited by the user\n")
        self.install()
        with open(rules) as f:
            self.assertEqual(f.read(), "edited by the user\n")

    def test_profile_skill_gets_linked(self):
        self.install()
        self.write(os.path.join(self.profile, "skills", "own", "SKILL.md"), "---\nname: own\ndescription: y\n---\n")
        self.install()
        self.assertTrue(os.path.islink(os.path.join(self.config, "skills", "own")))


class Doctor(HomeCase):
    def test_all_ok(self):
        self.install()
        run = self.harness("doctor")
        self.assertEqual(run.returncode, 0, run.stdout)
        for name in ("python", "claude", "links", "settings", "gate", "weight"):
            self.assertRegex(run.stdout, r"(?m)^OK\s+%s\b" % name)

    def test_broken_link_fails_with_a_fix(self):
        self.install()
        os.unlink(os.path.join(self.config, "skills", "task"))
        run = self.harness("doctor")
        self.assertEqual(run.returncode, 1)
        self.assertRegex(run.stdout, r"FAIL\s+links.*fix: run `harness install`")

    def test_missing_gate_hook_fails(self):
        self.install()
        settings = self.read_settings()
        del settings["hooks"]["PreToolUse"]
        self.write(self.settings, json.dumps(settings))
        self.assertEqual(self.harness("doctor").returncode, 1)

    def test_hook_that_cannot_start_fails(self):
        self.install()
        for path in (self.settings, os.path.join(self.home, ".harness", "managed.json")):
            with open(path) as f:
                text = f.read()
            self.write(path, text.replace("gate.py", "gone.py"))
        self.assertEqual(self.harness("doctor").returncode, 1)

    def test_missing_claude_is_a_warning(self):
        self.install()
        os.unlink(os.path.join(self.stubs, "claude"))
        run = self.harness("doctor")
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertRegex(run.stdout, r"WARN\s+claude")

    def test_old_claude_fails(self):
        self.install()
        self.stub("claude", "#!/bin/sh\necho '2.1.200 (Claude Code)'\n")
        run = self.harness("doctor")
        self.assertEqual(run.returncode, 1)
        self.assertRegex(run.stdout, r"FAIL\s+claude")

    def test_doctor_without_install_fails(self):
        self.assertEqual(self.harness("doctor").returncode, 1)


class InstallScript(GitCase):
    def test_install_twice_doctor_uninstall(self):
        source = self.source()
        first = self.script(source)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("Next:", first.stdout)
        before = tree(self.home)
        second = self.script(source)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(tree(self.home), before)
        cli = os.path.join(self.home, ".local", "bin", "harness")
        run = subprocess.run([cli, "doctor"], env=self.env(), capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        run = subprocess.run([cli, "uninstall"], env=self.env(), capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".harness", "core", ".git")))
        again = self.script(source)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(tree(self.home), before)
        self.assertEqual(self.cli("uninstall").returncode, 0)
        shutil.rmtree(os.path.join(self.home, ".harness"))
        self.assertEqual(tree(self.home), {})

    def test_update_fast_forwards(self):
        source = self.source()
        self.assertEqual(self.script(source).returncode, 0)
        self.write(os.path.join(source, "core", "skills", "extra", "SKILL.md"), "---\nname: extra\ndescription: z\n---\n")
        git = ["git", "-C", source, "-c", "user.name=test", "-c", "user.email=test@example.com"]
        subprocess.run(git + ["add", "-A"], check=True, env=self.env())
        subprocess.run(git + ["commit", "-qm", "more"], check=True, env=self.env())
        cli = os.path.join(self.home, ".local", "bin", "harness")
        run = subprocess.run([cli, "update"], env=self.env(), capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertTrue(os.path.islink(os.path.join(self.config, "skills", "extra")))

    def test_refusal_removes_the_fresh_clone(self):
        source = self.source()
        self.write(os.path.join(self.config, "skills", "task", "SKILL.md"), "the user's own\n")
        before = tree(self.home)
        run = self.script(source)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(os.path.join(self.config, "skills", "task"), run.stderr)
        self.assertEqual(tree(self.home), before)

    def test_refuses_a_core_path_that_is_not_a_clone(self):
        self.write(os.path.join(self.home, ".harness", "core", "file"), "x")
        run = self.script(self.tmp)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("not a git clone", run.stderr)

    def test_script_body_is_inside_main(self):
        with open(os.path.join(REPO, "install.sh")) as f:
            lines = [line.rstrip() for line in f if line.strip()]
        self.assertEqual(lines[-1], 'main "$@"')
        self.assertIn("  set -euo pipefail", lines)
        self.assertTrue(os.access(HARNESS, os.X_OK))


if __name__ == "__main__":
    unittest.main()
