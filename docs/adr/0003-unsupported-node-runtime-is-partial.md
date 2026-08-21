# An unsupported node runtime produces a partial collection

If an inventory node has no `python3`, does not provide CPython, or has an interpreter older than Python 3.10, that node is recorded as a Skipped Node and the remaining collection continues best-effort; any final Incident Bundle that is successfully delivered is a Partial Collection. Preserving available incident evidence is more valuable than abandoning later evidence attempts; the collector must not fall back to shell, install or modify Python, or probe alternate interpreter names.
