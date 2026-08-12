# An unsupported node runtime produces a partial collection

If an inventory node has no `python3` or its interpreter is older than Python 3.10, that node is recorded as a Skipped Node and the remaining collection continues as a Partial Collection. Preserving available incident evidence is more valuable than failing the whole run; the collector must not fall back to shell, install or modify Python, or probe alternate interpreter names.
