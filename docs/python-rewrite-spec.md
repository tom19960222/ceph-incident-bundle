# Rewrite the incident evidence collector in Python

## Problem Statement

During a Ceph incident, an operator needs to preserve as much evidence as possible from an explicitly selected set of servers and, when configured, from a Ceph cluster, an external-Rook Kubernetes cluster, and Prometheus. One unavailable command, unreadable file, unreachable node, or failed optional request must not discard evidence that was collected successfully.

The shell implementation at commit `edc73527e47ec9b5ca7a34f5e35b66b995b62be4` is a capability reference only. Its CLI, input format, output layout, implementation, validation, redaction, and exit-status conventions are not compatibility requirements.

The rewrite needs a deliberately smaller contract:

- Python 3.10 is the production floor, including every Target Node.
- Source readability is the highest implementation priority.
- Collection is operationally read-only: temporary collector-owned work is allowed, but persistent system, Ceph, and Kubernetes state must not be changed.
- Evidence is retained raw. The collector performs no redaction, secret scan, content validation, health judgment, or safe-to-share certification.
- Filesystem safety is structural: links, special objects, unsafe paths, and path collisions must never enter a collector workspace or bundle.
- Collection is best-effort and may deliver a truthful Partial Collection.
- The Collection Workstation alone builds the final bundle.

## Solution

Build a standard-library-only Python package exposing one `ceph-incident-bundle` command and exactly two initial subcommands:

- `generate-inventory` reads a hosts file and drafts an explicit INI Node Inventory without contacting any server.
- `collect` validates the complete inventory before doing work, visits nodes sequentially over exactly one system OpenSSH process per node, optionally collects local `kubectl` and Prometheus evidence, and constructs one local gzip tar Incident Bundle.

The workstation sends one readable, self-contained Python source payload unchanged to the existing remote `python3 -`. The remote program runs fixed read-only Probes, copies eligible regular files without following links, optionally runs the fixed direct-`ceph` Probes, and streams one gzip Node Evidence Archive on SSH standard output. The workstation receives the whole archive into a temporary file, treats it as untrusted, admits every member before extracting any member, and maps admitted evidence beneath the workstation-selected Inventory Name.

Every command Probe preserves byte-for-byte standard output and standard error beside a small execution result. Prometheus HTTP requests preserve raw response bytes beside a small request result. The final bundle always separates `nodes`, `ceph`, `kubernetes`, and `prometheus` evidence and reports only `complete` or `partial` overall outcome.

## Numbered User Stories

### Product and command interface

1. **Use the historical version only as a capability reference.** As a maintainer, I want the rewrite to reconsider capabilities from the minimum so obsolete shell interfaces and implementation choices are not preserved accidentally.
   - No input, output, layout, message, or exit-code compatibility is required.
   - A historical capability is included only when this specification confirms it.

2. **Run all production behavior on CPython 3.10+.** As an operator, I want the same support floor on the workstation and Target Nodes so the deployed collector has one clear runtime contract.
   - Production dependencies are limited to the Python 3.10 standard library.
   - Development and tooling may use newer Python versions.
   - Installation may use an operator-controlled virtual environment and never alters global Python.

3. **Provide a conventional readable package.** As a maintainer, I want a normal installable package so workstation responsibilities can live in focused modules.
   - The package has a standard `pyproject.toml` and `ceph-incident-bundle` console entry point.
   - Only the transferred Remote Node Collector is constrained to one self-contained source file.
   - Explicit domain code and fixed tables are preferred over clever frameworks, generated source, and compatibility layers.

4. **Expose exactly two public subcommands.** As an operator, I want one obvious preparation command and one obvious collection command.
   - The only initial public subcommands are `generate-inventory` and `collect`.
   - There is no verifier, redactor, secret scanner, arbitrary Probe runner, direct-on-node mode, or compatibility command.

5. **Use short conventional defaults.** As an operator, I want common invocations to need few arguments.
   - `generate-inventory` defaults to `/etc/hosts` and `inventory.ini`; it accepts `--hosts-file`, `--output`, and `--force`.
   - `collect` defaults to `inventory.ini`, `--since 24h`, and the current output directory; it accepts `--inventory`, `--since`, and `--output-dir`.
   - Inventory generation never starts evidence collection.

6. **Protect an existing inventory.** As an operator, I want generation to avoid silently destroying a reviewed scope.
   - Existing output causes a nonzero exit and remains unchanged unless `--force` is present.
   - `--force` replaces only the exact requested output file.

### Inventory and collection scope

7. **Define all targets explicitly.** As an operator, I want one INI Node Inventory to be the complete Collection Scope.
   - `[common]` and a nonempty `[nodes]` are required.
   - `[nodes]` maps `inventory_name = ssh_address` in preserved order.
   - The initial and generated common SSH user is exactly `root`; any other value is rejected.
   - Ceph, Kubernetes, or Prometheus observations never add SSH targets.

8. **Keep evidence names distinct from connection addresses.** As an evidence reviewer, I want stable node paths even when connection addresses change.
   - `inventory_name` matches `[A-Za-z0-9][A-Za-z0-9._-]*` and is the exact component below `nodes/`.
   - `ssh_address` accepts a hostname, bare IPv4 address, or bare IPv6 address.
   - It rejects `user@host`, embedded ports, per-node users, keys, and arbitrary SSH options.

