"""The differential scenario set: the contract surfaces the gate must cover.

Each scenario is one fake world plus one CLI invocation, run twice — once by the
shell reference, once by the Python candidate — with identical knobs, inventory,
payload limits and options.  `coverage` names the behaviour inventory scenarios
the differential run exercises end to end, so the ledger in
`docs/test-scenario-ledger.md` and this file cannot drift apart silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tests.differential.environment import DEFAULT_INVENTORY


CEPH_NODE = "10.0.0.1"
KUBE_NODE = "10.0.0.9"
CEPH_SEED = f"ceph@{CEPH_NODE}"
BOTH_CAPABLE = f"{CEPH_NODE}=cephadm,ceph {KUBE_NODE}=kubectl"

# Shared for every scenario: the defaults the two entrypoints do not agree on
# (node timeout) are always passed explicitly, so a difference can only come
# from behaviour rather than from a default.
COMMON_ARGUMENTS = ("--timeout", "20", "--node-timeout", "120")


@dataclass(frozen=True)
class Scenario:
    name: str
    summary: str
    arguments: tuple[str, ...]
    knobs: dict[str, str] = field(default_factory=dict)
    inventory: str = DEFAULT_INVENTORY
    coverage: tuple[str, ...] = ()
    expected_exit: int | None = None
    expect_archive: bool = True
    interrupt_after: str | None = None

    @property
    def command_arguments(self) -> tuple[str, ...]:
        return (*COMMON_ARGUMENTS, *self.arguments)


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="cephadm-direct-no-prometheus",
        summary=(
            "explicit cephadm mode on a pinned seed with the direct runner, "
            "Prometheus disabled"
        ),
        arguments=("--mode", "cephadm", "--seed", CEPH_SEED),
        knobs={"FAKE_CAPS": BOTH_CAPABLE},
        coverage=("CA1", "CA4", "O11", "O25"),
        expected_exit=0,
    ),
    Scenario(
        name="cephadm-sudo-fallback",
        summary="direct runner unusable, so both fall back to non-interactive sudo",
        arguments=("--mode", "cephadm", "--seed", CEPH_SEED),
        knobs={
            "FAKE_CAPS": BOTH_CAPABLE,
            "FAKE_CEPH_DIRECT_FAIL_TARGETS": CEPH_NODE,
        },
        coverage=("CA5", "O26"),
        expected_exit=0,
    ),
    Scenario(
        name="cephadm-partial-command-failure",
        summary="one Ceph command fails: collection continues and the layer is partial",
        arguments=("--mode", "cephadm", "--seed", CEPH_SEED),
        knobs={"FAKE_CAPS": BOTH_CAPABLE, "FAKE_CEPH_FAIL_ON": "osd perf"},
        coverage=("CA2", "O16"),
        expected_exit=2,
    ),
    Scenario(
        name="rook-remote-kubectl",
        summary="explicit rook mode with kubectl running on a capable inventory node",
        arguments=("--mode", "rook", "--kube-mode", "remote", "--kube-context", "lab"),
        knobs={"FAKE_CAPS": BOTH_CAPABLE, "FAKE_KUBE_TOOLS_POD": "1"},
        coverage=("K7", "K9", "K6"),
        expected_exit=0,
    ),
    Scenario(
        name="rook-local-namespace-missing",
        summary="local kubectl whose Rook namespace is absent: classified skip, partial",
        arguments=("--mode", "rook", "--kube-mode", "local"),
        knobs={"FAKE_CAPS": BOTH_CAPABLE, "FAKE_KUBE_MODE": "missing-namespace"},
        coverage=("K3", "O27", "O19"),
        expected_exit=2,
    ),
    Scenario(
        name="auto-without-any-cluster-source",
        summary="auto mode with no capable node: both cluster layers skip, nodes still collected",
        arguments=("--mode", "auto", "--kube-mode", "remote"),
        knobs={"FAKE_CAPS": ""},
        coverage=("O10",),
        expected_exit=2,
    ),
    Scenario(
        name="prometheus-enabled",
        summary="Prometheus metrics dump enabled next to the Ceph layer",
        arguments=(
            "--mode",
            "cephadm",
            "--seed",
            CEPH_SEED,
            "--prom-url",
            "http://prom.example:9090",
            "--since",
            "24h",
        ),
        knobs={"FAKE_CAPS": BOTH_CAPABLE},
        coverage=("P5", "P12", "O32"),
        expected_exit=0,
    ),
    Scenario(
        name="node-partial-and-unusable-archive",
        summary=(
            "one node returns a valid archive with a nonzero collector, another "
            "returns an archive without a manifest"
        ),
        arguments=("--mode", "cephadm", "--seed", CEPH_SEED),
        knobs={
            "FAKE_CAPS": BOTH_CAPABLE,
            "FAKE_NODE_EXITS": "monitor01=2",
            "FAKE_NODE_CASES": "kubenode=missing-manifest",
        },
        coverage=("O14", "O16", "N12"),
        expected_exit=2,
    ),
    Scenario(
        name="node-collection-timeout",
        summary="a node that never answers is a Skipped Node, not a fatal collect",
        arguments=("--mode", "cephadm", "--seed", CEPH_SEED, "--node-timeout", "2"),
        knobs={
            "FAKE_CAPS": BOTH_CAPABLE,
            "FAKE_NODE_SLEEPS": "monitor01=30 kubenode=30",
        },
        coverage=("O21", "N11"),
        expected_exit=2,
    ),
    Scenario(
        name="mixed-full-collection-redacted",
        summary=(
            "one invocation over Ceph, Rook, Prometheus and every node with "
            "/var/log evidence, redaction on (default)"
        ),
        arguments=(
            "--mode",
            "auto",
            "--seed",
            CEPH_SEED,
            "--kube-mode",
            "remote",
            "--kube-context",
            "lab",
            "--prom-url",
            "http://prom.example:9090",
            "--since",
            "24h",
            "--redact",
        ),
        knobs={
            "FAKE_CAPS": BOTH_CAPABLE,
            "FAKE_CEPH_SECRET_CONFIG": "1",
            "FAKE_KUBE_TOOLS_POD": "1",
        },
        coverage=("O6", "C5", "C10", "C14", "V3"),
        expected_exit=0,
    ),
    Scenario(
        name="mixed-full-collection-unredacted",
        summary="the same full collection with --no-redact: raw evidence is preserved",
        arguments=(
            "--mode",
            "auto",
            "--seed",
            CEPH_SEED,
            "--kube-mode",
            "remote",
            "--kube-context",
            "lab",
            "--prom-url",
            "http://prom.example:9090",
            "--since",
            "24h",
            "--no-redact",
        ),
        knobs={
            "FAKE_CAPS": BOTH_CAPABLE,
            "FAKE_CEPH_SECRET_CONFIG": "1",
            "FAKE_KUBE_TOOLS_POD": "1",
        },
        coverage=("O8", "O9"),
        expected_exit=0,
    ),
    Scenario(
        name="verify-failure-keeps-workdir",
        summary=(
            "a node smuggles a private key: verification fails, no bundle is "
            "published and the workdir is kept for investigation"
        ),
        arguments=("--mode", "cephadm", "--seed", CEPH_SEED),
        knobs={"FAKE_CAPS": BOTH_CAPABLE, "FAKE_NODE_CASES": "kubenode=pem-leak"},
        coverage=("O18", "B8", "B9"),
        expected_exit=1,
        expect_archive=False,
    ),
    Scenario(
        name="interrupt-cleans-up",
        summary="SIGINT during node collection: exit 130, no workdir and no bundle left",
        arguments=("--mode", "cephadm", "--seed", CEPH_SEED),
        knobs={
            "FAKE_CAPS": BOTH_CAPABLE,
            "FAKE_NODE_SLEEPS": "monitor01=30 kubenode=30",
        },
        coverage=("O17", "O35"),
        expected_exit=130,
        expect_archive=False,
        interrupt_after=r"\[1/2\].*monitor01",
    ),
)
