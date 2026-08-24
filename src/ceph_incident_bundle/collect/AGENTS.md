# Collection boundary

Before changing archive admission or Incident Bundle publication, read
`docs/read-only-safety.md` and the relevant ADRs, especially ADR-0004, ADR-0012, and ADR-0015.

- Validate every Node Evidence Archive completely before extraction; reject unsafe paths, links,
  special members, collisions, and schema violations.
- Preserve admitted evidence, best-effort collection, no-overwrite publication, and cleanup limited
  to invocation-owned resources.
- The local workstation, invoking user, inventory author, and configured workspace/output parents
  are trusted. Do not add hostile-local replacement defenses without an explicit requirement.
