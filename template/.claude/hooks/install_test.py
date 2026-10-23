#!/usr/bin/env python3
"""The installer: symlinked checkout paths (macOS /tmp), staging only what it writes,
and reporting conflicts before it overwrites."""
import os
import pathlib
import subprocess
import tempfile
import unittest

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
                         ".agents/skills/issuelooper-worker", ".githooks/pre-commit",
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


if __name__ == "__main__":
    unittest.main()
