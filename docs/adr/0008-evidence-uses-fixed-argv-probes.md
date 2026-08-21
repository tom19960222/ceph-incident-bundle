# Command evidence uses fixed argument-vector Probes

Command-based evidence is collected through a built-in list of independent Evidence Probes. Python executes each fixed argument vector directly without a shell and preserves raw stdout, stderr, and exit status without interpreting whether the evidence is healthy; missing commands and nonzero exits are recorded and do not stop later Probes. User-supplied arbitrary commands are outside the product boundary, favoring a readable, auditable read-only command surface over extensibility.
