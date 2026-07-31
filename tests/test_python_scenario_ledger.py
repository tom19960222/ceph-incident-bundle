"""The scenario ledger has to stay true, not merely present.

`docs/test-scenario-ledger.md` claims, for every scenario in
`docs/test-scenario-inventory.md`, either a Python test that covers it, a
documented reason for not porting it, or a blocked entry with an issue.  This
test is what makes that claim checkable: a renamed test, a new inventory row, a
quietly relaxed `not-ported` classification or an undocumented blocked entry all
fail here.

What no mechanical check can decide is whether a referenced test asserts every
clause of the row it is pointed at.  That is a reading, done by hand, and
recorded in `docs/test-scenario-audit.md`.  What *is* mechanical is knowing when
that reading went stale: each audit entry pins a fingerprint of both sides of
the reading — the inventory row that states the clauses, and the ledger's
coverage cell that answers them.  Rewording a scenario, changing its fixture
technique, reclassifying it, or repointing it at a different test all fail here
until someone re-reads the row.
"""

from __future__ import annotations

import hashlib
import importlib
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs" / "test-scenario-inventory.md"
LEDGER = ROOT / "docs" / "test-scenario-ledger.md"
AUDIT = ROOT / "docs" / "test-scenario-audit.md"

SCENARIO_ID = re.compile(r"^\|\s*([A-Z]{1,2}[0-9]+[a-z]?)\s*\|(.*)$")
STATUSES = ("ported", "blocked", "not-ported")
TEST_REFERENCE = re.compile(r"`(test_python_[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2})`")
OVERVIEW_ROW = re.compile(r"^\|\s*(?:\*\*)?([a-z-]+|合計)(?:\*\*)?\s*\|\s*(?:\*\*)?(\d+)")
AUDIT_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
# The scenario's meaning and how it is faked, but not the shell line numbers:
# unrelated edits to a shell test file move those without changing what the row
# claims, and a stale-audit signal nobody believes is worse than none.
AUDITED_COLUMNS = (0, 1, 3)


def inventory_scenarios() -> dict[str, bool]:
    """Every inventory scenario id, mapped to "is a shell implementation detail"."""

    scenarios: dict[str, bool] = {}
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        match = SCENARIO_ID.match(line)
        if match is not None:
            scenarios[match.group(1)] = "不移植" in line
    return scenarios


def audited_fingerprints() -> dict[str, str]:
    """Every scenario id, mapped to the fingerprint of what was audited.

    Both sides of the reading are covered: the inventory row states the clauses,
    the ledger's status and coverage cell answer them.  A change to either is a
    reason to read the row again.
    """

    fingerprints: dict[str, str] = {}
    coverage = {
        identifier: f"{status}\x1f{detail}"
        for identifier, (status, detail, _) in ledger_rows().items()
    }
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        match = SCENARIO_ID.match(line)
        if match is None:
            continue
        cells = [cell.strip() for cell in match.group(2).split("|")]
        if len(cells) <= max(AUDITED_COLUMNS):
            continue
        identifier = match.group(1)
        audited = "\x1f".join(
            [
                identifier,
                *(cells[column] for column in AUDITED_COLUMNS),
                coverage.get(identifier, "<no ledger row>"),
            ]
        )
        fingerprints[identifier] = hashlib.sha256(
            audited.encode("utf-8")
        ).hexdigest()[:16]
    return fingerprints


def audit_records() -> dict[str, tuple[str, str, str]]:
    """Every audit record: id -> (audit date, fingerprint, finding)."""

    records: dict[str, tuple[str, str, str]] = {}
    for line in AUDIT.read_text(encoding="utf-8").splitlines():
        match = SCENARIO_ID.match(line)
        if match is None:
            continue
        cells = [cell.strip() for cell in match.group(2).split("|")]
        if len(cells) < 4 or AUDIT_DATE.match(cells[0]) is None:
            continue
        records[match.group(1)] = (cells[0], cells[1], cells[2])
    return records


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

    def test_every_scenario_has_a_clause_level_audit_record(self) -> None:
        self.assertEqual(set(audit_records()), set(self.inventory))

    def test_no_audit_record_outlived_the_row_it_was_made_against(self) -> None:
        """A reworded, reclassified or repointed row needs re-reading."""

        fingerprints = audited_fingerprints()
        self.assertEqual(set(fingerprints), set(self.inventory))
        for identifier, (audited_on, fingerprint, finding) in sorted(
            audit_records().items()
        ):
            with self.subTest(scenario=identifier):
                self.assertEqual(
                    fingerprint,
                    fingerprints[identifier],
                    f"{identifier} changed since it was audited on {audited_on}; "
                    f"re-read the row against its tests and update "
                    f"docs/test-scenario-audit.md",
                )
                self.assertTrue(finding, "an audit record needs its finding")

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
    if "--fingerprints" in sys.argv:
        # Regenerating the fingerprint column of docs/test-scenario-audit.md.
        for scenario_id, digest in audited_fingerprints().items():
            print(f"{scenario_id}\t{digest}")
    else:
        unittest.main()
