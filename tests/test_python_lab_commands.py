from __future__ import annotations

import re
import unittest
from pathlib import Path

from validation import lab_commands
from validation.lab_commands import (
    ACTIVATION_CONFIRMATION_VARIABLE,
    PREFLIGHT_CONFIRMATION_VARIABLE,
    QUALIFICATION_TARGET,
    activate_command,
    discover_command,
    preflight_command,
    qualify_command,
    status_command,
)


ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
PROFILE = Path("/labs/lab.toml")
CANDIDATE = Path("/labs/lab.candidate.toml")


def make_targets() -> set[str]:
    """Every target the Makefile actually defines."""

    text = MAKEFILE.read_text(encoding="utf-8")
    return set(re.findall(r"^([a-z][a-z0-9-]*):", text, re.MULTILINE))


class RunnableActionTests(unittest.TestCase):
    """A next action is only useful if it can be pasted into a shell."""

    def test_every_action_names_a_target_the_makefile_defines(self) -> None:
        defined = make_targets()
        for command in (
            status_command(PROFILE),
            discover_command(PROFILE),
            discover_command(PROFILE, replace_candidate=True),
            activate_command(PROFILE, CANDIDATE),
            activate_command(PROFILE, CANDIDATE, replace_active=True),
            preflight_command(PROFILE),
        ):
            with self.subTest(command=command):
                self.assertTrue(command.startswith("make "))
                target = command.split()[1]
                self.assertIn(target, defined)
                self.assertIn(f"LAB_PROFILE={PROFILE}", command)

    def test_the_qualification_gate_is_a_real_confirmed_target(self) -> None:
        # The gate is only useful as a next action if it exists and carries its
        # explicit confirmation: it runs two real collects against the lab.
        self.assertIn(QUALIFICATION_TARGET, make_targets())
        command = qualify_command(PROFILE)
        self.assertIn(f"make {QUALIFICATION_TARGET}", command)
        self.assertIn(f"LAB_PROFILE={PROFILE}", command)
        self.assertIn(f"{PREFLIGHT_CONFIRMATION_VARIABLE}=1", command)

    def test_ordinary_validation_can_never_reach_a_lab(self) -> None:
        # `make validate` must stay offline: no lab target may become one of its
        # prerequisites, directly or through another target it depends on.
        text = MAKEFILE.read_text(encoding="utf-8")
        prerequisites: dict[str, set[str]] = {
            rule.group(1): set(rule.group(2).split())
            for rule in re.finditer(r"^([a-z][a-z0-9-]*):(.*)$", text, re.MULTILINE)
        }
        reachable: set[str] = set()
        pending = list(prerequisites.get("validate", ()))
        while pending:
            target = pending.pop()
            if target in reachable:
                continue
            reachable.add(target)
            pending += list(prerequisites.get(target, ()))
        for target in ("lab-status", "lab-profile-discover", "lab-profile-activate",
                       "lab-preflight", QUALIFICATION_TARGET):
            self.assertNotIn(target, reachable)

    def test_the_confirmation_variables_are_the_ones_the_makefile_documents(self) -> None:
        text = MAKEFILE.read_text(encoding="utf-8")
        activate = activate_command(PROFILE, CANDIDATE)
        self.assertIn(f"{ACTIVATION_CONFIRMATION_VARIABLE}=1", activate)
        self.assertIn(f"{PREFLIGHT_CONFIRMATION_VARIABLE}=1", preflight_command(PROFILE))
        self.assertIn("LAB_CANDIDATE", text)
        self.assertIn("LAB_ARGS", text)

    def test_the_extra_opt_ins_are_passed_the_way_the_makefile_forwards_them(self) -> None:
        self.assertIn(
            "LAB_ARGS=--replace-candidate", discover_command(PROFILE, replace_candidate=True)
        )
        self.assertIn(
            "LAB_ARGS=--replace-active",
            activate_command(PROFILE, CANDIDATE, replace_active=True),
        )
        self.assertNotIn("LAB_ARGS", discover_command(PROFILE))
        self.assertNotIn("LAB_ARGS", activate_command(PROFILE, CANDIDATE))

    def test_actions_quote_real_paths_so_they_stay_runnable(self) -> None:
        # `safe_display_path`'s `~` form would make a pasted command wrong.
        home_profile = Path.home() / "labs" / "lab.toml"
        self.assertIn(str(home_profile), status_command(home_profile))
        self.assertNotIn("~", status_command(home_profile))

    def test_an_activation_target_can_be_named_as_a_placeholder(self) -> None:
        # Preflight and status reach this path when the profile they were handed is
        # itself a candidate, so the active path is not known yet.
        command = activate_command("<active profile>", CANDIDATE)
        self.assertIn("LAB_PROFILE=<active profile>", command)
        self.assertIn(f"LAB_CANDIDATE={CANDIDATE}", command)


class VocabularyTests(unittest.TestCase):
    def test_no_module_spells_a_target_or_opt_in_out_by_hand(self) -> None:
        """The whole point of this module is that nothing else hardcodes these."""

        literals = (
            "make lab-status",
            "make lab-profile-discover",
            "make lab-profile-activate",
            "make lab-preflight",
            ACTIVATION_CONFIRMATION_VARIABLE,
            PREFLIGHT_CONFIRMATION_VARIABLE,
        )
        for module in sorted((ROOT / "validation").glob("*.py")):
            if module.name == "lab_commands.py":
                continue
            text = module.read_text(encoding="utf-8")
            for literal in literals:
                with self.subTest(module=module.name, literal=literal):
                    # A docstring may name a command; code may not build one.
                    body = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
                    self.assertNotIn(literal, body)

    def test_every_public_builder_is_covered_here(self) -> None:
        builders = {
            name
            for name in dir(lab_commands)
            if name.endswith("_command") and not name.startswith("_")
        }
        self.assertEqual(
            builders,
            {
                "status_command",
                "discover_command",
                "activate_command",
                "preflight_command",
                "qualify_command",
            },
        )


if __name__ == "__main__":
    unittest.main()
