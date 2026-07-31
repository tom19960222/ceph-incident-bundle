"""The inputs both Collect implementations are handed: one inventory, one trust store.

Qualification compares two collects of the same lab, so both have to be told
about that lab in the same words.  Both inputs are therefore *derived* from the
active Lab Profile once per run and thrown away with the run: a second,
hand-maintained host list — or a second idea of which host keys to trust — is
exactly what drifts, and a drifted input turns "the two implementations
disagree" into "the two runs were asked different questions".

Only values the profile loader already validated are rendered, so nothing here
can widen what the collect entrypoints accept.  Both files are local-only and
owner-only like every other lab artifact.
"""

from __future__ import annotations

from pathlib import Path

from validation.lab_output import write_owner_only
from validation.lab_profile import LabProfile


INVENTORY_NAME = "inventory.env"


def render_inventory(profile: LabProfile) -> str:
    """Render the shell inventory that describes exactly this profile's lab.

    The shell reference sources this file and the Python candidate parses it, so
    it stays inside the documented inventory grammar: quoted scalar assignments,
    one `HOSTS=( "alias=address" )` block, and comments.
    """

    lines = [
        "# Generated for one real-lab qualification run from Lab Profile",
        f"# {profile.name} ({profile.profile_hash}).",
        "# Local-only, regenerated per run — do not edit and do not commit.",
        f'SSH_USER="{profile.ssh_user}"',
        f'SEED_HOST="{profile.seed_host.address}"',
        f'ROOK_NAMESPACE="{profile.rook_namespace}"',
        f'ROOK_OPERATOR_NAMESPACE="{profile.rook_operator_namespace}"',
        "HOSTS=(",
    ]
    lines += [f'  "{host.name}={host.address}"' for host in profile.hosts]
    lines.append(")")
    return "\n".join(lines) + "\n"


def write_inventory(profile: LabProfile, directory: Path) -> Path:
    """Write the shared inventory into one run's workspace and return its path."""

    path = directory / INVENTORY_NAME
    write_owner_only(path, render_inventory(profile))
    return path


def write_known_hosts_home(directory: Path, host_key_lines: tuple[str, ...]) -> Path:
    """Build the collector-owned HOME whose `.ssh/known_hosts` pins the lab.

    Both entrypoints fall back to `$HOME/.ssh/known_hosts` when host-key trust is
    *not* delegated to them, so handing them a HOME that contains only the keys
    the active profile already trusts is how qualification gets strict host key
    checking without `accept-new` and without reading or writing the operator's
    own known_hosts.  A rotated key then fails the connection outright instead of
    being silently added to somebody's trust store.
    """

    ssh_directory = directory / ".ssh"
    ssh_directory.mkdir(parents=True, exist_ok=True)
    ssh_directory.chmod(0o700)
    known_hosts = ssh_directory / "known_hosts"
    write_owner_only(known_hosts, "".join(f"{line}\n" for line in host_key_lines))
    return known_hosts