9. **Reject ambiguous inventories before side effects.** As an operator, I want all configuration errors reported before collection begins.
   - Only `[common]`, `[nodes]`, `[ceph]`, `[kubernetes]`, and `[prometheus]` and their documented keys are accepted.
   - Duplicate sections, duplicate fixed keys, duplicate node keys, unknown sections, unknown keys, invalid values, unresolved references, and invalid regexes are rejected.
   - Exact, Unicode-NFC, or case-folded Inventory Name Collisions are rejected.
   - Parsing does not silently discard duplicates or perform INI interpolation.
   - All values are validated before creating a workspace, opening output, starting a process, or making a network request.

10. **Generate nodes predictably from a hosts file.** As an operator, I want a reviewable first inventory derived from familiar local data.
    - Parse eligible lines as an IP followed by hostnames; ignore blank, comment-only, malformed, and hostname-less lines.
    - Exclude loopback addresses and conventional localhost/loopback first names.
    - Use only the first hostname after the IP, preserve first-seen line order, and remove later repetitions of the same full hostname.
    - Keep the full hostname as `ssh_address`; do not substitute the IP.
    - Derive `inventory_name` from the hostname's first DNS label. For example, `mon01-123.aaa.com` becomes `mon01-123 = mon01-123.aaa.com`.

11. **Make generated name collisions actionable.** As an operator, I want colliding candidates retained so I can rename the correct nodes myself.
    - Emit every colliding active entry without automatic suffixing or dropping it.
    - Put an `ACTION REQUIRED` comment immediately before every colliding entry.
    - Print all conflicts to standard error, still write the inventory, and return nonzero.
    - `collect` rejects that inventory before any collection until the names are unique.

12. **Generate useful explicit defaults.** As an operator, I want the generated file to document every initial capability.
    - `[common]` contains `ssh_user = root`, `probe_timeout = 30m`, and `ssh_connect_timeout = 15s`.
    - `[ceph] source` is the first generated node, in hosts-file order, whose hostname contains `mon`, `cp`, or `cm` case-insensitively. If none matches, emit `# source =` and do not guess.
    - `[kubernetes]` contains `# context =`, `consumer_namespace = rook-ceph-external`, and `operator_namespace = rook-ceph`.
    - `[prometheus]` contains `# url =`, an empty `metrics_filter_regex =`, `query_step = 15s`, and `request_timeout = 5m`.

13. **Use one explicit Ceph source.** As an operator, I want cluster commands tied to one reviewed node.
    - An active `[ceph] source` must name one admitted node.
    - Its fixed Ceph query set runs exactly once in that node's existing SSH session.
    - There is no runtime source discovery, capability probe, source fallback, or replacement when the source fails.

14. **Use an explicit Kubernetes context.** As an operator running from a jump host, I want every Kubernetes request tied to the intended cluster.
    - Rook collection is disabled when `context` is absent.
    - When enabled, every `kubectl` argument vector carries the configured context and namespace explicitly.
    - The ambient current context is never used as a fallback.

15. **Make Prometheus explicitly optional.** As an operator, I want no implicit monitoring endpoint contacted.
    - Prometheus collection is disabled when `url` is absent.
    - The URL, if present, is an absolute HTTP or HTTPS base URL with a host and no query or fragment.
    - Credentials embedded in the URL are allowed, preserved unmasked, and receive no special handling.

### Orchestration and transport

16. **Collect sequentially in a stable order.** As an operator, I want behavior that is easy to understand and reproduce.
    - Visit Target Nodes in inventory order and each node's Probes in built-in order.
    - Collect Ceph during the configured source node's session.
    - After all nodes, run configured Kubernetes, then configured Prometheus, then package the bundle.
    - There is no worker pool, node/Probe concurrency, or `--jobs` option initially.

17. **Use system OpenSSH without a local shell.** As an operator, I want established SSH configuration to remain authoritative.
    - Start `ssh` with a direct argument vector, never `shell=True` or an interpolated local command.
    - OpenSSH remains responsible for keys, ports, jump hosts, host verification, known hosts, and ordinary user configuration.
    - Force noninteractive operation with batch mode and no pseudo-terminal; the collector neither accepts new keys automatically nor prompts for input.

18. **Use exactly one SSH process per Target Node.** As an operator, I want a small remote footprint.
    - Do not use SCP, SFTP, a second session, or a session per Probe.
    - The Ceph source uses the same SSH process for node and Ceph evidence.
    - `ssh_connect_timeout` controls only connection establishment and initial handshake; `0` leaves OpenSSH's configured timeout unchanged.
    - There is no total node-collection or archive-transfer timeout.

19. **Transfer one standalone collector unchanged.** As a maintainer, I want the remote code to be directly inspectable.
    - Send the checked-in self-contained Python source unchanged on SSH standard input to the fixed remote `python3 -` invocation.
    - Do not use zipapp, base64, generated source, remote installation, a remote virtual environment, or alternate interpreter probing.
    - Pass only fixed switches and validated canonical decimal/boolean values to the remote invocation.

20. **Reserve each SSH stream for one protocol role.** As a maintainer, I want archive data impossible to confuse with diagnostics.
    - Standard input is only the Python source followed by EOF.
    - Standard output is only one gzip Node Evidence Archive.
    - Standard error is only SSH or Remote Node Collector diagnostics.
    - The remote program streams the archive and never leaves a remote archive file.

