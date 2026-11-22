"""The installer keeps what is not its own: secrets, memory, local commits, protected files, and survives interruption."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from support import HARNESS, GitCase, HomeCase, tree

USER_SETTINGS = '{ "model":"custom",\n  "env": {"FAKE_TOKEN": "x"} }\n'
# Runs `harness install` and stops it at the Nth write: Ctrl-C, or an exit with no cleanup at all, like a hard kill.
INTERRUPT = r"""
import importlib.util, os, shutil, sys
spec = importlib.util.spec_from_file_location("harness_cli", sys.argv[1])
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
stop, mode, count = int(sys.argv[2]), sys.argv[3], [0]
def wrap(module, name):
    real = getattr(module, name)
    def step(*args, **kwargs):
        count[0] += 1
        if count[0] == stop:
            if mode == "kill":
                os._exit(9)
            raise KeyboardInterrupt
        return real(*args, **kwargs)
    setattr(module, name, step)
for name in ("mkdir", "symlink", "replace", "rename", "chmod", "unlink", "rmdir", "fsync", "open"):
    wrap(os, name)
wrap(shutil, "copyfile")
sys.argv = ["harness", "install"]
code = cli.main()
print("steps", count[0])
sys.exit(code)
"""


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class Backup(HomeCase):
    def test_backup_is_owner_only_and_the_folder_closed(self):
        self.write(self.settings, USER_SETTINGS)
        os.chmod(self.settings, 0o644)
        self.install()
        self.assertEqual(mode(os.path.join(self.home, ".harness", "settings.backup.json")), 0o600)
        self.assertEqual(mode(os.path.join(self.home, ".harness")), 0o700)

    def test_a_stricter_source_mode_is_kept(self):
        self.write(self.settings, USER_SETTINGS)
        os.chmod(self.settings, 0o600)
        self.install()
        self.assertEqual(mode(os.path.join(self.home, ".harness", "settings.backup.json")), 0o600)


class LastCopy(HomeCase):
    def restored(self):
        run = self.harness("uninstall")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        with open(self.settings, encoding="utf-8") as f:
            return f.read()

    def test_a_settings_file_that_went_missing_comes_back_from_the_backup(self):
        self.write(self.settings, USER_SETTINGS)
        self.install()
        os.unlink(self.settings)
        self.assertEqual(self.restored(), USER_SETTINGS)

    def kept(self):
        with open(os.path.join(self.home, ".harness", "settings.backup.json.1"), encoding="utf-8") as f:
            return f.read()

    def test_settings_deleted_then_reinstalled_keep_the_original(self):
        self.write(self.settings, USER_SETTINGS)
        self.install()
        os.unlink(self.settings)
        self.install()
        self.restored()
        self.assertEqual(self.kept(), USER_SETTINGS)

    def test_settings_deleted_reinstalled_and_edited_keep_the_original(self):
        self.write(self.settings, USER_SETTINGS)
        self.install()
        os.unlink(self.settings)
        self.install()
        data = self.read_settings()
        data["theme"] = "dark"
        self.write(self.settings, json.dumps(data))
        self.assertEqual(json.loads(self.restored()), {"theme": "dark"})
        self.assertEqual(self.kept(), USER_SETTINGS)

    def test_a_key_the_user_removed_stays_removed_and_the_copy_is_private(self):
        self.write(self.settings, '{"env": {"FAKE_TOKEN": "x"}}\n')
        self.install()
        data = self.read_settings()
        del data["env"]
        self.write(self.settings, json.dumps(data))
        self.assertEqual(json.loads(self.restored()), {})
        kept = os.path.join(self.home, ".harness", "settings.backup.json.1")
        self.assertEqual(mode(kept), 0o600)

    def test_a_key_added_later_needs_no_kept_copy(self):
        self.write(self.settings, USER_SETTINGS)
        self.install()
        data = self.read_settings()
        data["theme"] = "dark"
        self.write(self.settings, json.dumps(data))
        self.restored()
        self.assertFalse(os.path.exists(os.path.join(self.home, ".harness", "settings.backup.json.1")))

    def test_a_later_install_on_a_clean_machine_backs_up_no_hooks(self):
        self.install()
        path = os.path.join(self.home, ".harness", "profile", "profile.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["memory"]["shared"] = True
        self.write(path, json.dumps(data))
        self.install()
        self.assertFalse(os.path.exists(os.path.join(self.home, ".harness", "settings.backup.json")))
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertFalse(os.path.exists(self.settings))

    def test_a_lone_surrogate_survives_uninstall(self):
        self.install()
        data = self.read_settings()
        data["odd"] = "\ud800"
        self.write(self.settings, json.dumps(data))
        run = self.harness("uninstall")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.read_settings(), {"odd": "\ud800"})

    def test_a_config_folder_that_went_missing_comes_back_too(self):
        self.write(self.settings, USER_SETTINGS)
        self.install()
        shutil.rmtree(os.path.dirname(self.settings))
        self.assertEqual(self.restored(), USER_SETTINGS)


class EarlierRelease(HomeCase):
    def harness_dir(self):
        return os.path.join(self.home, ".harness")

    def age_the_hooks(self):
        with open(self.settings, encoding="utf-8") as f:
            text = f.read()
        self.write(self.settings, text.replace(" -I -S ", " -I -S -B "))

    def test_recovery_removes_a_hook_no_manifest_lists(self):
        self.install()
        self.age_the_hooks()
        shutil.rmtree(self.harness_dir())
        self.install()
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertFalse(os.path.exists(self.settings))

    def test_recovery_keeps_the_users_keys(self):
        self.write(self.settings, USER_SETTINGS)
        self.install()
        self.age_the_hooks()
        shutil.rmtree(self.harness_dir())
        self.install()
        self.assertEqual(self.harness("uninstall").returncode, 0)
        data = self.read_settings()
        self.assertEqual(data["model"], "custom")
        self.assertNotIn("hooks", data)

    def test_uninstall_without_a_manifest_removes_the_hooks(self):
        self.write(self.settings, USER_SETTINGS)
        self.install()
        os.unlink(os.path.join(self.harness_dir(), "managed.json"))
        run = self.harness("uninstall")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("hooks", self.read_settings())

    def test_a_hook_of_the_user_is_never_taken_for_ours(self):
        own = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python3 ~/my/gate.py"}]}]}}
        self.write(self.settings, json.dumps(own))
        self.install()
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertEqual(self.read_settings(), own)


class UsersOwnHooks(HomeCase):
    def forms(self):
        gate = os.path.join(self.home, ".harness", "core", "core", "hooks", "gate.py")
        start = gate.replace("gate.py", "session_start.py")
        return ["[ -e %s ] || echo 'gate missing' >&2" % gate, "audit-wrapper %s" % gate,
                "shasum %s >> /tmp/watch" % gate, "python3 %s.local" % gate,
                "python3 %s_wrapper.py" % gate, "python3 /backup%s" % gate, "grep x %s" % start]

    def with_hooks(self, forms):
        groups = [{"hooks": [{"type": "command", "command": c}]} for c in forms]
        return {"hooks": {"PostToolUse": groups}}

    def commands(self):
        data = self.read_settings()
        return [h["command"] for g in data.get("hooks", {}).get("PostToolUse", []) for h in g["hooks"]]

    def test_a_hook_added_later_that_names_our_path_survives_install_and_uninstall(self):
        self.install()
        data = self.read_settings()
        data["hooks"].update(self.with_hooks(self.forms())["hooks"])
        self.write(self.settings, json.dumps(data))
        self.install()
        self.assertEqual(self.commands(), self.forms())
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertEqual(self.commands(), self.forms())

    def test_a_file_holding_only_such_hooks_comes_back(self):
        text = json.dumps(self.with_hooks(self.forms()))
        self.write(self.settings, text)
        self.install()
        self.assertTrue(os.path.exists(os.path.join(self.home, ".harness", "settings.backup.json")))
        self.assertEqual(self.harness("uninstall").returncode, 0)
        with open(self.settings, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)

    def test_a_lost_manifest_keeps_them_too(self):
        self.install()
        data = self.read_settings()
        data["hooks"].update(self.with_hooks(self.forms())["hooks"])
        self.write(self.settings, json.dumps(data))
        os.unlink(os.path.join(self.home, ".harness", "managed.json"))
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertEqual(self.commands(), self.forms())
        self.assertNotIn("PreToolUse", self.read_settings()["hooks"])


class RecoveryAfterCustomizing(GitCase):
    def bin(self):
        return os.path.join(self.home, ".local", "bin", "harness")

    def recover(self, source):
        shutil.rmtree(os.path.join(self.home, ".harness"))
        run = self.script(source)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        run = self.cli("uninstall")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        if os.path.exists(self.settings):       # with no settings before install, none stay
            self.assertNotIn("gate.py", json.dumps(self.read_settings()))

    def dangling(self):
        return [p for p, _, names in os.walk(self.config) for n in names + _
                if os.path.islink(os.path.join(p, n)) and not os.path.exists(os.path.join(p, n))]

    def test_after_overriding_a_core_skill(self):
        source = self.source()
        self.assertEqual(self.script(source).returncode, 0)
        self.write(os.path.join(self.home, ".harness", "profile", "skills", "task", "SKILL.md"),
                   "---\nname: task\ndescription: mine\n---\n")
        self.assertEqual(self.cli("install").returncode, 0)
        self.recover(source)
        self.assertEqual(self.dangling(), [])

    def test_after_overriding_a_core_agent(self):
        source = self.source()
        self.assertEqual(self.script(source).returncode, 0)
        self.write(os.path.join(self.home, ".harness", "profile", "agents", "worker.md"),
                   "---\nname: worker\ndescription: mine\n---\n")
        self.assertEqual(self.cli("install").returncode, 0)
        self.recover(source)
        self.assertEqual(self.dangling(), [])

    def test_after_adding_a_skill_of_a_new_name_no_link_dangles(self):
        source = self.source()
        self.assertEqual(self.script(source).returncode, 0)
        self.write(os.path.join(self.home, ".harness", "profile", "skills", "mine", "SKILL.md"),
                   "---\nname: mine\ndescription: mine\n---\n")
        self.assertEqual(self.cli("install").returncode, 0)
        self.recover(source)
        self.assertEqual(self.dangling(), [])

    def test_a_link_of_the_user_elsewhere_still_refuses(self):
        source = self.source()
        self.assertEqual(self.script(source).returncode, 0)
        shutil.rmtree(os.path.join(self.home, ".harness"))
        path = os.path.join(self.config, "skills", "task")
        os.unlink(path)
        os.symlink(os.path.join(self.tmp, "my-skill"), path)
        self.assertNotEqual(self.script(source).returncode, 0)
        self.assertEqual(os.readlink(path), os.path.join(self.tmp, "my-skill"))

    def test_shared_memory_leaves_with_a_lost_record(self):
        source = self.source()
        self.assertEqual(self.script(source).returncode, 0)
        path = os.path.join(self.home, ".harness", "profile", "profile.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["memory"]["shared"] = True
        self.write(path, json.dumps(data))
        self.assertEqual(self.cli("install").returncode, 0)
        self.assertIn("autoMemoryDirectory", self.read_settings())
        os.unlink(os.path.join(self.home, ".harness", "managed.json"))
        self.assertEqual(self.cli("uninstall").returncode, 0)
        self.assertNotIn("autoMemoryDirectory", self.read_settings())

    def test_shared_memory_leaves_after_the_recovery(self):
        source = self.source()
        self.assertEqual(self.script(source).returncode, 0)
        path = os.path.join(self.home, ".harness", "profile", "profile.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["memory"]["shared"] = True
        self.write(path, json.dumps(data))
        self.assertEqual(self.cli("install").returncode, 0)
        shutil.rmtree(os.path.join(self.home, ".harness"))
        self.assertEqual(self.script(source).returncode, 0)
        self.assertEqual(self.cli("uninstall").returncode, 0)
        self.assertFalse(os.path.exists(self.settings) and "autoMemoryDirectory" in self.read_settings())


class HandSetMemory(HomeCase):
    def test_a_memory_folder_set_by_hand_survives_install(self):
        self.install()
        value = os.path.join(self.home, ".harness", "profile", "memory")
        data = self.read_settings()
        data["autoMemoryDirectory"] = value
        self.write(self.settings, json.dumps(data))
        self.install()
        self.assertEqual(self.read_settings().get("autoMemoryDirectory"), value)

    def test_a_hook_that_differs_by_a_newline_is_the_users(self):
        self.install()
        data = self.read_settings()
        command = data["hooks"]["SessionStart"][0]["hooks"][0]["command"] + "\n"
        data["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": command}]}]
        self.write(self.settings, json.dumps(data))
        self.install()
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertEqual(self.read_settings()["hooks"]["Stop"][0]["hooks"][0]["command"], command)


class ParallelFirstInstalls(GitCase):
    def test_every_parallel_first_install_succeeds(self):
        source = self.source()
        with ThreadPoolExecutor(max_workers=5) as pool:
            runs = list(pool.map(lambda _: self.script(source), range(5)))
        self.assertEqual([r.stderr for r in runs if r.returncode], [])
        names = os.listdir(os.path.join(self.home, ".harness"))
        self.assertEqual([n for n in names if ".clone." in n], [])


class LinkTargetChange(HomeCase):
    def failed_change(self):
        self.install()
        skill = os.path.join(self.home, ".harness", "profile", "skills", "task")
        self.write(os.path.join(skill, "SKILL.md"), "---\nname: task\ndescription: x\n---\n")
        skills = os.path.join(self.home, ".claude", "skills")
        os.chmod(skills, 0o555)
        try:
            self.assertNotEqual(self.harness("install").returncode, 0)
        finally:
            os.chmod(skills, 0o755)
        return skill, os.path.join(skills, "task")

    def test_the_next_install_finishes_the_change(self):
        skill, path = self.failed_change()
        self.install()
        self.assertEqual(os.readlink(path), skill)
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertFalse(os.path.lexists(path))

    def test_uninstall_removes_the_link_that_was_not_replaced(self):
        _, path = self.failed_change()
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertFalse(os.path.lexists(path))


class OneLineErrors(HomeCase):
    def test_settings_nested_too_deep_are_one_line(self):
        self.write(self.settings, '{"a": ' + "[" * 200000 + "]" * 200000 + "}")
        run = self.harness("install")
        self.assertEqual(run.returncode, 1)
        self.assertNotIn("Traceback", run.stderr)

    def test_doctor_warns_when_this_shell_uses_another_config(self):
        other = os.path.join(self.tmp, "other config")
        self.install(CLAUDE_CONFIG_DIR=other)
        run = self.harness("doctor")
        self.assertIn("installed for " + other, run.stdout)
        self.assertRegex(run.stdout, r"WARN\s+config")


class FailedClone(GitCase):
    def test_a_clone_that_fails_leaves_no_folder(self):
        run = self.script(os.path.join(self.tmp, "no-such-repo"))
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".harness")))

    def test_a_relative_path_to_the_same_repo_is_not_another_repo(self):
        self.source()
        self.assertEqual(self.script("./source").returncode, 0)
        run = self.script("./source")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_the_same_repo_with_a_trailing_slash_is_not_another_repo(self):
        source = self.source()
        self.assertEqual(self.script(source).returncode, 0)
        run = self.script(source + "/")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


class Memory(HomeCase):
    def set_shared(self, value):
        path = os.path.join(self.profile, "profile.json")
        with open(path) as f:
            data = json.load(f)
        data["memory"] = {"shared": value}
        self.write(path, json.dumps(data))

    def test_default_install_leaves_per_project_memory_alone(self):
        self.install()
        self.assertNotIn("autoMemoryDirectory", self.read_settings())
        with open(os.path.join(self.profile, "profile.json")) as f:
            self.assertIs(json.load(f)["memory"]["shared"], False)

    def test_opting_in_sets_the_key_and_uninstall_removes_it(self):
        self.write(self.settings, USER_SETTINGS)
        before = tree(self.config)
        self.install()
        self.set_shared(True)
        self.install()
        self.assertEqual(self.read_settings()["autoMemoryDirectory"], os.path.join(self.profile, "memory"))
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertEqual(tree(self.config), before)

    def test_opting_out_again_removes_the_key(self):
        self.install()
        self.set_shared(True)
        self.install()
        self.set_shared(False)
        self.install()
        self.assertNotIn("autoMemoryDirectory", self.read_settings())

    def test_opting_in_keeps_a_value_the_user_set(self):
        self.write(self.settings, '{"autoMemoryDirectory": "~/my-notes"}\n')
        self.install()
        self.set_shared(True)
        self.install()
        self.assertEqual(self.read_settings()["autoMemoryDirectory"], "~/my-notes")
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertEqual(self.read_settings()["autoMemoryDirectory"], "~/my-notes")


class UninstallKeepsTheFolder(GitCase):
    def setUp(self):
        super().setUp()
        self.origin = self.source()
        self.assertEqual(self.script(self.origin).returncode, 0)
        self.root = os.path.join(self.home, ".harness")
        self.clone = os.path.join(self.root, "core")

    def test_local_work_in_the_clone_survives(self):
        self.write(os.path.join(self.clone, "work.txt"), "x")
        self.git(self.clone, "add", "work.txt")
        self.git(self.clone, "commit", "-qm", "work")
        commit = self.git(self.clone, "rev-parse", "HEAD").stdout.strip()
        self.git(self.clone, "checkout", "-q", "--detach", "origin/HEAD")
        self.write(os.path.join(self.clone, "loose.txt"), "y")
        self.assertEqual(self.cli("uninstall").returncode, 0)
        self.assertEqual(self.git(self.clone, "cat-file", "-t", commit).stdout.strip(), "commit")
        self.assertTrue(os.path.isfile(os.path.join(self.clone, "loose.txt")))

    def test_a_file_the_installer_did_not_make_survives(self):
        foreign = os.path.join(self.profile, "foreign.txt")
        self.write(foreign, "not ours\n")
        self.assertEqual(self.cli("uninstall").returncode, 0)
        self.assertTrue(os.path.isfile(foreign))

    def test_uninstall_removes_only_what_it_installed(self):
        run = self.cli("uninstall")
        self.assertIn("rm -rf", run.stdout)
        self.assertEqual(sorted(os.listdir(self.root)), ["core", "profile"])
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".local", "bin", "harness")))

    def test_reinstall_after_uninstall_works(self):
        before = tree(self.home)
        self.assertEqual(self.cli("uninstall").returncode, 0)
        run = self.script(self.origin)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(tree(self.home), before)

    def test_install_recovers_after_the_folder_was_removed_by_hand(self):
        shutil.rmtree(self.root)
        run = self.script(self.origin)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(self.cli("doctor").returncode, 0)

    def test_the_block_message_names_the_recovery(self):
        command = self.read_settings()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        shutil.rmtree(self.root)
        run = subprocess.run(["/bin/sh", "-c", command], input="{}", capture_output=True,
                             text=True, env=self.env())
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("install.sh", run.stderr)


class LinkedProfile(HomeCase):
    def test_uninstall_leaves_a_symlinked_profile_and_its_target(self):
        real = os.path.join(self.tmp, "real-profile")
        shutil.copytree(os.path.join(os.path.dirname(HARNESS), "..", "profile-template"), real)
        os.makedirs(os.path.dirname(self.profile))
        os.symlink(real, self.profile)
        self.install()
        run = self.harness("uninstall")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(os.path.islink(self.profile))
        self.assertTrue(os.path.isfile(os.path.join(real, "profile.json")))


class UninstallOrder(HomeCase):
    def test_the_manifest_goes_last_so_a_killed_run_can_be_repeated(self):
        self.install()
        script = (
            "import importlib.util, os, sys\n"
            "spec = importlib.util.spec_from_file_location('cli', sys.argv[1])\n"
            "cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)\n"
            "real = cli.clear\n"
            "def stop(path):\n"
            "    if path == cli.Paths().manifest: os._exit(9)\n"
            "    real(path)\n"
            "cli.clear = stop\n"
            "sys.argv = ['harness', 'uninstall']\n"
            "cli.main()\n")
        killed = subprocess.run([sys.executable, "-c", script, HARNESS], env=self.env(),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(killed.returncode, 9, killed.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".harness", "managed.json")))
        run = self.harness("uninstall")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Uninstalled", run.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.config, "skills")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".harness", "managed.json")))


class Concurrent(HomeCase):
    def test_parallel_installs_all_succeed(self):
        command = [sys.executable, HARNESS, "install"]
        for _ in range(5):
            runs = [subprocess.Popen(command, env=self.env(), stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True) for _ in range(4)]
            results = [(run.communicate()[1], run.returncode) for run in runs]
            self.assertEqual([code for _, code in results], [0] * 4, results)
            self.assertEqual(self.harness("uninstall").returncode, 0)


class FolderMode(HomeCase):
    def test_an_existing_folder_keeps_its_mode(self):
        root = os.path.join(self.home, ".harness")
        os.mkdir(root)
        os.chmod(root, 0o755)
        self.install()
        self.assertEqual(mode(root), 0o755)
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertEqual(mode(root), 0o755)

    def test_a_linked_folder_keeps_its_target_mode(self):
        real = os.path.join(self.tmp, "real-root")
        os.mkdir(real)
        os.chmod(real, 0o755)
        os.symlink(real, os.path.join(self.home, ".harness"))
        self.install()
        self.assertEqual(mode(real), 0o755)


class Interrupted(HomeCase):
    """Stops install at every write step, with Ctrl-C and with a hard kill."""

    def prepare(self, home):
        config = os.path.join(home, ".claude")
        os.makedirs(os.path.join(config, "rules"))
        with open(os.path.join(config, "settings.json"), "w") as f:
            f.write(USER_SETTINGS)
        with open(os.path.join(config, "rules", "own.md"), "w") as f:
            f.write("own rule\n")
        return config

    def run_in(self, home, *args):
        return subprocess.run([sys.executable] + list(args), env=self.env(HOME=home), cwd=self.tmp,
                              capture_output=True, text=True, timeout=60)

    def interrupt(self, home, stop, how):
        return self.run_in(home, "-c", INTERRUPT, HARNESS, str(stop), how)

    def leftovers(self, home):
        return [p for p in tree(home) if ".harness-tmp" in p]

    def one_case(self, case):
        stop, how, then = case
        home = tempfile.mkdtemp(dir=self.tmp)
        config = self.prepare(home)
        before = tree(config)
        label = "%s at step %d, then %s" % (how, stop, then)
        if self.interrupt(home, stop, how).returncode == 0:
            return "%s: it was not interrupted" % label
        with open(os.path.join(config, "settings.json")) as f:
            json.load(f)                                        # never half written
        if then == "install":
            run = self.run_in(home, HARNESS, "install")
            if run.returncode:
                return "%s: install failed: %s" % (label, run.stderr)
            if "hooks" not in json.load(open(os.path.join(config, "settings.json"))):
                return "%s: install did not finish" % label
        run = self.run_in(home, HARNESS, "uninstall")
        if run.returncode:
            return "%s: uninstall failed: %s" % (label, run.stderr)
        if tree(config) != before:
            return "%s: config not restored" % label
        shutil.rmtree(os.path.join(home, ".harness"), ignore_errors=True)
        extra = sorted(set(tree(home)) - {".claude"} - {os.path.join(".claude", p) for p in before})
        return "%s: left behind %s" % (label, extra) if extra else None

    def installed_release(self, home, change):
        """An install whose hook, or memory setting, the next install has to change."""
        config = self.prepare(home)
        settings, manifest = os.path.join(config, "settings.json"), os.path.join(home, ".harness", "managed.json")
        profile = os.path.join(home, ".harness", "profile", "profile.json")
        self.assertEqual(self.run_in(home, HARNESS, "install").returncode, 0)
        if change == "hook":
            for path in (settings, manifest):
                with open(path, encoding="utf-8") as f:
                    text = f.read()
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text.replace(" -I -S ", " -I -S -B "))
            return settings
        for shared in (True, False):
            with open(profile, encoding="utf-8") as f:
                data = json.load(f)
            data["memory"]["shared"] = shared
            with open(profile, "w", encoding="utf-8") as f:
                json.dump(data, f)
            if shared:
                self.assertEqual(self.run_in(home, HARNESS, "install").returncode, 0)
        return settings

    def one_update(self, case):
        change, stop, how = case
        home = tempfile.mkdtemp(dir=self.tmp)
        settings = self.installed_release(home, change)
        self.interrupt(home, stop, how)
        run = self.run_in(home, HARNESS, "uninstall")
        label = "%s, %s at step %d" % (change, how, stop)
        if run.returncode:
            return "%s: uninstall failed: %s" % (label, run.stderr)
        with open(settings, encoding="utf-8") as f:
            text = f.read()
        return "%s: left %s" % (label, text) if "gate.py" in text or "autoMemoryDirectory" in text else None

    def test_an_update_stopped_at_any_step_can_still_be_uninstalled(self):
        for change in ("hook", "memory"):
            home = tempfile.mkdtemp(dir=self.tmp)
            self.installed_release(home, change)
            full = self.interrupt(home, 0, "none")
            self.assertEqual(full.returncode, 0, full.stderr)
            steps = int(full.stdout.split("steps")[-1])
            cases = [(change, stop, how) for stop in range(1, steps + 1) for how in ("interrupt", "kill")]
            with ThreadPoolExecutor(max_workers=6) as pool:
                self.assertEqual([p for p in pool.map(self.one_update, cases) if p], [])

    def test_every_write_step_can_be_interrupted(self):
        home = tempfile.mkdtemp(dir=self.tmp)
        self.prepare(home)
        full = self.interrupt(home, 0, "none")
        self.assertEqual(full.returncode, 0, full.stderr)
        steps = int(full.stdout.split("steps")[-1])
        self.assertGreater(steps, 20)
        cases = [(stop, how, then) for stop in range(1, steps + 1) for how in ("interrupt", "kill")
                 for then in ("install", "uninstall")]
        with ThreadPoolExecutor(max_workers=6) as pool:
            problems = [p for p in pool.map(self.one_case, cases) if p]
        self.assertEqual(problems, [])


class Protected(HomeCase):
    def test_a_write_protected_settings_file_is_refused(self):
        self.write(self.settings, USER_SETTINGS)
        os.chmod(self.settings, 0o444)
        self.addCleanup(os.chmod, self.settings, 0o644)
        before = tree(self.home)
        run = self.harness("install")
        self.assertEqual(run.returncode, 1)
        self.assertIn("write-protected", run.stderr)
        self.assertEqual(tree(self.home), before)

    def test_uninstall_also_refuses_a_settings_file_protected_later(self):
        self.write(self.settings, USER_SETTINGS)
        self.install()
        os.chmod(self.settings, 0o444)
        self.addCleanup(os.chmod, self.settings, 0o644)
        run = self.harness("uninstall")
        self.assertEqual(run.returncode, 1)
        self.assertIn("write-protected", run.stderr)

    def test_a_hard_linked_settings_file_is_refused(self):
        self.write(self.settings, USER_SETTINGS)
        other = os.path.join(self.tmp, "other-name.json")
        os.link(self.settings, other)
        run = self.harness("install")
        self.assertEqual(run.returncode, 1)
        self.assertIn("hard links", run.stderr)
        self.assertEqual(os.stat(self.settings).st_ino, os.stat(other).st_ino)


class SettingsRestore(HomeCase):
    def test_a_value_changed_from_1_to_true_survives_uninstall(self):
        self.write(self.settings, '{"flag": 1}\n')
        self.install()
        data = self.read_settings()
        data["flag"] = True
        self.write(self.settings, json.dumps(data))
        self.assertEqual(self.harness("uninstall").returncode, 0)
        self.assertIs(self.read_settings()["flag"], True)

    def test_the_users_empty_event_survives_when_they_edited_settings(self):
        self.write(self.settings, '{"hooks": {"Stop": []}}\n')
        self.install()
        data = self.read_settings()
        data["addedLater"] = 1
        self.write(self.settings, json.dumps(data))
        self.assertEqual(self.harness("uninstall").returncode, 0)
        after = self.read_settings()
        self.assertEqual(after["hooks"], {"Stop": []})
        self.assertEqual(after["addedLater"], 1)

    def test_an_empty_hooks_key_is_restored_byte_for_byte(self):
        text = '{ "hooks": {} }\n'
        self.write(self.settings, text)
        self.install()
        self.assertEqual(self.harness("uninstall").returncode, 0)
        with open(self.settings) as f:
            self.assertEqual(f.read(), text)


class CleanErrors(HomeCase):
    def assertOneLine(self, run):
        self.assertEqual(run.returncode, 1)
        self.assertNotIn("Traceback", run.stderr)
        self.assertEqual(len(run.stderr.strip().splitlines()), 1, run.stderr)

    def test_a_corrupt_manifest_is_one_line(self):
        self.install()
        manifest = os.path.join(self.home, ".harness", "managed.json")
        for text in ("{broken", "[]", '{"links": 1}'):
            self.write(manifest, text)
            for command in ("install", "uninstall", "doctor"):
                with self.subTest(text=text, command=command):
                    self.assertOneLine(self.harness(command))

    @unittest.skipIf(os.getuid() == 0, "root ignores file modes")
    def test_a_read_only_config_is_one_line(self):
        os.makedirs(self.config)
        os.chmod(self.config, 0o500)
        self.addCleanup(os.chmod, self.config, 0o700)
        self.assertOneLine(self.harness("install"))


class Python(HomeCase):
    def test_the_hook_does_not_pin_the_interpreter(self):
        self.install()
        for groups in self.read_settings()["hooks"].values():
            command = groups[0]["hooks"][0]["command"]
            self.assertTrue(command.startswith("/usr/bin/env python3 -I -S "), command)
            self.assertNotIn(sys.executable, command)

    def test_the_hook_survives_the_install_interpreter_going_away(self):
        self.install()
        other = os.path.join(self.tmp, "other-python")
        os.makedirs(other)
        os.symlink(sys.executable, os.path.join(other, "python3"))
        command = self.read_settings()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push --force"}, "cwd": "/tmp"})
        run = subprocess.run(["/bin/sh", "-c", command], input=event, capture_output=True, text=True,
                             env=self.env(PATH=other + ":/usr/bin:/bin"))
        self.assertEqual(json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_no_python_on_path_exits_non_zero_so_the_call_is_blocked(self):
        self.install()
        command = self.read_settings()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        run = subprocess.run(["/bin/sh", "-c", command], input="{}", capture_output=True, text=True,
                             env=self.env(PATH=empty))
        self.assertNotEqual(run.returncode, 0)

    def test_doctor_fails_on_a_python_below_the_minimum(self):
        self.install()
        self.stub("python3", "#!/bin/sh\necho 3.8.10\nexit 1\n")
        run = self.harness("doctor")
        self.assertEqual(run.returncode, 1)
        self.assertRegex(run.stdout, r"FAIL\s+python\s+3\.8\.10 is below 3\.9.*fix: install Python 3\.9")


class PatternsAndDoctor(HomeCase):
    def set_gate(self, gate):
        path = os.path.join(self.profile, "profile.json")
        with open(path) as f:
            data = json.load(f)
        data["gate"] = gate
        self.write(path, json.dumps(data))

    def test_a_pattern_matching_the_gate_samples_does_not_break_install_or_doctor(self):
        self.install()
        self.set_gate({"ask": [], "deny": [r"/project(/|\s|$)"]})
        self.install()
        run = self.harness("doctor")
        self.assertEqual(run.returncode, 0, run.stdout)
        self.assertRegex(run.stdout, r"OK\s+gate")

    def test_doctor_fails_when_all_hooks_are_disabled(self):
        self.install()
        settings = self.read_settings()
        settings["disableAllHooks"] = True
        self.write(self.settings, json.dumps(settings))
        run = self.harness("doctor")
        self.assertEqual(run.returncode, 1)
        self.assertRegex(run.stdout, r"FAIL\s+gate.*disableAllHooks")

    def test_doctor_names_the_profile_for_a_broken_pattern(self):
        self.install()
        self.set_gate({"ask": [], "deny": "/secret"})
        run = self.harness("doctor")
        self.assertEqual(run.returncode, 1)
        self.assertRegex(run.stdout, r"FAIL\s+gate.*list of regular expressions.*fix: correct gate\.ask")


class InstallScriptEdges(GitCase):
    def test_a_partial_clone_names_the_fix(self):
        source = self.source()
        os.makedirs(os.path.join(self.home, ".harness", "core", ".git"))
        run = self.script(source)
        self.assertEqual(run.returncode, 1)
        self.assertIn("broken git clone", run.stderr)
        self.assertIn("Move it away", run.stderr)

    def test_a_clone_on_a_detached_commit_stays_there_and_update_says_why(self):
        source = self.source()
        self.assertEqual(self.script(source).returncode, 0)
        clone = os.path.join(self.home, ".harness", "core")
        commit = self.git(clone, "rev-parse", "HEAD").stdout.strip()
        self.git(clone, "checkout", "-q", "--detach")
        self.write(os.path.join(source, "later"), "x")
        self.git(source, "add", "later")
        self.git(source, "commit", "-qm", "later")
        again = self.script(source)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("detached", again.stdout)
        self.assertEqual(self.git(clone, "rev-parse", "HEAD").stdout.strip(), commit)
        update = self.cli("update")
        self.assertEqual(update.returncode, 1)
        self.assertIn("detached", update.stderr)
        self.assertNotIn("Traceback", update.stderr)


class NumbersInSettings(HomeCase):
    def test_a_number_a_rewrite_would_change_is_refused_before_any_write(self):
        for text in ("1e999", "-1e999", "NaN", "Infinity", "-Infinity", "0.12345678901234567890123"):
            with self.subTest(text=text):
                self.write(self.settings, '{"cleanupPeriodDays": %s}\n' % text)
                before = tree(self.home)
                run = self.harness("install")
                self.assertEqual(run.returncode, 1)
                self.assertIn(text, run.stderr)
                self.assertEqual(len(run.stderr.strip().splitlines()), 1)
                self.assertEqual(tree(self.home), before)

    def test_an_exponent_too_large_for_a_decimal_is_refused(self):
        self.write(self.settings, '{"a": 1e-99999999999999999999}\n')
        run = self.harness("install")
        self.assertEqual(run.returncode, 1)
        self.assertNotIn("Traceback", run.stderr)

    def test_numbers_that_survive_a_rewrite_install_and_restore(self):
        text = '{"a": 0.5, "b": 1e3, "c": -2.25, "d": 12345678901234567890, "e": 0.1}\n'
        self.write(self.settings, text)
        self.install()
        self.assertEqual(self.read_settings()["d"], 12345678901234567890)
        self.assertEqual(self.harness("uninstall").returncode, 0)
        with open(self.settings) as f:
            self.assertEqual(f.read(), text)


class InstallScriptRerun(GitCase):
    def setUp(self):
        super().setUp()
        self.origin = self.source()
        self.assertEqual(self.script(self.origin).returncode, 0)
        self.core = os.path.join(self.home, ".harness", "core")

    def head(self, path):
        return self.git(path, "rev-parse", "HEAD").stdout.strip()

    def commit(self, path, name):
        self.write(os.path.join(path, name), "x\n")
        self.git(path, "add", "-A")
        self.git(path, "commit", "-qm", name)

    def test_another_repo_is_refused_with_the_line_that_switches(self):
        other = os.path.join(self.tmp, "fork")
        shutil.copytree(self.origin, other)
        run = self.script(other)
        self.assertEqual(run.returncode, 1)
        self.assertIn("remote set-url origin", run.stderr)
        self.assertIn(other, run.stderr)
        self.assertEqual(self.git(self.core, "remote", "get-url", "origin").stdout.strip(), self.origin)

    def test_updated_is_printed_only_when_head_moved(self):
        self.commit(self.core, "local.txt")
        self.assertNotIn("Updated", self.script(self.origin).stdout)

    def test_a_new_upstream_commit_is_pulled_and_reported(self):
        self.commit(self.origin, "upstream.txt")
        run = self.script(self.origin)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("Updated", run.stdout)
        self.assertEqual(self.head(self.core), self.head(self.origin))

    def test_no_network_warns_and_still_installs(self):
        moved = self.origin + "-gone"
        os.rename(self.origin, moved)
        os.unlink(os.path.join(self.config, "rules", "harness-core"))
        run = self.script(self.origin)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("could not reach", run.stderr)
        self.assertTrue(os.path.islink(os.path.join(self.config, "rules", "harness-core")))

    def test_a_diverged_clone_warns_keeps_its_commit_and_installs(self):
        self.commit(self.core, "local.txt")
        local = self.head(self.core)
        self.commit(self.origin, "upstream.txt")
        run = self.script(self.origin)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("could not fast-forward", run.stderr)
        self.assertEqual(self.head(self.core), local)

    def test_a_partial_tree_names_the_fix_and_changes_nothing(self):
        os.unlink(os.path.join(self.core, "core", "settings.json"))
        with open(self.settings, "rb") as f:
            before = f.read()
        run = self.script(self.origin)
        self.assertEqual(run.returncode, 1)
        self.assertIn("core/settings.json", run.stderr)
        self.assertIn("checkout -- .", run.stderr)
        self.assertNotIn("Traceback", run.stderr)
        with open(self.settings, "rb") as f:
            self.assertEqual(f.read(), before)


if __name__ == "__main__":
    unittest.main()
