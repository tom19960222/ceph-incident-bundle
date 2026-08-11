from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from validation.lab_qualify import default_entrypoints


ROOT = Path(__file__).resolve().parents[1]


class PythonOnlyCutoverTests(unittest.TestCase):
    def test_no_shell_implementation_or_shell_only_gate_remains(self) -> None:
        removed = [
            ROOT / "lib",
            ROOT / "run",
            ROOT / "tests" / "run-tests.sh",
            ROOT / "tests" / "export-shell-collect-fixture.sh",
            ROOT / "tests" / "fixtures" / "shell-collect-environment.sh",
            ROOT / "tests" / "differential",
        ]
        self.assertEqual([str(path.relative_to(ROOT)) for path in removed if path.exists()], [])
        self.assertEqual(list(ROOT.glob("tests/test-*.sh")), [])

        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        for retired_target in (
            "test-differential",
            "shellcheck",
            "tests/run-tests.sh",
        ):
            with self.subTest(retired_target=retired_target):
                self.assertNotIn(retired_target, makefile)

    def test_python_collect_and_verify_are_the_only_production_entrypoints(self) -> None:
        (entrypoint,) = default_entrypoints()
        self.assertEqual(entrypoint.implementation, "python")
        self.assertEqual(entrypoint.collect[-1], "collect")
        self.assertEqual(entrypoint.verify[-1], "verify")

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("bash run/collect.sh", readme)
        self.assertNotIn("bash lib/verify-bundle.sh", readme)


class RepositoryGateTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is required for repository gates")
    def test_local_connection_note_and_agent_worktrees_are_repo_ignored(self) -> None:
        paths = ".claude/worktrees/review/\nCEPH-LAB-CONNECTION.md\n"
        result = subprocess.run(
            ["git", "check-ignore", "-v", "--stdin"],
            cwd=ROOT,
            input=paths,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        ignored = result.stdout.splitlines()
        self.assertEqual(len(ignored), 2, result.stdout)
        self.assertTrue(
            all(line.startswith(".gitignore:") for line in ignored), result.stdout
        )
        self.assertTrue(ignored[0].endswith("\t.claude/worktrees/review/"))
        self.assertTrue(ignored[1].endswith("\tCEPH-LAB-CONNECTION.md"))

        tracked = subprocess.run(
            ["git", "ls-files", "--", ".claude/worktrees", "CEPH-LAB-CONNECTION.md"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(tracked.returncode, 0, tracked.stderr)
        self.assertEqual(tracked.stdout, "")

    @unittest.skipUnless(shutil.which("make"), "make is required for repository gates")
    def test_python_gate_rejects_an_interpreter_below_311(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_python = Path(temporary_directory) / "python-below-311"
            fake_python.write_text(
                f"#!{sys.executable}\n"
                "import os\n"
                "import sys\n"
                "if len(sys.argv) != 3 or sys.argv[1] != '-c':\n"
                "    raise SystemExit('expected Python -c invocation')\n"
                "real_python = os.environ['REAL_PYTHON']\n"
                "spoof = \"import sys; sys.version_info = (3, 10, 0, 'final', 0); \"\n"
                "os.execv(real_python, [real_python, '-c', spoof + sys.argv[2]])\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)

            result = subprocess.run(
                ["make", "check-python", f"PYTHON={fake_python}"],
                cwd=ROOT,
                env={**os.environ, "REAL_PYTHON": sys.executable},
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.11", result.stdout + result.stderr)

        supported = subprocess.run(
            ["make", "check-python", f"PYTHON={sys.executable}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(supported.returncode, 0, supported.stderr)

        test_python_dry_run = subprocess.run(
            ["make", "-n", "test-python", f"PYTHON={sys.executable}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(test_python_dry_run.returncode, 0, test_python_dry_run.stderr)
        self.assertIn("version_info < (3, 11)", test_python_dry_run.stdout)
        # The sharded runner spawns its own interpreters, so the gate only means
        # something if the checked interpreter is the one it is handed.
        self.assertIn("run-python-tests.sh", test_python_dry_run.stdout)
        self.assertIn(f'PYTHON="{sys.executable}"', test_python_dry_run.stdout)

        validate_dry_run = subprocess.run(
            [
                "make",
                "-n",
                "-j4",
                "validate",
                f"PRODUCTION_PYTHON={sys.executable}",
                f"TOOLING_PYTHON={sys.executable}",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(validate_dry_run.returncode, 0, validate_dry_run.stderr)
        self.assertIn("run-offline-validation.sh", validate_dry_run.stdout)
        self.assertIn(f'PRODUCTION_PYTHON="{sys.executable}"', validate_dry_run.stdout)
        self.assertIn(f'TOOLING_PYTHON="{sys.executable}"', validate_dry_run.stdout)


if __name__ == "__main__":
    unittest.main()