21. **Handle an unsupported remote runtime as a Skipped Node.** As an operator, I want other evidence retained when one server cannot run the collector.
    - Missing `python3`, non-CPython, or a version older than 3.10 yields no admitted archive for that node, prints one concise node-specific line, makes the result partial, and continues.
    - Do not install or repair Python and do not fall back to shell collection.

22. **Treat every Node Evidence Archive as untrusted.** As an operator, I want a compromised node unable to escape the workstation workspace.
    - Receive the complete stream into a collector-owned regular temporary file.
    - Inspect every member before extracting any member.
    - Admit only ordinary directories and regular files beneath the allowed `node/` root and, only for the Ceph source, `ceph/` root.
    - Reject the entire archive for corruption, truncation, unknown roots, absolute/traversal/ambiguous paths, duplicates, normalized collisions, file/descendant conflicts, links, devices, FIFOs, sockets, or any other special type.
    - Never repair, rename, selectively extract, or partially admit an unsafe archive.

23. **Map admitted node evidence under workstation-owned names.** As an operator, I want remote input unable to choose final destinations.
    - Map `node/` to `nodes/<inventory_name>/`.
    - Map the configured source's `ceph/` to top-level `ceph/`.
    - Extract manually inside the collector-owned workspace only after full admission.

24. **Preserve useful complete archives despite SSH status.** As an operator, I want structurally safe evidence retained when transport reports a late error.
    - A complete admitted archive is preserved even if SSH exits nonzero; print one concise warning and mark partial.
    - Connection failure, interruption, incomplete transfer, or rejected structure admits nothing and creates no node directory.
    - Do not put SSH stderr, a transport Capture, a failure ledger, or transport metadata in the bundle.

### Evidence and Probe model

25. **Use built-in fixed Evidence Probes only.** As an operator, I want an auditable read-only command surface.
    - Each Probe has a stable lowercase kebab-case name, stable area, and fixed argument vector.
    - Execute directly without a shell.
    - No operator-supplied command or plug-in command surface is accepted initially.

26. **Preserve every attempted Probe independently.** As an evidence reviewer, I want raw streams separated from collector facts.
    - Each capture contains `stdout`, `stderr`, and `result.json`.
    - Preserve stdout and stderr byte-for-byte, including empty, binary, and non-UTF-8 streams; never add headers or merge them.
    - Missing commands, nonzero exits, and timeouts do not stop later independent work and make the delivered collection partial.
    - Minimal parsing of a successful control response may only schedule confirmed dependent crash, Pod-log, or Prometheus requests and never changes Raw Evidence.

27. **Apply one independent command timeout.** As an operator, I want a stuck command bounded without adding total collection limits.
    - `probe_timeout` applies independently to node, Ceph, and Kubernetes Probes; `0` disables it.
    - On timeout, terminate the Probe process group, retain bytes already produced, record a timed-out result with null exit code, and continue.
    - It does not apply to regular-file copying, SSH archive transfer, Prometheus HTTP, or final packaging.

28. **Copy only no-follow regular-file bytes.** As an operator, I want source links and special objects excluded without losing ordinary file evidence.
    - Recursively walk configured roots without following symbolic links.
    - Recheck at open time and copy only a source that is still a regular file.
    - A hard-linked source may be copied as independent bytes, but no link identity is preserved.
    - Preserve bytes and mirrored source path only; do not promise source mode, owner, group, mtime, ACLs, xattrs, SELinux labels, or hard-link relationships.
    - A selected file inspection/read/copy failure prints one concise line, marks partial, creates no placeholder, and does not stop other files.

### Target Node evidence

29. **Collect the fixed node baseline.** As an incident responder, I want comparable point-in-time operating-system evidence from every admitted node.
    - Attempt hostname, current UTC, kernel, uptime, CPU, memory, processes, filesystems, block devices, I/O, LVM, addresses, kernel messages, failed units, and Podman/Docker listings as the fixed Probe catalog below.
    - Copy `/etc/os-release`, `/etc/hosts`, and `/etc/resolv.conf` only when the source itself is regular.

30. **Attempt every supported time implementation.** As an incident responder, I want raw time state without guessing which daemon is active.
    - Attempt all fixed Chrony, ntpd, and systemd-timesyncd Probes independently.
    - Consider `/etc/chrony.conf`, `/etc/chrony/chrony.conf`, `/etc/ntp.conf`, `/etc/systemd/timesyncd.conf`, and direct `*.conf` children of `/etc/systemd/timesyncd.conf.d/` under the ordinary regular-file rule.
    - Missing source files are normal omissions; a command that was attempted and is unavailable is a Probe failure.

31. **Use one shared relative evidence window.** As an operator, I want one `--since` value for filesystem logs, journald, Kubernetes logs, and Prometheus ranges.
    - Accept a positive integer plus `m`, `h`, `d`, or `w`; default to `24h`.
    - Each node calculates one cutoff from its own clock when its log collection starts and uses that cutoff for both `/var/log` selection and journal argv.
    - Do not align clocks or promise an exact cross-node boundary.

