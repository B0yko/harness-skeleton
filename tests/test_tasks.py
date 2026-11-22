import json
import os
import subprocess
import sys
import unittest

from support import SESSION_START, HomeCase


class Tasks(HomeCase):
    def setUp(self):
        super().setUp()
        self.install()

    def task(self, *args, **env):
        return self.harness("task", *args, **env)

    def stored(self, task_id):
        with open(os.path.join(self.profile, "state", "tasks", task_id + ".json")) as f:
            return json.load(f)

    def test_new_set_show_list(self):
        self.assertEqual(self.task("new", "fix-login", "--title", "Fix it", "--next", "Reproduce").returncode, 0)
        self.assertEqual(self.stored("fix-login")["version"], 1)
        self.assertEqual(self.task("set", "fix-login", "--expect", "1", "--status", "RUNNING").returncode, 0)
        task = self.stored("fix-login")
        self.assertEqual((task["version"], task["status"], task["next_step"]), (2, "RUNNING", "Reproduce"))
        self.assertEqual(json.loads(self.task("show", "fix-login").stdout)["status"], "RUNNING")
        self.assertIn("fix-login", self.task("list").stdout)

    def test_version_conflict_exits_3_and_writes_nothing(self):
        self.task("new", "a-task")
        self.task("set", "a-task", "--next", "one")
        run = self.task("set", "a-task", "--expect", "1", "--next", "stale")
        self.assertEqual(run.returncode, 3)
        self.assertEqual(self.stored("a-task")["next_step"], "one")

    def test_done_needs_evidence(self):
        self.task("new", "a-task")
        run = self.task("set", "a-task", "--status", "DONE")
        self.assertEqual(run.returncode, 1)
        self.assertIn("evidence", run.stderr)
        self.assertEqual(self.task("set", "a-task", "--status", "DONE", "--evidence", "  ").returncode, 1)
        self.assertEqual(self.stored("a-task")["status"], "TODO")
        self.assertEqual(self.task("set", "a-task", "--status", "DONE", "--evidence", "tests: 5 passed").returncode, 0)
        self.assertEqual(self.stored("a-task")["evidence"], ["tests: 5 passed"])

    def test_bad_id_and_duplicates_are_refused(self):
        self.assertEqual(self.task("new", "../escape").returncode, 1)
        self.task("new", "a-task")
        self.assertEqual(self.task("new", "a-task").returncode, 1)

    def test_writes_leave_no_temp_files(self):
        self.task("new", "a-task")
        self.task("set", "a-task", "--next", "x")
        names = os.listdir(os.path.join(self.profile, "state", "tasks"))
        self.assertEqual(sorted(names), ["a-task.json"])

    def test_a_task_file_of_the_wrong_shape_is_one_line(self):
        self.task("new", "a-task")
        path = os.path.join(self.profile, "state", "tasks", "a-task.json")
        for text in ("[]", "1", '{"id": "a-task"}', '{"id":"a","title":"","status":"TODO",'
                     '"next_step":"","evidence":1,"version":"x"}'):
            self.write(path, text)
            for args in (("list",), ("show", "a-task"), ("set", "a-task", "--next", "x")):
                with self.subTest(text=text, args=args):
                    run = self.task(*args)
                    self.assertEqual(run.returncode, 1)
                    self.assertNotIn("Traceback", run.stderr)
                    self.assertEqual(len(run.stderr.strip().splitlines()), 1, run.stderr)

    def test_concurrent_writers_with_expect_one_wins(self):
        self.task("new", "a-task")
        cmd = [sys.executable, os.path.join(os.path.dirname(SESSION_START), "..", "..", "tools", "harness.py"),
               "task", "set", "a-task", "--expect", "1", "--next", "x"]
        runs = [subprocess.Popen(cmd, env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for _ in range(6)]
        codes = sorted(run.wait() for run in runs)
        self.assertEqual(codes, [0, 3, 3, 3, 3, 3])
        self.assertEqual(self.stored("a-task")["version"], 2)

    def session_start(self, session):
        return subprocess.run([sys.executable, "-I", "-S", SESSION_START, "--profile", self.profile],
                              input=json.dumps({"session_id": session, "hook_event_name": "SessionStart"}),
                              capture_output=True, text=True, env=self.env())

    def test_bind_and_session_start(self):
        self.task("new", "a-task", "--title", "A task", "--next", "Write the test")
        self.assertEqual(self.session_start("sess-1").stdout, "")
        self.assertEqual(self.task("bind", "a-task").returncode, 1)     # no session id
        self.assertEqual(self.task("bind", "a-task", CLAUDE_CODE_SESSION_ID="sess-1").returncode, 0)
        output = json.loads(self.session_start("sess-1").stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        self.assertIn("a-task", output["additionalContext"])
        self.assertIn("Write the test", output["additionalContext"])
        self.assertEqual(self.session_start("sess-2").stdout, "")
        self.assertEqual(self.session_start("../sess-1").stdout, "")
        shown = self.task("show", CLAUDE_CODE_SESSION_ID="sess-1")
        self.assertEqual(json.loads(shown.stdout)["id"], "a-task")

    def test_a_binding_reminds_the_resumed_session_and_not_a_new_one(self):
        self.task("new", "a-task", "--next", "Continue")
        self.task("bind", "a-task", CLAUDE_CODE_SESSION_ID="first")
        self.assertIn("a-task", self.session_start("first").stdout)       # the same id comes back on resume
        self.assertEqual(self.session_start("brand-new").stdout, "")
        self.assertIn("a-task", self.task("list").stdout)                 # how a new session finds it


class Weight(HomeCase):
    def test_default_install_is_within_budget(self):
        self.install()
        run = self.harness("weight", "--json")
        self.assertEqual(run.returncode, 0, run.stdout)
        data = json.loads(run.stdout)
        self.assertLessEqual(data["loaded_chars"], 8000)
        self.assertGreater(data["parts"]["rules"], 500)      # the linked rule folders are counted
        self.assertEqual(data["parts"]["memory"], 0)         # per-project memory is not a fixed load
        self.assertEqual(data["hooks"], 2)

    def test_over_budget_exits_1(self):
        self.install()
        self.write(os.path.join(self.config, "CLAUDE.md"), "x" * 9000)
        run = self.harness("weight")
        self.assertEqual(run.returncode, 1)
        self.assertIn("OVER", run.stdout)
        doctor = self.harness("doctor")
        self.assertRegex(doctor.stdout, r"WARN\s+weight")

    def test_hook_budget(self):
        self.install()
        path = os.path.join(self.profile, "profile.json")
        with open(path) as f:
            profile = json.load(f)
        profile["budgets"]["hooks"] = 1
        self.write(path, json.dumps(profile))
        self.assertEqual(self.harness("weight").returncode, 1)

    def test_budgets_of_the_wrong_shape_are_one_line(self):
        self.install()
        path = os.path.join(self.profile, "profile.json")
        for text in ('{"budgets": 1}', '{"budgets": {"hooks": "x"}}', "[]"):
            self.write(path, text)
            with self.subTest(text=text):
                run = self.harness("weight")
                self.assertEqual(run.returncode, 1)
                self.assertNotIn("Traceback", run.stderr)
                self.assertEqual(len(run.stderr.strip().splitlines()), 1, run.stderr)
                doctor = self.harness("doctor")
                self.assertEqual(doctor.returncode, 1)
                self.assertNotIn("Traceback", doctor.stdout + doctor.stderr)

    def test_weight_reads_the_config_the_install_used(self):
        other = os.path.join(self.tmp, "other-config")
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.install(CLAUDE_CONFIG_DIR=other)
        data = json.loads(self.harness("weight", "--json").stdout)     # run without the variable
        self.assertGreater(data["parts"]["rules"], 500)
        self.assertEqual(data["hooks"], 2)
        self.assertEqual(self.harness("doctor").returncode, 0)

    def test_only_the_first_200_lines_of_shared_memory_count(self):
        self.install()
        path = os.path.join(self.profile, "profile.json")
        with open(path) as f:
            profile = json.load(f)
        profile["memory"]["shared"] = True
        self.write(path, json.dumps(profile))
        self.install()
        self.write(os.path.join(self.profile, "memory", "MEMORY.md"), "abcd\n" * 1000)
        data = json.loads(self.harness("weight", "--json").stdout)
        self.assertEqual(data["parts"]["memory"], 1000)


if __name__ == "__main__":
    unittest.main()
