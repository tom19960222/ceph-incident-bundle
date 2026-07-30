from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from validation.lab_profile import (
    SCHEMA_VERSION,
    LabProfileError,
    load_profile,
    parse_profile,
    safe_display_path,
)


ROOT = Path(__file__).resolve().parents[1]
ACTIVE_EXAMPLE = ROOT / "validation" / "lab-profile.example.toml"
BOOTSTRAP_EXAMPLE = ROOT / "validation" / "lab-bootstrap.example.toml"
FINGERPRINT = "SHA256:/h4FqMEWPRJfZPw6PJz4b5i2//0AtbRLctMFudlKKWU"
OTHER_FINGERPRINT = "SHA256:mctW2oBnIHUeSwsmqclzdaOPdE3amEImn9Q8CuzWih4"


def active_profile_text(**overrides: str) -> str:
    """Render a complete active profile so each test can spoil one field."""

    fields = {
        "schema_version": "schema_version = 1\n",
        "profile": '[profile]\nname = "lab"\nstate = "active"\n',
        "ssh": '[ssh]\nuser = "operator"\nkey_path = "/keys/id_ed25519"\n',
        "ceph": '[ceph]\nseed = "monitor01"\nfsid = "3f2b1c8e-0000-4a1d-8b7e-000000000001"\n',
        "rook": (
            "[rook]\n"
            'kubeconfig_path = "/keys/kubeconfig"\n'
            'namespace = "rook-ceph"\n'
            'fsid = "3f2b1c8e-0000-4a1d-8b7e-000000000002"\n'
        ),
        "prometheus": '[prometheus]\nurl = "http://10.0.0.11:9095"\n',
        "hosts": (
            "[[hosts]]\n"
            'name = "monitor01"\n'
            'address = "10.0.0.11"\n'
            'hostname = "monitor01"\n'
            f'ssh_fingerprints = ["{FINGERPRINT}"]\n'
            "\n"
            "[[hosts]]\n"
            'name = "osd01"\n'
            'address = "10.0.0.21"\n'
            'hostname = "osd01"\n'
            f'ssh_fingerprints = ["{OTHER_FINGERPRINT}"]\n'
        ),
    }
    fields.update(overrides)
    return "\n".join(value for value in fields.values() if value)


