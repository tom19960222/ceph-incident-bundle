# Use system OpenSSH for transport

The Python orchestrator invokes the system `ssh` executable with an explicit argument vector and never through a local shell command string. OpenSSH remains responsible for keys, ports, host verification, jump hosts, and user configuration; collection is noninteractive with `BatchMode=yes` and no pseudo-terminal, and it never overrides the operator's ordinary host-key policy. The rewrite does not add a Python SSH library or reimplement the protocol, keeping the collection code small and readable while retaining mature SSH behavior.
