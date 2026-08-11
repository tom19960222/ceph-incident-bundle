from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_MODULES = ROOT / "tests" / "production-test-modules.txt"
OFFLINE_RUNNER = ROOT / "tests" / "run-offline-validation.sh"


def make_fake_python(
    path: Path,
    *,
    role: str,
    version: tuple[int, int, int, str, int],
) -> None:
    path.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import shutil\n"
        "import sys\n"
        "import types\n"
        f"role = {role!r}\n"
        f"fake_executable = {str(path)!r}\n"
        f"fake_version = {version!r}\n"
        "if len(sys.argv) >= 3 and sys.argv[1] == '-c':\n"
        "    sys.executable = fake_executable\n"
        "    sys.implementation = types.SimpleNamespace(name='cpython')\n"
        "    sys.version_info = fake_version\n"
        "    code = sys.argv[2]\n"
        "    sys.argv = ['-c', *sys.argv[3:]]\n"
        "    exec(code, {'__name__': '__main__'})\n"
        "    raise SystemExit(0)\n"
        "if os.environ.get('REQUIRE_PINNED_PYTHON3') == '1':\n"
        "    if os.path.realpath(shutil.which('python3') or '') != os.path.realpath(fake_executable):\n"
        "        raise SystemExit(91)\n"
        "with open(os.environ['OFFLINE_VALIDATION_INVOCATIONS'], 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps([role, *sys.argv[1:]]) + '\\n')\n"
        "raise SystemExit(int(os.environ.get(role.upper() + '_TEST_RC', '0')))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@unittest.skipUnless(shutil.which("make"), "make is required for repository gates")