class ProfileShapeTests(unittest.TestCase):
    def assert_rejected(self, text: str, expected_message: str) -> LabProfileError:
        with self.assertRaises(LabProfileError) as raised:
            parse_profile(text, Path("/labs/lab.toml"))
        self.assertIn(expected_message, str(raised.exception))
        return raised.exception

    def test_accepts_a_complete_active_profile(self) -> None:
        profile = parse_profile(active_profile_text(), Path("/labs/lab.toml"))
        self.assertEqual(profile.name, "lab")
        self.assertEqual(profile.state, "active")
        self.assertEqual(profile.ssh_user, "operator")
        self.assertEqual(profile.ssh_key_path, Path("/keys/id_ed25519"))
        self.assertEqual(profile.ceph_seed, "monitor01")
        self.assertEqual(profile.rook_namespace, "rook-ceph")
        self.assertEqual(profile.rook_operator_namespace, "rook-ceph")
        self.assertEqual(profile.prometheus_url, "http://10.0.0.11:9095")
        self.assertEqual([host.name for host in profile.hosts], ["monitor01", "osd01"])
        self.assertEqual(profile.hosts[0].ssh_fingerprints, (FINGERPRINT,))
        self.assertTrue(profile.identity_complete)
        self.assertEqual(profile.missing_identity(), ())

    def test_reports_the_seed_host_and_ssh_targets(self) -> None:
        profile = parse_profile(active_profile_text(), Path("/labs/lab.toml"))
        self.assertEqual(profile.seed_host.name, "monitor01")
        self.assertEqual(profile.ssh_target(profile.seed_host), "operator@10.0.0.11")
        self.assertEqual(profile.host("osd01").address, "10.0.0.21")
        with self.assertRaises(KeyError):
            profile.host("absent")

    def test_rejects_an_unsupported_schema_version(self) -> None:
        self.assert_rejected(
            active_profile_text(schema_version="schema_version = 2\n"),
            "schema_version must be 1",
        )
        self.assert_rejected(
            active_profile_text(schema_version=""), "missing schema_version"
        )
        self.assert_rejected(
            active_profile_text(schema_version='schema_version = "1"\n'),
            "schema_version must be 1",
        )

    def test_rejects_unknown_tables_and_keys(self) -> None:
        self.assert_rejected(
            active_profile_text() + '\n[extra]\nvalue = "x"\n',
            "unknown profile table: extra",
        )
        self.assert_rejected(
            active_profile_text(schema_version='schema_version = 1\nunexpected = "x"\n'),
            "unknown profile key: unexpected",
        )
        self.assert_rejected(
            active_profile_text(
                ssh='[ssh]\nuser = "operator"\nkey_path = "/keys/k"\npassword = "hunter2"\n'
            ),
            "unknown key in [ssh]: password",
        )
        self.assert_rejected(
            active_profile_text(
                hosts=(
                    "[[hosts]]\n"
                    'name = "monitor01"\n'
                    'address = "10.0.0.11"\n'
                    'hostname = "monitor01"\n'
                    f'ssh_fingerprints = ["{FINGERPRINT}"]\n'
                    'keyring = "AQBmust-not-be-here"\n'
                )
            ),
            "unknown key in [[hosts]]: keyring",
        )

    def test_rejects_a_missing_required_table(self) -> None:
        for table, message in (
            ("profile", "missing [profile]"),
            ("ssh", "missing [ssh]"),
            ("ceph", "missing [ceph]"),
            ("rook", "missing [rook]"),
            ("prometheus", "missing [prometheus]"),
            ("hosts", "profile lists no hosts"),
        ):
            with self.subTest(table=table):
                self.assert_rejected(active_profile_text(**{table: ""}), message)

    def test_rejects_an_unknown_profile_state(self) -> None:
        self.assert_rejected(
            active_profile_text(profile='[profile]\nname = "lab"\nstate = "trusted"\n'),
            "profile state must be one of bootstrap, candidate, active",
        )

    def test_rejects_unsafe_scalars(self) -> None:
        cases = (
            ('[profile]\nname = "lab lab"\nstate = "active"\n', "invalid profile name"),
            ('[ssh]\nuser = "-oProxyCommand=x"\nkey_path = "/keys/k"\n', "invalid ssh user"),
            ('[ssh]\nuser = "operator"\nkey_path = "keys/k"\n', "ssh key_path must be absolute"),
        )
        for text, message in cases:
            with self.subTest(message=message):
                table = "profile" if text.startswith("[profile]") else "ssh"
                self.assert_rejected(active_profile_text(**{table: text}), message)

    def test_rejects_an_unsafe_prometheus_url(self) -> None:
        for url, message in (
            ("ftp://10.0.0.11:9095", "invalid prometheus url"),
            ("-http://10.0.0.11", "invalid prometheus url"),
            ("http://10.0.0.11 /x", "invalid prometheus url"),
        ):
            with self.subTest(url=url):
                self.assert_rejected(
                    active_profile_text(prometheus=f'[prometheus]\nurl = "{url}"\n'),
                    message,
                )

    def test_normalises_a_trailing_slash_in_the_prometheus_url(self) -> None:
        profile = parse_profile(
            active_profile_text(prometheus='[prometheus]\nurl = "http://10.0.0.11:9095/"\n'),
            Path("/labs/lab.toml"),
        )
        self.assertEqual(profile.prometheus_url, "http://10.0.0.11:9095")

    def test_rejects_an_invalid_host_map(self) -> None:
        cases = (
            (
                "[[hosts]]\nname = \"../escape\"\naddress = \"10.0.0.11\"\n",
                "invalid host name",
            ),
            (
                "[[hosts]]\nname = \"monitor01\"\naddress = \"-oProxyCommand=x\"\n",
                "invalid host address",
            ),
            (
                "[[hosts]]\nname = \"monitor01\"\naddress = \"10.0.0.11\"\n\n"
                "[[hosts]]\nname = \"MONITOR01\"\naddress = \"10.0.0.12\"\n",
                "duplicate host name",
            ),
            (
                "[[hosts]]\nname = \"monitor01\"\naddress = \"10.0.0.11\"\n\n"
                "[[hosts]]\nname = \"mon02\"\naddress = \"10.0.0.11\"\n",
                "duplicate host address",
            ),
            (
                "[[hosts]]\nname = \"monitor01\"\naddress = \"10.0.0.11\"\n"
                'ssh_fingerprints = ["MD5:aa:bb"]\n',
                "invalid ssh fingerprint",
            ),
            (
                "[[hosts]]\nname = \"monitor01\"\naddress = \"10.0.0.11\"\n"
                f'ssh_fingerprints = ["{FINGERPRINT}", "{FINGERPRINT}"]\n',
                "duplicate ssh fingerprint",
            ),
        )
        for text, message in cases:
            with self.subTest(message=message):
                self.assert_rejected(
                    active_profile_text(ceph='[ceph]\nseed = "monitor01"\n', hosts=text),
                    message,
                )

    def test_rejects_a_seed_that_is_not_in_the_host_map(self) -> None:
        self.assert_rejected(
            active_profile_text(ceph='[ceph]\nseed = "absent"\n'),
            "ceph seed is not in the host map: absent",
        )

    def test_rejects_an_invalid_fsid(self) -> None:
        self.assert_rejected(
            active_profile_text(ceph='[ceph]\nseed = "monitor01"\nfsid = "not-a-uuid"\n'),
            "invalid ceph fsid",
        )

    def test_normalises_fsid_case(self) -> None:
        profile = parse_profile(
            active_profile_text(
                ceph='[ceph]\nseed = "monitor01"\nfsid = "3F2B1C8E-0000-4A1D-8B7E-000000000001"\n'
            ),
            Path("/labs/lab.toml"),
        )
        self.assertEqual(profile.ceph_fsid, "3f2b1c8e-0000-4a1d-8b7e-000000000001")

    def test_rejects_an_invalid_rook_namespace(self) -> None:
        for namespaces, message in (
            ('namespace = ""\n', "invalid rook namespace"),
            ('namespace = "-rook"\n', "invalid rook namespace"),
            ('namespace = ["rook-ceph"]\n', "rook namespace must be a string"),
            ("", "missing namespace in [rook]"),
            (
                'namespace = "rook-ceph"\noperator_namespace = "bad ns"\n',
                "invalid rook operator_namespace",
            ),
        ):
            with self.subTest(namespaces=namespaces):
                self.assert_rejected(
                    active_profile_text(
                        rook=(
                            "[rook]\n"
                            'kubeconfig_path = "/keys/kubeconfig"\n' + namespaces
                        )
                    ),
                    message,
                )

    def test_the_operator_namespace_defaults_to_the_cluster_namespace(self) -> None:
        profile = parse_profile(
            active_profile_text(
                rook=(
                    "[rook]\n"
                    'kubeconfig_path = "/keys/kubeconfig"\n'
                    'namespace = "rook-a"\n'
                    'fsid = "3f2b1c8e-0000-4a1d-8b7e-000000000002"\n'
                )
            ),
            Path("/labs/lab.toml"),
        )
        self.assertEqual(profile.rook_namespace, "rook-a")
        self.assertEqual(profile.rook_operator_namespace, "rook-a")

    def test_rejects_credential_material_hidden_in_a_comment(self) -> None:
        self.assert_rejected(
            active_profile_text(
                profile='[profile]\nname = "lab"\nstate = "active"\n'
                "# -----BEGIN OPENSSH PRIVATE KEY-----\n"
            ),
            "profile carries credential material",
        )

    def test_rejects_a_string_value_carrying_credential_material(self) -> None:
        self.assert_rejected(
            active_profile_text(
                profile='[profile]\nname = "lab"\nstate = "active"\n',
                ssh='[ssh]\nuser = "operator"\n'
                'key_path = "/keys/-----BEGIN OPENSSH PRIVATE KEY-----"\n',
            ),
            "profile carries credential material",
        )

    def test_rejects_an_over_long_string_value(self) -> None:
        self.assert_rejected(
            active_profile_text(
                ssh='[ssh]\nuser = "operator"\nkey_path = "/keys/' + "k" * 600 + '"\n'
            ),
            "profile value is too long",
        )


