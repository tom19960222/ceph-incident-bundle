"""The scenario ledger has to stay true, not merely present.

`docs/test-scenario-ledger.md` claims, for every scenario in
`docs/test-scenario-inventory.md`, either a Python test that covers it, a
documented reason for not porting it, or a blocked entry with an issue.  This
test is what makes that claim checkable: a renamed test, a new inventory row, a
quietly relaxed `not-ported` classification or an undocumented blocked entry all
fail here.
"""

from __future__ import annotations

import importlib
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "test-scenario-inventory.md"
LEDGER = ROOT / "docs" / "test-scenario-ledger.md"

SCENARIO_ID = re.compile(r"^\|\s*([A-Z]{1,2}[0-9]+[a-z]?)\s*\|(.*)$")
STATUSES = ("ported", "blocked", "not-ported")
TEST_REFERENCE = re.compile(r"`(test_python_[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2})`")
OVERVIEW_ROW = re.compile(r"^\|\s*(?:\*\*)?([a-z-]+|合計)(?:\*\*)?\s*\|\s*(?:\*\*)?(\d+)")


def inventory_scenarios() -> dict[str, bool]:
    """Every inventory scenario id, mapped to "is a shell implementation detail"."""

    scenarios: dict[str, bool] = {}
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        match = SCENARIO_ID.match(line)
        if match is not None:
            scenarios[match.group(1)] = "不移植" in line
    return scenarios


def ledger_rows() -> dict[str, tuple[str, str, str]]:
    """Every ledger row: id -> (status, detail, differential scenario)."""

    rows: dict[str, tuple[str, str, str]] = {}
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        match = SCENARIO_ID.match(line)
        if match is None:
            continue
        cells = [cell.strip() for cell in match.group(2).split("|")]
        if len(cells) < 4 or cells[1] not in STATUSES:
            continue
        rows[match.group(1)] = (cells[1], cells[2], cells[3])
    return rows


class ScenarioLedgerTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.inventory = inventory_scenarios()
        self.rows = ledger_rows()

    def test_the_ledger_covers_every_inventory_scenario_exactly_once(self) -> None:
        self.assertTrue(self.inventory, "no inventory scenarios were parsed")
        self.assertEqual(set(self.rows), set(self.inventory))

    def test_only_the_documented_scenarios_are_left_unported(self) -> None:
        classified = {
            identifier
            for identifier, detail_is_implementation in self.inventory.items()
            if detail_is_implementation
        }
        declared = {
            identifier
            for identifier, (status, _, _) in self.rows.items()
            if status == "not-ported"
        }
        self.assertEqual(declared, classified)
        for identifier in declared:
            with self.subTest(scenario=identifier):
                self.assertTrue(
                    self.rows[identifier][1].strip(),
                    "a not-ported scenario needs its reason",
                )

    def test_every_ported_scenario_points_at_tests_that_exist(self) -> None:
        ported = {
            identifier: detail
            for identifier, (status, detail, _) in self.rows.items()
            if status == "ported"
        }
        self.assertTrue(ported)
        for identifier, detail in sorted(ported.items()):
            references = TEST_REFERENCE.findall(detail)
            with self.subTest(scenario=identifier):
                self.assertTrue(references, "a ported scenario needs a test reference")
                for reference in references:
                    self.assertIsNotNone(
                        self.resolve(reference), f"unresolved test: {reference}"
                    )

    def test_every_blocked_scenario_cites_an_issue(self) -> None:
        blocked = {
            identifier: detail
            for identifier, (status, detail, _) in self.rows.items()
            if status == "blocked"
        }
        for identifier, detail in sorted(blocked.items()):
            with self.subTest(scenario=identifier):
                self.assertRegex(detail, r"#\d+")
        if blocked:
            text = LEDGER.read_text(encoding="utf-8")
            self.assertIn("## Blocked", text)
            for identifier in blocked:
                with self.subTest(scenario=identifier):
                    self.assertIn(identifier, text.split("## Blocked", 1)[1])

    def test_the_overview_totals_match_the_rows(self) -> None:
        counted = {status: 0 for status in STATUSES}
        for status, _, _ in self.rows.values():
            counted[status] += 1
        declared: dict[str, int] = {}
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            match = OVERVIEW_ROW.match(line)
            if match is not None:
                declared[match.group(1)] = int(match.group(2))
        for status, count in counted.items():
            with self.subTest(status=status):
                self.assertEqual(declared.get(status), count)
        self.assertEqual(declared.get("合計"), len(self.rows))

    def test_named_differential_scenarios_exist(self) -> None:
        from tests.differential.scenarios import SCENARIOS

        names = {scenario.name for scenario in SCENARIOS}
        for identifier, (_, _, differential) in sorted(self.rows.items()):
            if differential in ("", "—"):
                continue
            with self.subTest(scenario=identifier):
                self.assertIn(differential, names)

    def resolve(self, reference: str) -> object | None:
        module_name, class_name, method_name = reference.rsplit(".", 2)
        try:
            module = importlib.import_module(f"tests.{module_name}")
        except ImportError:
            return None
        test_case = getattr(module, class_name, None)
        if test_case is None:
            return None
        return getattr(test_case, method_name, None)


if __name__ == "__main__":
    unittest.main()
