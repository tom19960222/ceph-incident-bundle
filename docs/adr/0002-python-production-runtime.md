# All production collection code uses Python 3.10 or newer

The workstation collector and ephemeral Remote Node Collector are implemented in Python, with CPython 3.10 as the production compatibility floor. Development and tooling may use newer Python versions, but they must not introduce production syntax or dependencies that exclude Python 3.10; collection does not install packages, create environments, or change the selected interpreter on a remote node.