32. **Preserve eligible `/var/log` files in full.** As an incident responder, I want original log files rather than normalized excerpts.
    - Recursively consider regular files, including compressed, binary, rotated, and raw `/var/log/journal` files.
    - Use only file mtime for approximate eligibility; when mtime is within the window, copy the entire file unchanged.
    - Do not parse, decompress, merge, rename, redact, classify, truncate, or cap file/member/byte counts.

33. **Collect one all-system journal.** As an incident responder, I want a broad readable journal without per-service guesses.
    - Run exactly one fixed all-system journal Probe for the node cutoff.
    - Do not add separate Ceph, time-daemon, or other unit journals.

34. **Collect node-local Ceph configuration without daemon data.** As an incident responder, I want configuration and a metadata view without copying OSD databases or block content.
    - Copy every regular file recursively under `/etc/ceph`, including keyrings and files containing credentials.
    - Under `/var/lib/ceph`, copy only regular files whose basename is `ceph.conf`, `config`, `*.conf`, or `*.config`.
    - Produce a recursive metadata listing with relative path, filesystem type, size, and mtime, without following links or copying other daemon data.

### Ceph Cluster Evidence

35. **Run only direct `ceph` commands.** As an operator, I want cluster evidence without runner discovery or container startup.
    - Run the fixed catalog once on the configured source in that node's SSH session.
    - Never invoke `cephadm`, `cephadm shell`, `sudo`, a toolbox, or any runner fallback.
    - Each command is independent; a failure does not stop the remaining Ceph Probes.

36. **Preserve the historical structured and text query set.** As an incident responder, I want the confirmed cluster state views retained.
    - Attempt all fixed JSON and text commands in the Ceph catalog below.
    - Preserve raw stdout even when a command's name says JSON; do not validate or rewrite it.

37. **Collect up to ten crash details.** As an incident responder, I want bounded dependent detail for the crash list.
    - Only a successfully exited, parseable `crash ls --format json-pretty` schedules detail Probes.
    - Use the first ten `crash_id` values in response order.
    - Use sequence-only capture names; keep the full external ID only in argv and Raw Evidence.
    - A required control parse failure marks partial and does not discard the crash-list capture.

### External Rook evidence

38. **Run `kubectl` only on the Collection Workstation.** As an operator using a jump host, I want its configured client and credentials used directly.
    - Do not run remote kubectl or choose a Kubernetes source node.
    - Every invocation is a direct argument vector with explicit context and namespace.

39. **Collect broad external-Rook objects and events.** As an incident responder, I want a first read-only snapshot that can be refined after live use.
    - In the consumer namespace, collect Pods wide, time-sorted Events, and YAML for CephCluster, CephBlockPool, CephFilesystem, and CephObjectStore.
    - Preserve Pod JSON from the consumer and operator namespaces, deduplicating when they are equal.
    - The initial topology is the external-Ceph consumer/operator namespace pattern only.

40. **Collect logs for every listed container.** As an incident responder, I want broad Rook logs without assuming one Pod name.
    - From preserved Pod JSON, minimally parse identities and restart status.
    - Attempt current logs within `--since` for every regular, init, and ephemeral container in both namespaces.
    - Also attempt previous logs when the matching status has `restartCount > 0`.
    - Use one Probe per Pod/container/log generation and sequence-only paths; external names remain only in argv and Raw Evidence.

41. **Keep Kubernetes operations read-only.** As an operator, I want evidence collection never to mutate or enter workloads.
    - The only allowed command families are `kubectl get` and `kubectl logs` from the fixed catalog.
    - Never use `exec`, `cp`, `apply`, `create`, `patch`, `replace`, `delete`, `rollout`, `scale`, `port-forward`, a toolbox, or Pod-hosted `ceph`.
    - A failed or unparseable control Probe blocks only dependent log requests, marks partial, and lets independent work continue.

### Prometheus Evidence

42. **Use standard-library HTTP GET only.** As an operator, I want no curl process or third-party HTTP runtime.
    - Build URLs with standard URL utilities and issue only GET requests from the workstation.
    - Stream each body incrementally into its raw `response` file.
    - Preserve non-success bodies and bytes received before a later timeout/read failure.

43. **Discover jobs and metrics before range queries.** As an incident responder, I want available Ceph/node series collected without a fixed metric list.
    - Preserve buildinfo, active targets, job-label values, and per-job metric-name discovery responses.
    - Select jobs by case-insensitive search for the fixed regex `ceph|node`.
    - For each selected job in response order, discover metric names in the shared range, remove exact repetitions by first occurrence, and apply `metrics_filter_regex` to each name.
    - An empty `metrics_filter_regex` admits all discovered names.

44. **Issue one range request per job/metric pair.** As an incident responder, I want the original association retained even when the same metric occurs in several jobs.
    - Query each admitted `(job_name, metric_name)` with both escaped label matchers.
    - Preserve selected jobs and metrics in returned order.
    - Use the configured `query_step` unchanged; never auto-enlarge it.
    - Do not impose metric, sample, response, request-count, or total-byte limits.

45. **Use request timeout as a no-progress timeout.** As an operator, I want stalled HTTP operations bounded without imposing a total duration.
    - `request_timeout` defaults to `5m`; `0` disables it.
    - It applies to each blocking connect/read operation, not to total request lifetime; a progressing response may run indefinitely.
    - A failed request or required control parse blocks only dependent requests, marks partial, and lets independent requests continue.

