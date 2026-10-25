#!/usr/bin/env python3
"""The installer: symlinked checkout paths (macOS /tmp), staging only what it writes,
reporting conflicts before it overwrites, and keeping the repository's own git hooks."""
import atexit
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

# Every temporary file this file and what it runs make goes under one directory,
# removed at exit: the suite left hundreds of directories in /tmp, and a run's /tmp
# ran out of inodes. Once per process: a test file another one imports keeps it.
if "IL_TEST_TMP" not in os.environ:
    os.environ["IL_TEST_TMP"] = os.environ["TMPDIR"] = tempfile.mkdtemp(prefix="il-test-")
    os.environ["ISSUELOOPER_TMP"] = os.path.join(os.environ["IL_TEST_TMP"], "scratch")
    atexit.register(shutil.rmtree, os.environ["IL_TEST_TMP"], True)
tempfile.tempdir = os.environ["IL_TEST_TMP"]

ROOT = pathlib.Path(__file__).resolve().parents[3]


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check).stdout


class InstallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="install-test-")
        self.base = pathlib.Path(self._tmp.name)
        # install.sh only checks that gh exists; labels are skipped (--no-labels).
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "gh").chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")

    def tearDown(self):
        self._tmp.cleanup()

    def repo(self, name="repo"):
        repo = self.base / name
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "user.name", "t")
        git(repo, "config", "user.email", "t@t")
        git(repo, "config", "commit.gpgsign", "false")
        (repo / "README.md").write_text("project\n")
        git(repo, "add", "README.md")
        git(repo, "commit", "-qm", "init")
        return repo

    def install(self, repo, *args):
        return subprocess.run([str(ROOT / "install.sh"), str(repo), "--no-labels", *args],
                              capture_output=True, text=True, check=False, env=self.env)

    def staged(self, repo):
        return set(git(repo, "diff", "--cached", "--name-only").split())

    def untracked(self, repo):
        return set(git(repo, "ls-files", "--others", "--exclude-standard").split())

    def test_install_through_symlink(self):
        repo = self.base / "repo"
        repo.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(repo, target_is_directory=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        result = self.install(alias, "--no-commit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((repo / ".claude/hooks/role_guard.py").is_file())
        self.assertTrue((repo / ".agents/skills/issuelooper-worker/SKILL.md").is_file())

    def test_stages_only_what_it_writes(self):
        repo = self.repo()
        private = {".claude/private-notes.txt": "secret plans\n", ".agents/local.json": "{}\n",
                   ".issuelooper/notes.md": "mine\n", ".githooks/README": "mine\n"}
        for path, text in private.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(text)
        result = self.install(repo, "--no-commit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        staged = self.staged(repo)
        self.assertFalse(staged & set(private), staged & set(private))
        self.assertTrue(set(private) <= self.untracked(repo))
        for path, text in private.items():
            self.assertEqual((repo / path).read_text(), text)
        self.assertTrue({".claude/hooks/ccpool.py", ".claude/skills/issuelooper-worker/SKILL.md",
                         ".agents/skills/issuelooper-worker", ".githooks/pre-commit", ".githooks/_chain",
                         ".issuelooper/config.toml", ".claude/settings.json", ".gitignore", "CLAUDE.md"} <= staged)
        self.assertFalse(any(p.endswith("_test.py") for p in staged))

        repo2 = self.repo("repo2")
        (repo2 / ".claude").mkdir()
        (repo2 / ".claude/private-notes.txt").write_text("secret plans\n")
        result = self.install(repo2)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        committed = set(git(repo2, "ls-files").split())
        self.assertNotIn(".claude/private-notes.txt", committed)
        self.assertIn(".claude/hooks/ccpool.py", committed)

    def test_upgrade_stages_only_what_it_writes(self):
        repo = self.repo()
        self.assertEqual(self.install(repo).returncode, 0)
        (repo / ".claude/private-notes.txt").write_text("secret plans\n")
        (repo / ".issuelooper/config.toml").write_text("# the owner's settings\n")
        git(repo, "add", ".issuelooper/config.toml")
        git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "configure")
        hook = repo / ".claude/hooks/claim.py"
        hook.write_text(hook.read_text() + "# an old version\n")
        git(repo, "add", ".claude/hooks/claim.py")
        git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "old framework")
        result = self.install(repo, "--upgrade", "--no-commit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.staged(repo), {".claude/hooks/claim.py"})
        self.assertIn(".claude/private-notes.txt", self.untracked(repo))
        self.assertEqual((repo / ".issuelooper/config.toml").read_text(), "# the owner's settings\n")

    def test_an_upgrade_with_nothing_new_succeeds(self):
        repo = self.repo()
        self.assertEqual(self.install(repo).returncode, 0)
        head = git(repo, "rev-parse", "HEAD")
        result = self.install(repo, "--upgrade")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("already up to date", result.stdout)
        self.assertEqual(git(repo, "rev-parse", "HEAD"), head)

    def test_conflicts_are_reported_before_writing(self):
        repo = self.repo()
        (repo / ".claude/hooks").mkdir(parents=True)
        (repo / ".claude/hooks/claim.py").write_text("# my own claim script\n")
        result = self.install(repo, "--no-commit")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".claude/hooks/claim.py", result.stderr)
        self.assertIn("nothing was written", result.stderr)
        self.assertEqual((repo / ".claude/hooks/claim.py").read_text(), "# my own claim script\n")
        self.assertFalse((repo / ".claude/hooks/ccpool.py").exists())
        result = self.install(repo, "--no-commit", "--force")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual((repo / ".claude/hooks/claim.py").read_text(), "# my own claim script\n")

        repo2 = self.repo("repo2")
        self.assertEqual(self.install(repo2).returncode, 0)
        notes = repo2 / ".claude/skills/issuelooper-worker/my-notes.md"
        notes.write_text("mine\n")
        result = self.install(repo2, "--upgrade", "--no-commit")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".claude/skills/issuelooper-worker/my-notes.md", result.stderr)
        self.assertEqual(notes.read_text(), "mine\n")

    def hooks_dir(self, repo, where):
        d = repo / where
        d.mkdir(parents=True, exist_ok=True)
        for name in ("pre-commit", "commit-msg", "pre-push"):
            (d / name).write_text(f'#!/bin/sh\necho ran >> "{self.base}/{name}.ran"\n')
            (d / name).chmod(0o755)
        return d

    def test_a_custom_hooks_path_is_kept_running(self):
        repo = self.repo()
        self.hooks_dir(repo, ".husky")
        git(repo, "config", "core.hooksPath", ".husky")
        result = self.install(repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".husky", result.stderr)
        self.assertIn("--chain-hooks", result.stderr)
        self.assertEqual(git(repo, "config", "core.hooksPath").strip(), ".husky")
        self.assertFalse((repo / ".claude").exists())

        result = self.install(repo, "--chain-hooks")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git(repo, "config", "core.hooksPath").strip(), ".githooks")
        self.assertEqual(git(repo, "config", "issuelooper.chainedHooksPath").strip(), ".husky")
        # install.sh's own commit ran .githooks, which ran the project's hooks after its own.
        self.assertTrue((self.base / "pre-commit.ran").exists())
        self.assertTrue((self.base / "commit-msg.ran").exists())
        self.assertIn(".githooks/commit-msg", git(repo, "ls-files"))
        # issuelooper's own check still refuses first, and then the project's hook does not run.
        (self.base / "pre-commit.ran").unlink()
        (repo / ".claude/hooks/x.txt").write_text("x\n")
        git(repo, "add", ".claude/hooks/x.txt")
        refused = subprocess.run(["git", "-C", str(repo), "commit", "-qm", "x"], capture_output=True, text=True,
                                 env={k: v for k, v in self.env.items() if k != "ISSUELOOPER_CONFIG_OK"})
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.base / "pre-commit.ran").exists())
        # pre-push hands the project's hook the same ref lines it read.
        refs = ("refs/heads/x 1111111111111111111111111111111111111111 "
                "refs/heads/x 0000000000000000000000000000000000000000\n")
        (repo / ".husky/pre-push").write_text(f'#!/bin/sh\ncat > "{self.base}/pre-push.stdin"\n')
        push = subprocess.run([str(repo / ".githooks/pre-push"), "origin", "url"], input=refs, cwd=repo,
                              capture_output=True, text=True)
        self.assertEqual(push.returncode, 0, push.stderr)
        self.assertEqual((self.base / "pre-push.stdin").read_text(), refs)

    def test_pre_push_refuses_an_empty_issue_branch(self):
        # A rebase stopped on a conflict leaves HEAD at origin/main; pushed to the issue
        # branch, GitHub closed its pull request for good (#100 and #120 in one run).
        repo = self.repo()
        main = git(repo, "rev-parse", "HEAD").strip()
        git(repo, "update-ref", "refs/remotes/origin/main", main)
        zero = "0" * 40

        def push(sha):
            refs = f"refs/heads/issue-4 {sha} refs/heads/issue-4 {zero}\n"
            return subprocess.run([str(ROOT / "template/.githooks/pre-push"), "origin", "url"], input=refs,
                                  cwd=repo, capture_output=True, text=True)

        refused = push(main)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("no commits beyond origin/main", refused.stderr)
        (repo / "a.py").write_text("x = 1\n")
        git(repo, "add", "a.py")
        git(repo, "commit", "-qm", "fix")
        head = git(repo, "rev-parse", "HEAD").strip()
        self.assertEqual(push(head).returncode, 0, push(head).stderr)
        (repo / ".git/rebase-merge").mkdir()
        refused = push(head)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("rebase or merge is in progress", refused.stderr)

    def test_hooks_in_git_hooks_are_kept_running(self):
        repo = self.repo()
        hooks = pathlib.Path(git(repo, "rev-parse", "--git-path", "hooks").strip())
        hooks = hooks if hooks.is_absolute() else repo / hooks
        (hooks / "pre-commit").write_text(f'#!/bin/sh\necho ran >> "{self.base}/pre-commit.ran"\n')
        (hooks / "pre-commit").chmod(0o755)
        result = self.install(repo)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pre-commit", result.stderr)
        self.assertEqual(git(repo, "config", "--get", "core.hooksPath", check=False).strip(), "")
        result = self.install(repo, "--chain-hooks")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.base / "pre-commit.ran").exists())

    def test_keep_hooks_leaves_the_hooks_path_alone(self):
        repo = self.repo()
        self.hooks_dir(repo, ".husky")
        git(repo, "config", "core.hooksPath", ".husky")
        result = self.install(repo, "--keep-hooks", "--no-commit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git(repo, "config", "core.hooksPath").strip(), ".husky")
        self.assertIn(".githooks/pre-commit and .githooks/pre-push", result.stdout)
        # A session's start (ccrole.sh) must not take core.hooksPath back.
        subprocess.run(["bash", str(repo / ".claude/hooks/ccrole.sh"), "assign", "x"], cwd=repo,
                       capture_output=True, text=True, env=self.env)
        self.assertEqual(git(repo, "config", "core.hooksPath").strip(), ".husky")

    def test_sample_hooks_alone_need_no_option(self):
        repo = self.repo()  # git init leaves only *.sample hooks
        result = self.install(repo, "--no-commit")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git(repo, "config", "core.hooksPath").strip(), ".githooks")


if __name__ == "__main__":
    unittest.main()
