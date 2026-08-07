"""Single source for collector-owned paths fabricated by offline fixtures."""

from __future__ import annotations

from pathlib import PurePosixPath


NODE_FIXTURE_WORKSPACE = PurePosixPath("/tmp/ceph-incident-node.fixture")
NODE_OUTPUT_DIRECTORY = "out"
NODE_BUNDLE_DIRECTORY = "nodes"


def node_manifest_artifact(
    relative: str, *, workspace: str | PurePosixPath = NODE_FIXTURE_WORKSPACE
) -> str:
    """The workspace-absolute artifact path a node manifest records."""

    return str(PurePosixPath(workspace) / NODE_OUTPUT_DIRECTORY / relative)


def node_bundle_member(alias: str, relative: str) -> str:
    """Where the same relative artifact is packed in a workstation bundle."""

    return str(PurePosixPath(NODE_BUNDLE_DIRECTORY) / alias / relative)