46. **Keep external Prometheus names out of paths.** As an evidence reviewer, I want arbitrary label values unable to create filesystem problems.
    - Fixed request names have fixed directories.
    - Per-job metric discovery and per-pair range captures use one-based sequence directories padded to at least six digits with no maximum width.
    - Full job and metric names live in result JSON and Raw Evidence, never path components.

### Bundle, outcomes, and cleanup

47. **Build one local Incident Bundle.** As an operator, I want remote servers to produce transport evidence only.
    - The workstation alone assembles a gzip-compressed tar archive named and rooted `ceph-incident-bundle-YYYYMMDDTHHMMSSZ` with `.tar.gz` on the filename only.
    - Use the collection's UTC start second; no special same-second collision scheme is required.
    - Do not overwrite an existing destination; an ordinary destination creation failure is fatal.
    - Publish the final name only after the archive closes successfully; remove incomplete staging/output after failure or interruption.

48. **Use a small stable root layout.** As an evidence reviewer, I want evidence separated by its major source.
    - Root contains exact input bytes as `inventory.ini`, minimal `collection.json`, and the four directories `nodes/`, `ceph/`, `kubernetes/`, and `prometheus/`.
    - Always create all four directories, leaving an unconfigured/empty capability directory empty without `SKIPPED` files.
    - An admitted node has `nodes/<inventory_name>/probes/` and `files/`; copied absolute paths are mirrored under `files/` after removing only the leading slash.
    - A Skipped Node has no node directory and remains visible only in the Inventory Snapshot.

49. **Construct only structurally admissible output.** As an operator, I want the final bundle safe to extract without claiming its contents are trustworthy.
    - Every final member is an ordinary directory or regular file at one unique safe relative path.
    - Apply the same absolute/traversal, NFC/case-fold collision, file/descendant, link, and special-object exclusions during construction.
    - Do not add a verifier, manifest, hash list, error ledger, completeness claim, or safe-to-share judgment.

50. **Distinguish delivery from completeness.** As an automation caller, I want exit status to answer whether a usable bundle was delivered.
    - Any actually attempted Probe, selected file, node, cleanup, Kubernetes request, or Prometheus request/control failure makes the bundle partial.
    - Unconfigured optional capabilities, nonexistent optional paths, out-of-window logs, skipped links/special objects, and filtered metrics do not by themselves make it partial.
    - `collect` exits zero whenever it delivers the final bundle, complete or partial, and its final stdout line contains the path plus `(complete)` or `(partial)`.
    - Startup rejection or a fatal workstation error that prevents delivery exits nonzero.

51. **Print simple operator-visible failures.** As an operator, I want immediate readable feedback without another in-bundle reporting system.
    - Print concise node, path, cleanup, and capability failure/warning lines on the main command's stdout and continue where possible.
    - Do not add transport stderr, placeholders, error manifests, debug logs, or a failure ledger to the bundle.

52. **Clean collector-owned work.** As an operator, I want ephemeral collection work removed without broad cleanup behavior.
    - Use unique collector-owned workspaces locally and remotely and remove only those exact workspaces.
    - Cleanup runs on ordinary success and failure; a reported remote cleanup failure after admitted evidence makes the bundle partial without discarding it.
    - On Ctrl-C, terminate the active child process group, attempt normal remote and local cleanup, remove incomplete output, deliver no bundle, and exit 130.
    - Uncatchable process or machine termination is not claimed to guarantee cleanup.

53. **Remain operationally read-only.** As an operator, I want collection to preserve the state being investigated.
    - Do not change persistent configuration, services, packages, mounts, Ceph desired state, or Kubernetes objects/workloads.
    - Collector-owned temporary writes and unavoidable observation-side audit/log/limited metadata effects are allowed.
    - Do not implement privilege escalation; every remote action runs as the configured root user.

54. **Preserve Raw Evidence without content policy.** As an evidence reviewer, I want maximum fidelity for internal analysis.
    - Do not redact, mask, scan for secrets, reject credential-like content, semantically validate, or apply special permissions because of content.
    - Keyrings, URL credentials, binary data, and unexpectedly large evidence may be present and are treated like any other bytes.

## Implementation Decisions

### Strict inventory schema

The accepted fixed keys are:

| Section | Keys | Required behavior |
|---|---|---|
| `[common]` | `ssh_user`, `probe_timeout`, `ssh_connect_timeout` | Section and `ssh_user = root` are required; omitted timeouts use `30m` and `15s`. |
| `[nodes]` | operator-defined Inventory Names | Section must contain at least one entry. |
| `[ceph]` | `source` | Absent key disables Ceph Cluster Evidence; a present value must be nonempty and reference `[nodes]`. |
| `[kubernetes]` | `context`, `consumer_namespace`, `operator_namespace` | Absent context disables Rook; namespaces default to the generated defaults. |
| `[prometheus]` | `url`, `metrics_filter_regex`, `query_step`, `request_timeout` | Absent URL disables Prometheus; remaining values default to the generated defaults. An empty metrics regex is valid. |

