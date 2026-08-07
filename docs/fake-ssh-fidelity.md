# Fake SSH fidelity contract

## Why this contract exists

The offline suites have seven `ssh` replacements. They are safety whitelists as
well as test data providers, but they used to disagree about one OpenSSH
property: OpenSSH reads stdin to EOF for every invocation, even when the remote
command does not use it. The gentler fakes hid the lost-crash-ID defect found by
the real-lab run in #52. A separate fixture-shape error omitted the node
workspace's `out/` directory and made the lab bundle reduction silently ask
about members that could not exist.

This document inventories the intentional differences and defines the small
surface that must stay common.

## Inventory

All seven fakes keep remote output on stdout and transport/remote diagnostics on
stderr. The debug-probe fixtures intentionally emit recognisable lines on both
streams so their callers exercise both capture paths; that is test stimulus,
not a claim that OpenSSH writes debug output to stdout.

| Fake | Invocation surface | stdin | Exit status | argv and quoting model |
| --- | --- | --- | --- | --- |
| `tests/fixtures/bin/ssh` | Shell reference's direct Ceph reads | Drains every non-TTY invocation before dispatch | `0`, configured `17`, or reject `99` | Flattens argv through `$*` and pattern-matches the resulting command string; argument boundaries are not preserved |
| `tests/fixtures/python-node/bin/ssh` | Python node bootstrap and its transport-failure modes | Reads the payload before canned modes; timeout mode `exec`s the remote shell with stdin still attached | Passes through the remote shell status, or the selected canned status | Records the outer argv list, then executes the final remote-command string with `/bin/sh -c`; it does not whitelist the outer option vector |
| `tests/fixtures/python-ceph/bin/ssh` | Debug, capability, node bootstrap, Ceph runner probes, direct/sudo Ceph reads | Drains every non-TTY invocation before dispatch | Canned remote/transport statuses, including configured Ceph failures | Matches the complete outer argv list; parses the one-string bootstrap with `shlex`; direct Ceph commands remain token lists |
| `tests/fixtures/python-prometheus/bin/ssh` | Debug and the node bootstrap needed by the public collect CLI | Drains every non-TTY invocation before dispatch | Bootstrap's configured status, debug `255`, or reject `99` | Matches the complete outer argv list and a digest-pinned, `shlex`-parsed bootstrap string |
| `tests/fixtures/python-rook/bin/ssh` | Debug, capability, node bootstrap and remote read-only kubectl | Drains every non-TTY invocation before dispatch | Preserves the delegated fake-kubectl status; other statuses are canned | Matches the complete outer argv list; passes remote kubectl tokens without a shell; parses only the bootstrap string |
| `tests/differential/fakes/ssh` | Both implementations' debug, capability, node, Ceph and Rook shapes | Drains once before all dispatch and retains the bytes for node-ledger comparison | Canned scenario status, except delegated kubectl preserves its status | Matches the complete outer argv; shell-node strings use a constrained grammar, Python bootstrap uses `shlex`, direct commands remain token lists |
| `tests/fixtures/lab/bin/ssh` | Lab identity, stable-state and residue probes | Drains every non-TTY invocation before dispatch | Canned statuses matching each probe's real failure class | Matches the complete option and remote-command vectors; residue is the one recognised command grammar |

The TTY exception exists only so a developer can run a fixture directly without
blocking on their terminal. Collector and test invocations use a pipe, so the
EOF rule still applies.

## Design decision

Keep the seven SSH fakes independent. Each suite owns a different whitelist and
failure vocabulary; making the differential fake and a black-box fixture call
the same transport implementation would create a correlated oracle and could
let one mistake bless both sides of a comparison.

Share assertions instead:

- `tests/test_python_fake_fidelity.py` gives every executable a pipe and a
  rejected invocation. The observer end must be empty after exit. Exercising
  the earliest reject path proves stdin consumption happens before route
  selection instead of only in a node-bootstrap branch.
- `tests/fixture_product.py` is the single source used by fabricated lab-bundle
  records for the node product shape: manifests name
  `<workspace>/out/<relative>`, while workstation bundles name
  `nodes/<alias>/<relative>`. A worked example pins both paths.

The fakes may continue to differ in whitelist, ledger and failure knobs. A new
fake must join the shared stdin contract and document its argv and stream model
here.

`docs/behavior-contract.md` line-anchor drift is a different problem (documents
pointing at code rather than fixtures modelling a real system), so #68 does not
change or validate those anchors.

## Verification record

On 2026-08-07 the new stdin contract first failed for the five fakes that only
drained a node-bootstrap branch or never drained stdin. After moving the drain
ahead of dispatch, the existing suites exposed two fixture-harness mistakes in
the change itself: interactive test runners need the existing TTY guard, and
the isolated differential world must copy the shared product-shape helper next
to its copied fake. Neither failure was in either collector.

With those harness corrections, the existing Python modules and all 18
differential tests passed without exposing a third collector defect. The final
offline gate was `make validate`; no real-lab command or collection was run for
#68.
