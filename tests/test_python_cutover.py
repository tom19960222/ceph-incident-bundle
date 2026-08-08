from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
