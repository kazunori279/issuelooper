#!/usr/bin/env python3
"""The installer accepts symlinked checkout paths, including macOS /tmp."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]


class InstallTest(unittest.TestCase):
    def test_install_through_symlink(self):
        with tempfile.TemporaryDirectory(prefix="install-test-") as d:
            base = pathlib.Path(d)
            repo = base / "repo"
            repo.mkdir()
            alias = base / "alias"
            alias.symlink_to(repo, target_is_directory=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            result = subprocess.run(
                [str(ROOT / "install.sh"), str(alias), "--no-labels", "--no-commit"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((repo / ".claude/hooks/role_guard.py").is_file())
            self.assertTrue((repo / ".agents/skills/issuelooper-worker/SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
