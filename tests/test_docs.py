"""The instructions an agent follows, and the claims a reader can check."""
import os
import re
import tempfile
import unittest

from support import REPO
import gate

NONE = {"ask": [], "deny": []}
LIMITS = ("python3 read_key.py", "f=.en; cat ${f}v", "cat $'\\x2eenv'", "echo cm0gLXJmIHg= | base64 -d | sh",
          "cp /bin/rm r; ./r -rf x", "sudo cp ssh -i .env dst/", "cat {.env,a}", "cd ~/.config/gh && cat hosts.yml", "grep -r TOKEN .",
          "shopt -s dotglob; cat *", "cd sub && cat 'my dir/.env'", "cat${IFS}.env", "echo ${x:- #}; rm -rf x", "R=rm; $R -rf target",
          "${SHELL:-sh} -c 'rm -rf target'", "rm --rec --for t", "git reset --har")
FALSE_QUESTIONS = ("git diff $(git merge-base main HEAD)", "echo rm -rf build", "python3 -m app.env",
                   "zip -r a.zip . -x '*.git*' '.env'",
                   'grep -rn "rm -rf" . | xargs wc -l')


def read(*parts):
    with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
        return f.read()


def step(number):
    """The text of one numbered section of AGENTS.md."""
    return re.search(r"^## %d\. .*?(?=^## |\Z)" % number, read("AGENTS.md"), re.M | re.S).group(0)


def code(text):
    return re.search(r"```bash\n(.*?)```", text, re.S).group(1)


class AgentInstructions(unittest.TestCase):
    def test_the_gate_proof_plants_nothing_and_passes_the_gate_it_tests(self):
        text = step(8)
        block = code(text)
        self.assertNotIn("printf", block)
        self.assertIn("cat $demo/.env", block)
        self.assertIn("harness gate", text)
        self.assertIsNone(gate.decide({"tool_name": "Bash", "tool_input": {"command": block}, "cwd": "/tmp"}, NONE)[0])

    def test_the_proof_step_says_hooks_apply_to_the_running_session(self):
        self.assertIn("applies hook changes to a running session", step(8))
        self.assertNotIn("Hooks load when a session starts", read("AGENTS.md"))

    def test_the_dry_run_passes_the_gate_on_a_rerun(self):
        block = code(step(3))
        self.assertNotIn("rm -rf", block)
        self.assertIsNone(gate.decide({"tool_name": "Bash", "tool_input": {"command": block}, "cwd": "/tmp"}, NONE)[0])

    def test_the_proof_step_has_a_route_without_a_login(self):
        text = step(8)
        self.assertIn("no login", text)
        self.assertIn("`gate` line of `harness doctor`", text)
        self.assertIn("which proof you used", text)

    def test_setup_asks_five_questions(self):
        self.assertEqual(len(re.findall(r"^\d\. ", step(5), re.M)), 5)

    def test_the_documented_folder_pattern_catches_relative_paths(self):
        for text in (step(6), read("docs", "customize.md")):
            self.assertIn(r"(^|[/\\s])finance-archive(/|\\s|$)", text)


class Memory(unittest.TestCase):
    def test_every_doc_states_the_effect_of_shared_memory(self):
        self.assertIn("notes from one project load in every project", step(6))
        self.assertIn("notes from one project load in every project", read("docs", "customize.md"))
        self.assertIn("memory.shared", read("core", "skills", "memory", "SKILL.md"))

    def test_the_skill_is_right_in_both_modes(self):
        text = read("core", "skills", "memory", "SKILL.md")
        self.assertIn("one folder per project", text)
        self.assertIn("~/.harness/profile/memory/", text)


class Claims(unittest.TestCase):
    def test_the_task_skill_says_who_is_reminded(self):
        text = read("core", "skills", "task", "SKILL.md")
        self.assertNotIn("a new session is reminded", text)
        self.assertIn("resuming it brings the reminder back", text)
        self.assertIn("harness task list", text)

    def test_platforms_are_named(self):
        self.assertIn("macOS and Linux; Windows is not supported", read("README.md"))

    def test_the_known_limits_are_listed(self):
        text = read("docs", "architecture.md")
        for limit in LIMITS + ("a `Grep` over a whole directory", "PowerShell"):
            self.assertIn(limit, text)

    def test_every_listed_limit_really_gets_through(self):
        with tempfile.TemporaryDirectory() as folder:
            for command in LIMITS:
                event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": folder}
                self.assertIsNone(gate.decide(event, NONE)[0], command)

    def test_every_listed_false_question_really_asks(self):
        text = read("docs", "architecture.md")
        with tempfile.TemporaryDirectory() as folder:
            for command in FALSE_QUESTIONS:
                self.assertIn(command, text)
                event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": folder}
                self.assertEqual(gate.decide(event, NONE)[0], "ask", command)

    def test_uninstall_is_documented_as_keeping_the_folder(self):
        for name in ("README.md", "AGENTS.md", os.path.join("docs", "architecture.md"),
                     os.path.join(".github", "workflows", "ci.yml")):
            self.assertNotIn("--purge", read(name), name)
        self.assertIn("rm -rf ~/.harness", read("README.md"))

    def test_the_byte_exact_claim_is_scoped_to_an_intact_folder(self):
        self.assertIn("and `~/.harness` is still there, uninstall puts", read("README.md"))

    def test_forking_after_install_documents_the_switch(self):
        self.assertIn("git -C ~/.harness/core remote set-url origin", read("docs", "customize.md"))

    def test_the_readme_names_the_recovery_when_the_folder_was_deleted(self):
        self.assertIn("was deleted without uninstalling", read("README.md"))

    def test_the_readme_says_what_is_left_out(self):
        self.assertIn("your accounts and infrastructure", read("README.md"))


class Workflow(unittest.TestCase):
    def test_jobs_are_pinned_so_the_minimum_python_exists(self):
        text = read(".github", "workflows", "ci.yml")
        self.assertNotIn("ubuntu-latest", text)
        self.assertRegex(text, r'os: ubuntu-24\.04, python: "3\.9"')

    def test_the_live_job_runs_on_both_systems(self):
        text = read(".github", "workflows", "ci.yml")
        live = text[text.index("  live:"):]
        self.assertIn("os: [ubuntu-24.04, macos-latest]", live)
        self.assertIn("runs-on: ${{ matrix.os }}", live)

    def test_actions_are_pinned_by_commit(self):
        uses = re.findall(r"uses: (\S+)", read(".github", "workflows", "ci.yml"))
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