class IdentityCompletenessTests(unittest.TestCase):
    def test_a_bootstrap_profile_may_omit_lab_identity(self) -> None:
        profile = load_profile(BOOTSTRAP_EXAMPLE)
        self.assertEqual(profile.state, "bootstrap")
        self.assertIsNone(profile.ceph_fsid)
        self.assertIsNone(profile.rook_fsid)
        self.assertFalse(profile.identity_complete)
        self.assertEqual(
            profile.missing_identity(),
            (
                "ceph fsid",
                "rook fsid",
                "hostname for monitor01",
                "ssh fingerprints for monitor01",
                "hostname for mon02",
                "ssh fingerprints for mon02",
                "hostname for osd01",
                "ssh fingerprints for osd01",
            ),
        )

    def test_an_active_profile_must_carry_complete_identity(self) -> None:
        with self.assertRaises(LabProfileError) as raised:
            parse_profile(
                active_profile_text(ceph='[ceph]\nseed = "monitor01"\n'),
                Path("/labs/lab.toml"),
            )
        self.assertIn("active profile is missing lab identity", str(raised.exception))
        self.assertIn("ceph fsid", str(raised.exception))

    def test_a_candidate_profile_must_also_carry_complete_identity(self) -> None:
        with self.assertRaises(LabProfileError):
            parse_profile(
                active_profile_text(
                    profile='[profile]\nname = "lab"\nstate = "candidate"\n',
                    ceph='[ceph]\nseed = "monitor01"\n',
                ),
                Path("/labs/lab.toml"),
            )