`--since` accepts `[1-9][0-9]*(m|h|d|w)`. `probe_timeout` uses the same units and additionally accepts the exact value `0`. `ssh_connect_timeout`, `query_step`, and `request_timeout` accept a positive integer plus `s`, `m`, `h`, `d`, or `w`; the two timeout fields also accept `0`. Convert remote control values to canonical base-ten seconds before the SSH invocation.

### One-SSH invocation

Use the semantic invocation `ssh -T -o BatchMode=yes`, add `-o ConnectTimeout=<seconds>` only when configured nonzero, then the validated `root@<ssh_address>` destination and fixed remote `python3 -` command. Remote arguments are limited to canonical `--since-seconds`, `--probe-timeout-seconds`, and a fixed `--collect-ceph` flag for the selected source. OpenSSH may serialize the remote command through the server shell, so no inventory or external identifier is placed in that command.

The Remote Node Collector creates one unique temporary directory, executes/copies evidence there, writes only `node/` and optional `ceph/` into a gzip tar stream on stdout, and removes its directory in `finally`. The workstation drains stdout and stderr without deadlock, saves the whole archive stream before admission, and never exposes remote stderr in bundle artifacts.

### Portable path admission

After tar long-name processing, interpret member names as POSIX paths. Reject empty names, absolute names, backslashes or other ambiguous separator forms, empty/`.`/`..` components, unknown roots, duplicate logical paths, and file-as-ancestor conflicts. Build a portable comparison key by applying Unicode NFC normalization and case folding to every component; reject any collision without changing the admitted spelling. Admit only regular files and directory entries, with no links or special types. Perform a complete pass before manually creating any admitted output member.

### Probe Capture schema

Every Probe Capture has exactly `stdout`, `stderr`, and `result.json`. `result.json` contains exactly:

| Field | Contract |
|---|---|
| `argv` | JSON array of exact argument strings. |
| `started_at` | RFC 3339 UTC string ending in `Z`. |
| `finished_at` | RFC 3339 UTC string ending in `Z`. |
| `outcome` | `exited`, `failed_to_start`, or `timed_out`. |
| `exit_code` | Integer only for `exited`; otherwise `null`. |
| `error` | `null`, or exactly `{ "kind": <string>, "message": <string> }` for collector failures. |

An ordinary nonzero process exit is still `outcome: exited`, retains its integer status, and has `error: null`. Fixed Probe directories use the Probe name. Dynamic names append `-000001`, `-000002`, and so on, increasing width rather than imposing an upper bound.

### Fixed Target Node Probe catalog

| Probe name | Exact argv |
|---|---|
| `hostname` | `hostname` |
| `current-utc` | `date -u +%Y-%m-%dT%H:%M:%SZ` |
| `uname` | `uname -a` |
| `uptime` | `uptime` |
| `lscpu` | `lscpu` |
| `free` | `free -h` |
| `processes` | `ps auxfww` |
| `df` | `df -hT` |
| `lsblk` | `lsblk -a -o NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL` |
| `iostat` | `iostat -xz 1 3` |
| `pvs` | `pvs --noheadings --separator " "` |
| `vgs` | `vgs --noheadings --separator " "` |
| `lvs` | `lvs --noheadings --separator " "` |
| `ip-address` | `ip addr show` |
| `dmesg` | `dmesg -T` |
| `failed-units` | `systemctl --failed --no-pager --plain` |
| `podman-ps` | `podman ps -a` |
| `docker-ps` | `docker ps -a` |
| `chronyc-tracking` | `chronyc tracking` |
| `chronyc-sources` | `chronyc sources -v` |
| `ntpq-peers` | `ntpq -pn` |
| `timedatectl-status` | `timedatectl status` |
| `timedatectl-show-timesync` | `timedatectl show-timesync --all` |
| `timedatectl-timesync-status` | `timedatectl timesync-status` |
| `systemd-timesyncd-status` | `systemctl status systemd-timesyncd --no-pager --plain` |
| `journal-system` | `journalctl --since <node-cutoff-rfc3339> --no-pager --utc --output=short-iso-precise` |

The quoted single-space separator above is one argv value containing one space, not shell syntax.

### Fixed direct-Ceph Probe catalog

| Probe name | Exact argv after `ceph` |
|---|---|
| `status-json` | `status --format json-pretty` |
| `health-detail-json` | `health detail --format json-pretty` |
| `versions-json` | `versions --format json-pretty` |
| `df-detail-json` | `df detail --format json-pretty` |
| `osd-tree-json` | `osd tree --format json-pretty` |
| `osd-df-json` | `osd df --format json-pretty` |
| `osd-dump-json` | `osd dump --format json-pretty` |
| `osd-perf-json` | `osd perf --format json-pretty` |
| `osd-blocked-by-json` | `osd blocked-by --format json-pretty` |
| `pg-stat-json` | `pg stat --format json-pretty` |
| `pg-dump-json` | `pg dump --format json-pretty` |
| `pg-dump-stuck-json` | `pg dump_stuck --format json-pretty` |
| `mon-dump-json` | `mon dump --format json-pretty` |
| `quorum-status-json` | `quorum_status --format json-pretty` |
| `mgr-dump-json` | `mgr dump --format json-pretty` |
| `orch-host-ls-json` | `orch host ls --format json-pretty` |
| `orch-ps-json` | `orch ps --format json-pretty` |
| `orch-device-ls-wide-json` | `orch device ls --wide --format json-pretty` |
| `config-dump-json` | `config dump --format json-pretty` |
| `crash-ls-json` | `crash ls --format json-pretty` |
| `status-text` | `status` |
| `health-detail-text` | `health detail` |
| `osd-tree-text` | `osd tree` |
| `orch-ps-text` | `orch ps` |
| `crash-info-<sequence>` | `crash info <crash-id>` |