class OfflineValidationEntrypointTests(unittest.TestCase):
    def run_validate(
        self,
        production: Path | None,
        tooling: Path | None,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        with tempfile.TemporaryDirectory() as temporary_directory:
            invocation_log = Path(temporary_directory) / "invocations.jsonl"
            env = {
                key: value
                for key, value in os.environ.items()
                if key not in {"PRODUCTION_PYTHON", "TOOLING_PYTHON", "PYTHON"}
            }
            env["OFFLINE_VALIDATION_INVOCATIONS"] = str(invocation_log)
            env.update(extra_env or {})
            arguments = ["bash", str(OFFLINE_RUNNER), "2"]
            if production is not None:
                env["PRODUCTION_PYTHON"] = str(production)
            if tooling is not None:
                env["TOOLING_PYTHON"] = str(tooling)
            result = subprocess.run(
                arguments,
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            invocations = (
                [json.loads(line) for line in invocation_log.read_text().splitlines()]
                if invocation_log.exists()
                else []
            )
        return result, invocations

    def test_make_validate_delegates_to_the_offline_runner(self) -> None:
        result = subprocess.run(
            [
                "make",
                "-n",
                "validate",
                "PRODUCTION_PYTHON=/isolated/production-python",
                "TOOLING_PYTHON=/isolated/tooling-python",
                "TEST_JOBS=3",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("run-offline-validation.sh", result.stdout)
        self.assertIn("PRODUCTION_PYTHON=", result.stdout)
        self.assertIn("TOOLING_PYTHON=", result.stdout)
        self.assertIn(" 3", result.stdout)

    def test_both_interpreters_are_required_before_any_test_gate(self) -> None:
        result, invocations = self.run_validate(None, None)

        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("PRODUCTION_PYTHON is required", output)
        self.assertIn("TOOLING_PYTHON is required", output)
        self.assertEqual(invocations, [])

    def test_interpreters_are_reported_before_two_distinguishable_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            production = directory / "production-python"
            tooling = directory / "tooling-python"
            make_fake_python(
                production, role="production", version=(3, 11, 7, "final", 0)
            )
            make_fake_python(tooling, role="tooling", version=(3, 12, 2, "final", 0))

            result, invocations = self.run_validate(production, tooling)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = result.stdout + result.stderr
        production_identity = output.index("production interpreter:")
        tooling_identity = output.index("tooling interpreter:")
        production_gate = output.index("production test gate:")
        complete_gate = output.index("complete suite gate:")
        self.assertLess(production_identity, production_gate)
        self.assertLess(tooling_identity, production_gate)
        self.assertLess(production_gate, complete_gate)
        self.assertIn(f"executable: {production}", output)
        self.assertIn("implementation: cpython", output)
        self.assertIn(
            "version: major=3 minor=11 micro=7 releaselevel=final serial=0", output
        )
        self.assertIn(f"executable: {tooling}", output)
        self.assertIn(
            "version: major=3 minor=12 micro=2 releaselevel=final serial=0", output
        )

        production_runs = [argv for role, *argv in invocations if role == "production"]
        tooling_runs = [argv for role, *argv in invocations if role == "tooling"]
        self.assertTrue(production_runs)
        self.assertTrue(tooling_runs)
        self.assertTrue(
            all("test_python_lab_" not in " ".join(argv) for argv in production_runs)
        )
        self.assertTrue(
            any("test_python_lab_" in " ".join(argv) for argv in tooling_runs)
        )

    def test_each_role_rejects_an_old_interpreter_before_tests_start(self) -> None:
        for old_role in ("production", "tooling"):
            with self.subTest(role=old_role), tempfile.TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                production = directory / "production-python"
                tooling = directory / "tooling-python"
                make_fake_python(
                    production,
                    role="production",
                    version=(3, 10, 14, "final", 0)
                    if old_role == "production"
                    else (3, 11, 9, "final", 0),
                )
                make_fake_python(
                    tooling,
                    role="tooling",
                    version=(3, 10, 14, "final", 0)
                    if old_role == "tooling"
                    else (3, 11, 9, "final", 0),
                )

                result, invocations = self.run_validate(production, tooling)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                f"{old_role} interpreter requires Python 3.11 or newer",
                result.stdout + result.stderr,
            )
            self.assertEqual(invocations, [])

    def test_each_role_reports_a_missing_selected_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            supported = directory / "supported-python"
            missing = directory / "missing-python"
            make_fake_python(
                supported, role="tooling", version=(3, 11, 9, "final", 0)
            )

            result, invocations = self.run_validate(missing, supported)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"production interpreter is missing or not executable: {missing}",
            result.stdout + result.stderr,
        )
        self.assertEqual(invocations, [])

    def test_both_gates_are_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            production = directory / "production-python"
            tooling = directory / "tooling-python"
            make_fake_python(
                production, role="production", version=(3, 11, 7, "final", 0)
            )
            make_fake_python(tooling, role="tooling", version=(3, 11, 7, "final", 0))

            result, invocations = self.run_validate(
                production,
                tooling,
                extra_env={"TOOLING_TEST_RC": "23"},
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(role == "production" for role, *_ in invocations))
        self.assertTrue(any(role == "tooling" for role, *_ in invocations))

    def test_each_gate_pins_python3_to_its_selected_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            production = directory / "production-python"
            tooling = directory / "tooling-python"
            make_fake_python(
                production, role="production", version=(3, 11, 7, "final", 0)
            )
            make_fake_python(tooling, role="tooling", version=(3, 12, 2, "final", 0))

            result, _ = self.run_validate(
                production,
                tooling,
                extra_env={
                    "REQUIRE_PINNED_PYTHON3": "1",
                    "TMPDIR": str(directory),
                },
            )
            runtime_shim_residue = list(
                directory.glob("ceph-incident-runtime-shims.*")
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(runtime_shim_residue, [])


class ProductionTestMembershipTests(unittest.TestCase):
    def test_every_non_tooling_module_is_in_the_production_gate(self) -> None:
        declared = {
            line
            for line in PRODUCTION_MODULES.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        }
        discovered = {
            path.name
            for path in (ROOT / "tests").glob("test_python_*.py")
            if not path.name.startswith("test_python_lab_")
        }

        self.assertEqual(declared, discovered)
        self.assertTrue(
            {
                "test_python_collect_ceph.py",
                "test_python_collect_cli.py",
                "test_python_collect_node.py",
                "test_python_collect_orchestration.py",
                "test_python_collect_prometheus.py",
                "test_python_collect_rook.py",
                "test_python_content_safety.py",
                "test_python_scenario_ledger.py",
                "test_python_verify.py",
            }.issubset(declared)
        )

    def test_the_complete_gate_discovers_every_test_module(self) -> None:
        all_tests = {
            path.name for path in (ROOT / "tests").glob("test_*.py") if path.is_file()
        }
        complete_gate = {
            path.name
            for path in (ROOT / "tests").glob("test_python_*.py")
            if path.is_file()
        }

        self.assertEqual(complete_gate, all_tests)


if __name__ == "__main__":
    unittest.main()