class ExampleProfileTests(unittest.TestCase):
    def test_the_committed_examples_load(self) -> None:
        active = load_profile(ACTIVE_EXAMPLE)
        self.assertEqual(active.state, "active")
        self.assertTrue(active.identity_complete)
        bootstrap = load_profile(BOOTSTRAP_EXAMPLE)
        self.assertEqual(bootstrap.state, "bootstrap")

    def test_the_committed_examples_carry_no_real_endpoints(self) -> None:
        for example in (ACTIVE_EXAMPLE, BOOTSTRAP_EXAMPLE):
            with self.subTest(example=example.name):
                text = example.read_text(encoding="utf-8")
                self.assertNotIn("192.168.", text)
                self.assertNotIn("-----BEGIN", text)
                self.assertIn("198.51.100.", text)


class RenderingTests(unittest.TestCase):
    def test_rendering_round_trips_through_the_loader(self) -> None:
        original = load_profile(ACTIVE_EXAMPLE)
        reparsed = parse_profile(original.render(), original.path)
        self.assertEqual(reparsed.hosts, original.hosts)
        self.assertEqual(reparsed.ceph_fsid, original.ceph_fsid)
        self.assertEqual(reparsed.rook_fsid, original.rook_fsid)
        self.assertEqual(reparsed.profile_hash, original.profile_hash)

    def test_the_profile_hash_covers_content_not_formatting(self) -> None:
        spaced = active_profile_text().replace("\n\n", "\n\n\n")
        commented = "# a comment\n" + active_profile_text()
        first = parse_profile(spaced, Path("/labs/lab.toml"))
        second = parse_profile(commented, Path("/labs/other.toml"))
        self.assertEqual(first.profile_hash, second.profile_hash)
        self.assertTrue(first.profile_hash.startswith("sha256:"))

    def test_the_profile_hash_changes_with_identity(self) -> None:
        first = parse_profile(active_profile_text(), Path("/labs/lab.toml"))
        second = parse_profile(
            active_profile_text(
                ceph='[ceph]\nseed = "monitor01"\nfsid = "3f2b1c8e-0000-4a1d-8b7e-00000000ffff"\n'
            ),
            Path("/labs/lab.toml"),
        )
        self.assertNotEqual(first.profile_hash, second.profile_hash)

    def test_escapes_are_not_reachable_because_values_are_validated(self) -> None:
        profile = parse_profile(active_profile_text(), Path("/labs/lab.toml"))
        self.assertNotIn("\\", profile.render())