Every row's actual argv starts with `ceph`. Crash-detail identifiers come only from the successful crash-list control response.

### Fixed workstation Kubernetes Probe catalog

Use `--context=<context>` and `--namespace=<namespace>` as single argv values so external configuration cannot be reinterpreted as another option.

| Probe name | Exact semantic argv |
|---|---|
| `consumer-pods-wide` | `kubectl --context=<context> --namespace=<consumer> get pods --output=wide` |
| `consumer-events` | `kubectl --context=<context> --namespace=<consumer> get events --sort-by=.lastTimestamp` |
| `consumer-rook-resources-yaml` | `kubectl --context=<context> --namespace=<consumer> get cephclusters.ceph.rook.io,cephblockpools.ceph.rook.io,cephfilesystems.ceph.rook.io,cephobjectstores.ceph.rook.io --output=yaml` |
| `consumer-pods-json` | `kubectl --context=<context> --namespace=<consumer> get pods --output=json` |
| `operator-pods-json` | `kubectl --context=<context> --namespace=<operator> get pods --output=json` |
| `pod-log-<sequence>` | `kubectl --context=<context> --namespace=<namespace> logs <pod> --container=<container> --since=<since>` |
| `pod-previous-log-<sequence>` | same as current log, followed by `--previous` |

When namespaces are equal, use the consumer Pods JSON capture as the single control source and do not duplicate the operator capture or resulting log attempts.

### Prometheus request contract

Capture these GETs in order:

1. `/api/v1/status/buildinfo` at `prometheus/buildinfo/`.
2. `/api/v1/targets` at `prometheus/targets/`.
3. `/api/v1/label/job/values` at `prometheus/job-values/`.
4. `/api/v1/label/__name__/values` with escaped `match[]={job="..."}`, `start`, and `end`, once per selected job at `prometheus/metric-names/<sequence>/`.
5. `/api/v1/query_range` with escaped `{job="...",__name__="..."}`, `start`, `end`, and unchanged `step`, once per admitted pair at `prometheus/query-range/<sequence>/`.

Capture one Prometheus end instant when this capability begins and derive its start by subtracting `--since`. Every capture has `response` and `result.json`. Base result fields are exactly `url`, `started_at`, `finished_at`, `outcome`, `http_status`, and `error`. Metric-name captures additionally contain exactly `job_name`; range captures additionally contain exactly `job_name` and `metric_name`. Timestamps and error shape match Probe results. `outcome` is `received` or `failed`; `http_status` is integer or null. Transport, timeout, read, non-success HTTP, Prometheus API error, or required control-response parse failure is `failed`, while every received body byte remains preserved.

### Bundle contract

The single archive root contains:

```text
ceph-incident-bundle-YYYYMMDDTHHMMSSZ/
  inventory.ini
  collection.json
  nodes/
  ceph/
  kubernetes/
  prometheus/
```

Probe captures remain flat under `nodes/<inventory_name>/probes/`, `ceph/probes/`, and `kubernetes/probes/`. Copied files mirror source paths below `nodes/<inventory_name>/files/`. No external crash ID, Kubernetes name, job name, or metric name becomes a path component.

`collection.json` contains exactly `collector_version`, `started_at`, `finished_at`, `since`, and `outcome`. Timestamps are RFC 3339 UTC ending in `Z`; outcome is exactly `complete` or `partial`. It is not a manifest or detailed report.

## Testing Decisions

