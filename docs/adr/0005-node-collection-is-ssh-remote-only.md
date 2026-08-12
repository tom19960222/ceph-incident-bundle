# Node collection is initiated remotely over SSH

Operators run collection from a Collection Workstation, which transfers and executes the ephemeral Python Remote Node Collector on each Target Node over SSH and receives its evidence. The product does not expose a separate run-locally-on-the-node mode; one remote-node flow keeps behavior, safety boundaries, and tests consistent, and multi-node collection repeats that same unit of work.