class LoaderTests(unittest.TestCase):
    def test_reports_a_missing_profile(self) -> None:
        with self.assertRaises(LabProfileError) as raised:
            load_profile(Path("/nonexistent/lab.toml"))
        self.assertIn("missing lab profile", str(raised.exception))

    def test_requires_an_absolute_profile_path(self) -> None:
        with self.assertRaises(LabProfileError) as raised:
            load_profile(Path("lab.toml"))
        self.assertIn("lab profile path must be absolute", str(raised.exception))

    def test_reports_unparseable_toml(self) -> None:
        with self.assertRaises(LabProfileError) as raised:
            parse_profile("this is not toml", Path("/labs/lab.toml"))
        self.assertIn("cannot parse lab profile", str(raised.exception))

    def test_rejects_an_oversized_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lab.toml"
            path.write_text("# " + "x" * (1024 * 1024), encoding="utf-8")
            with self.assertRaises(LabProfileError) as raised:
                load_profile(path)
            self.assertIn("lab profile is too large", str(raised.exception))

    def test_schema_version_constant_matches_the_examples(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 1)
        self.assertIn(
            f"schema_version = {SCHEMA_VERSION}",
            ACTIVE_EXAMPLE.read_text(encoding="utf-8"),
        )


class SafeDisplayTests(unittest.TestCase):
    def test_collapses_the_home_directory(self) -> None:
        self.assertEqual(
            safe_display_path(Path.home() / "labs" / "lab.toml"),
            str(Path("~") / "labs" / "lab.toml"),
        )

    def test_keeps_other_paths_verbatim(self) -> None:
        self.assertEqual(safe_display_path(Path("/srv/labs/lab.toml")), "/srv/labs/lab.toml")


class DerivationTests(unittest.TestCase):
    def test_with_identity_produces_a_candidate_without_touching_the_source(self) -> None:
        bootstrap = load_profile(BOOTSTRAP_EXAMPLE)
        candidate = bootstrap.with_identity(
            state="candidate",
            ceph_fsid="3f2b1c8e-0000-4a1d-8b7e-000000000001",
            rook_fsid="3f2b1c8e-0000-4a1d-8b7e-000000000002",
            host_identity={
                "monitor01": ("monitor01", (FINGERPRINT,)),
                "mon02": ("mon02", (OTHER_FINGERPRINT,)),
                "osd01": ("osd01", (FINGERPRINT,)),
            },
        )
        self.assertEqual(candidate.state, "candidate")
        self.assertTrue(candidate.identity_complete)
        self.assertEqual(candidate.hosts[0].hostname, "monitor01")
        self.assertEqual(bootstrap.state, "bootstrap")
        self.assertIsNone(bootstrap.ceph_fsid)
        reparsed = parse_profile(candidate.render(), candidate.path)
        self.assertEqual(reparsed.profile_hash, candidate.profile_hash)

    def test_with_state_only_changes_the_state(self) -> None:
        candidate = parse_profile(
            active_profile_text(profile='[profile]\nname = "lab"\nstate = "candidate"\n'),
            Path("/labs/lab.candidate.toml"),
        )
        activated = candidate.with_state("active", path=Path("/labs/lab.toml"))
        self.assertEqual(activated.state, "active")
        self.assertEqual(activated.path, Path("/labs/lab.toml"))
        self.assertEqual(activated.hosts, candidate.hosts)

    def test_an_incomplete_candidate_cannot_be_derived(self) -> None:
        bootstrap = load_profile(BOOTSTRAP_EXAMPLE)
        with self.assertRaises(LabProfileError) as raised:
            bootstrap.with_state("candidate", path=Path("/labs/lab.candidate.toml"))
        self.assertIn("missing lab identity", str(raised.exception))


class DocumentationTests(unittest.TestCase):
    def test_the_example_documents_the_local_only_and_credential_rules(self) -> None:
        text = ACTIVE_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn("local-only", text)
        self.assertIn("credential", text)
        self.assertIn("candidate", text)


if __name__ == "__main__":
    unittest.main()