1. The highest acceptance seam is the installed CLI, exercised as an external process. Unit tests may cover parsers and structural helpers but cannot replace black-box acceptance.
2. The default suite is fully offline. It uses fixture filesystems, executable fake `ssh` and `kubectl` programs on `PATH`, and a loopback fake Prometheus HTTP server. It never contacts real infrastructure.
3. The production-floor gate runs both the installed workstation package and Remote Node Collector with an actual pre-provisioned CPython 3.10 interpreter in an isolated environment. It proves CPython and exact minor version and never silently substitutes the developer's Python or changes global Python.
4. Fake SSH records argv, stdin bytes, process count, destination, order, stderr, and exit status. It proves one process per node, unchanged source stdin, fixed `python3 -`, noninteractive options, same-session Ceph, no local shell, and no SCP/SFTP. At least one path executes the received source using real CPython 3.10 to produce a genuine archive.
5. Fake SSH also returns complete, nonzero-with-complete, truncated, corrupt, and adversarial archives. Every structural rejection asserts zero extracted members, no node placeholder, an unchanged sentinel outside the workspace, continuation to later sources, and partial outcome when a bundle can still be delivered.
6. Archive fixtures cover absolute/traversal paths, empty and dot components, backslash ambiguity, duplicates, NFC and case-fold collisions, file-as-parent collisions, unknown roots, unauthorized `ceph/`, symbolic/hard links, devices, FIFOs, sockets, corrupt gzip, truncated tar/header/body, and hostile external identifiers.
7. Probe black-box tests independently enumerate the fixed names and argv rather than importing the production table. They cover exit 0, nonzero, missing executable, timeout, binary/non-UTF-8/NUL streams, empty streams, partial bytes before termination, continuation, and exact result schemas.
8. File tests cover no-follow traversal and open-time races, byte equality, mirrored paths, mtime boundaries, complete-file inclusion, out-of-window omission, compressed/binary/raw-journal files, read/stat/copy failures, `/etc/ceph` credentials, and the `/var/lib/ceph` copy exclusion.
9. Inventory CLI tests cover defaults and overrides, precise force behavior, hosts ordering, first-hostname selection, loopback exclusion, full-host deduplication, first-label names, Ceph heuristic, inactive placeholders, generated defaults, exact/portable collisions, IP addresses, and all startup validation. Rejected startup asserts no workspace, output, process, or HTTP activity.
10. A shared fake-boundary event log proves the sequential order: nodes, same-session Ceph at its selected node, Kubernetes, Prometheus, and final packaging.
11. Fake kubectl records complete argv and returns Pod fixtures for regular, init, ephemeral, restarted, malformed, and failed cases. It proves explicit context/namespace, equal-namespace deduplication, safe dynamic paths, dependent-request isolation, and that only `get` and `logs` ever occur.
12. The loopback Prometheus server records method, URL, query encoding, order, and connection behavior. It returns success, non-2xx, invalid control JSON, API errors, empty/binary bodies, delayed first bytes, progressing chunks, partial bodies, and interrupted reads. Tests prove GET-only behavior, exact captures, per-job discovery, per-pair range requests, filter semantics, unchanged step, sequence paths, preserved credentials, no size caps, and inactivity rather than total-duration timeout.
13. Bundle black-box tests re-inspect the produced tar and assert the name/root, exact four evidence directories, byte-identical inventory, exact minimal JSON, admitted/Skipped Node mapping, empty optional directories, flat Probe paths, ordinary file/directory types only, no overwrite, complete/partial exit-zero behavior, final stdout line, and no half-published archive on failure.
14. Cleanup tests cover remote and local success, Probe failure, archive failure/admission rejection, cleanup failure, fatal packaging failure, and Ctrl-C. They assert cleanup stays within the unique owned workspace and no production-boundary event mutates services, packages, mounts, Ceph state, or Kubernetes state.
15. Negative tests assert there is no shell collector, `sudo`, `cephadm`, alternate interpreter, package installation, remote kubectl, `kubectl exec`, curl, arbitrary command, verifier, redactor, secret scanner, or concurrency surface.
16. Live acceptance is separate and explicit. Once the operator provides inventory, access, and authorization, the implementation agent first runs one Target Node to prove real Python 3.10+, one-SSH behavior, readable evidence, and remote cleanup. If that passes, the agent automatically runs the full configured nodes, direct Ceph, Rook, and Prometheus collection, inspects the bundle and residue, and refines the provisional broad Rook-log selection without relaxing the `get`/`logs` boundary.

## Out of Scope

- Compatibility with historical flags, inventory, wording, exit codes, file names, bundle layout, manifests, normalizers, or shell internals.
- Shell collection, direct-on-node public operation, alternate Python discovery, remote installation/venv/repair, non-root operation, privilege escalation, or per-node SSH configuration in inventory.
- Automatic Target Node discovery, Ceph source discovery/fallback, runner detection, `sudo ceph`, any `cephadm`, or Rook toolbox Ceph.
- Remote kubectl, ambient Kubernetes context, `kubectl exec`, Kubernetes mutation, and Rook topologies other than the initial external consumer/operator model.
- Verification, redaction, secret/DLP scanning, URL masking, content validation, health analysis, completeness certification, safe-to-share claims, hashes, manifests, error ledgers, debug bundles, or `SKIPPED` artifacts.
- Arbitrary operator Probes, plug-in commands, node/Probe concurrency, worker pools, and performance optimization.
- Total node/archive/package timeouts, proactive log/metric/response/request/member/byte limits, Prometheus auto-step, decompression, log merging, or truncation.
- Preservation of source file ownership, permission mode, mtime, ACL, xattr, SELinux label, or hard-link identity.
- Destructive live fault injection, automatic live-environment discovery, or live collection before the operator explicitly provides the environment and authority.
- A broad cross-version Ceph/Kubernetes/Prometheus compatibility matrix in the first implementation; live Rook findings update this specification rather than silently widening behavior.

## Further Notes

- The implementation work belongs on the isolated `codex/python-rewrite-edc7352` branch/worktree; the pre-existing dirty working tree remains untouched.
- “Read-only” describes operational state, not an impossible zero-write guarantee. Unique temporary workspaces, output, audit records, ordinary access-time/metadata effects, and API access logs may occur.
- Partial is a successful deliverable state, not a validation grade. Process status reports delivery; `collection.json` and the final stdout line report evidence completeness.
- Data volume is intentionally not optimized in this first version. Preserve eligible raw bytes and allow ordinary capacity, network, or command failures to become partial rather than silently imposing a collector budget.
- The first Rook implementation is intentionally broad because live topology details are not frozen. It may be revised after the operator-provided read-only trial, but it must never introduce `kubectl exec` or a mutating command.
