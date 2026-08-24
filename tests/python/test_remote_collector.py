import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest

from ceph_incident_bundle.remote_collector import NODE_PROBE_CATALOG


REMOTE_COLLECTOR = (
    Path(__file__).parents[2]
    / "src"
    / "ceph_incident_bundle"
    / "remote_collector.py"
)


# Independent specification oracle: do not derive this from the production
# catalog.  These are the exact direct commands and capture names from the
# fixed direct-Ceph Probe table in docs/python-rewrite-spec.md.
EXPECTED_CEPH_PROBES = (
    ("status-json", ("ceph", "status", "--format", "json-pretty")),
    (
        "health-detail-json",
        ("ceph", "health", "detail", "--format", "json-pretty"),
    ),
    ("versions-json", ("ceph", "versions", "--format", "json-pretty")),
    (
        "df-detail-json",
        ("ceph", "df", "detail", "--format", "json-pretty"),
    ),
    ("osd-tree-json", ("ceph", "osd", "tree", "--format", "json-pretty")),
    ("osd-df-json", ("ceph", "osd", "df", "--format", "json-pretty")),
    ("osd-dump-json", ("ceph", "osd", "dump", "--format", "json-pretty")),
    ("osd-perf-json", ("ceph", "osd", "perf", "--format", "json-pretty")),
    (
        "osd-blocked-by-json",
        ("ceph", "osd", "blocked-by", "--format", "json-pretty"),
    ),
    ("pg-stat-json", ("ceph", "pg", "stat", "--format", "json-pretty")),
    ("pg-dump-json", ("ceph", "pg", "dump", "--format", "json-pretty")),
    (
        "pg-dump-stuck-json",
        ("ceph", "pg", "dump_stuck", "--format", "json-pretty"),
    ),
    ("mon-dump-json", ("ceph", "mon", "dump", "--format", "json-pretty")),
    (
        "quorum-status-json",
        ("ceph", "quorum_status", "--format", "json-pretty"),
    ),
    ("mgr-dump-json", ("ceph", "mgr", "dump", "--format", "json-pretty")),
    (
        "orch-host-ls-json",
        ("ceph", "orch", "host", "ls", "--format", "json-pretty"),
    ),
    ("orch-ps-json", ("ceph", "orch", "ps", "--format", "json-pretty")),
    (
        "orch-device-ls-wide-json",
        (
            "ceph",
            "orch",
            "device",
            "ls",
            "--wide",
            "--format",
            "json-pretty",
        ),
    ),
    (
        "config-dump-json",
        ("ceph", "config", "dump", "--format", "json-pretty"),
    ),
    (
        "crash-ls-json",
        ("ceph", "crash", "ls", "--format", "json-pretty"),
    ),
    ("status-text", ("ceph", "status")),
    ("health-detail-text", ("ceph", "health", "detail")),
    ("osd-tree-text", ("ceph", "osd", "tree")),
    ("orch-ps-text", ("ceph", "orch", "ps")),
)


class RemoteCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._default_fixture = TemporaryDirectory()
        fixture_root = Path(self._default_fixture.name)
        (fixture_root / "var/log").mkdir(parents=True)
        (fixture_root / "sitecustomize.py").write_text(
            """import os

_source_root = os.environ["REMOTE_COLLECTOR_FILE_FIXTURES"]
_original_open = os.open

def open(path, flags, *args, **kwargs):
    if os.fspath(path) == "/" and flags & os.O_DIRECTORY:
        return _original_open(_source_root, flags, *args, **kwargs)
    return _original_open(path, flags, *args, **kwargs)

os.open = open
""",
            encoding="utf-8",
        )
        self._previous_pythonpath = os.environ.get("PYTHONPATH")
        self._previous_fixture_root = os.environ.get(
            "REMOTE_COLLECTOR_FILE_FIXTURES"
        )
        os.environ["PYTHONPATH"] = str(fixture_root)
        os.environ["REMOTE_COLLECTOR_FILE_FIXTURES"] = str(fixture_root)

    def tearDown(self) -> None:
        for name, value in (
            ("PYTHONPATH", self._previous_pythonpath),
            ("REMOTE_COLLECTOR_FILE_FIXTURES", self._previous_fixture_root),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._default_fixture.cleanup()

    @staticmethod
    def write_successful_node_probe_commands(directory: Path) -> None:
        script = f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys
event_log = os.environ.get("PROBE_EVENT_LOG")
if event_log:
    with Path(event_log).open("a", encoding="utf-8") as events:
        events.write(json.dumps([os.path.basename(__file__), *sys.argv[1:]]) + "\\n")
print(os.path.basename(__file__))
"""
        for command in (
            "hostname",
            "date",
            "uname",
            "uptime",
            "lscpu",
            "free",
            "ps",
            "df",
            "lsblk",
            "iostat",
            "pvs",
            "vgs",
            "lvs",
            "ip",
            "dmesg",
            "systemctl",
            "podman",
            "docker",
            "chronyc",
            "ntpq",
            "timedatectl",
            "journalctl",
        ):
            executable = directory / command
            executable.write_text(script, encoding="utf-8")
            executable.chmod(0o755)

    @staticmethod
    def write_ceph_probe_command(path: Path) -> None:
        path.write_text(
            f'''#!{sys.executable}
import json
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
with Path(os.environ["CEPH_EVENT_LOG"]).open("a", encoding="utf-8") as events:
    events.write(json.dumps(["ceph", *arguments]) + "\\n")
failed_argv = json.loads(os.environ.get("CEPH_FAIL_ARGV", "null"))
if failed_argv == ["ceph", *arguments]:
    os.write(1, b"failed raw stdout\\x00\\xff")
    os.write(2, b"failed raw stderr\\x00\\xfe")
    raise SystemExit(9)
if arguments == ["crash", "ls", "--format", "json-pretty"]:
    if "CEPH_CRASH_LIST_TEXT" in os.environ:
        os.write(1, os.environ["CEPH_CRASH_LIST_TEXT"].encode())
    else:
        crash_ids = json.loads(os.environ.get("CEPH_CRASH_IDS", "[]"))
        os.write(1, json.dumps([{{"crash_id": value}} for value in crash_ids]).encode())
elif arguments[:2] == ["crash", "info"]:
    os.write(1, b"crash raw stdout\\x00\\xff")
    os.write(2, b"crash raw stderr\\x00\\xfe")
elif arguments == ["status", "--format", "json-pretty"]:
    os.write(1, b"not-json-is-still-raw\\x00\\xff")
else:
    os.write(1, b"ordinary raw stdout\\x00\\xff")
''',
            encoding="utf-8",
        )
        path.chmod(0o755)

    @staticmethod
    def write_log_fixture_sitecustomize(path: Path) -> None:
        path.write_text(
            """import datetime as _datetime
import os
from pathlib import Path

_source_root = os.environ["REMOTE_COLLECTOR_FILE_FIXTURES"]
_source_log = os.path.join(_source_root, "var/log")
_original_open = os.open
_original_fstat = os.fstat
_original_fdopen = os.fdopen
_original_path_open = Path.open
_original_scandir = os.scandir
_fail_stat = os.environ.get("REMOTE_COLLECTOR_FAIL_LOG_STAT")
_fail_fstat = os.environ.get("REMOTE_COLLECTOR_FAIL_LOG_FSTAT")
_fail_open = os.environ.get("REMOTE_COLLECTOR_FAIL_LOG_OPEN")
_fail_read = os.environ.get("REMOTE_COLLECTOR_FAIL_LOG_READ")
_fail_write_suffix = os.environ.get("REMOTE_COLLECTOR_FAIL_WRITE_SUFFIX")
_race_open = os.environ.get("REMOTE_COLLECTOR_RACE_OPEN")
_race_old_open = os.environ.get("REMOTE_COLLECTOR_RACE_OLD_OPEN")
_directory_disappears = os.environ.get(
    "REMOTE_COLLECTOR_LOG_DIRECTORY_DISAPPEARS"
)
_directory_becomes_link = os.environ.get(
    "REMOTE_COLLECTOR_LOG_DIRECTORY_BECOMES_LINK"
)
_directory_becomes_special = os.environ.get(
    "REMOTE_COLLECTOR_LOG_DIRECTORY_BECOMES_SPECIAL"
)
_read_descriptor = None
_fstat_descriptor = None
_raced = False
_raced_old = False
_directory_disappeared = False
_directory_replaced = False

class _FrozenDateTime(_datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 8, 21, 12, 0, 0, tzinfo=_datetime.timezone.utc)
        if tz is None:
            return fixed.replace(tzinfo=None)
        return fixed.astimezone(tz)

def _name(path):
    return os.fspath(path).rsplit("/", 1)[-1]

def _is_source_log_descriptor(path):
    if not isinstance(path, int):
        return False
    try:
        opened = _original_fstat(path)
        source = os.stat(_source_log)
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (source.st_dev, source.st_ino)

class _Entry:
    def __init__(self, entry):
        self._entry = entry
        self.name = entry.name
    def stat(self, *args, **kwargs):
        if _fail_stat and self.name == _name(_fail_stat):
            raise PermissionError("fixture log stat failure")
        return self._entry.stat(*args, **kwargs)

class _Scanner:
    def __init__(self, scanner):
        self._scanner = scanner
        self._iterator = iter(scanner)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return self._scanner.__exit__(*args)
    def __iter__(self):
        return self
    def __next__(self):
        return _Entry(next(self._iterator))

def scandir(path):
    scanner = _original_scandir(path)
    if _fail_stat and _is_source_log_descriptor(path):
        return _Scanner(scanner)
    return scanner

def open(path, flags, *args, **kwargs):
    global _directory_disappeared, _fstat_descriptor, _read_descriptor
    global _directory_replaced, _raced, _raced_old
    source = os.fspath(path)
    if source == "/" and flags & os.O_DIRECTORY:
        return _original_open(_source_root, flags, *args, **kwargs)
    if (
        _directory_disappears
        and source == _directory_disappears
        and flags & os.O_DIRECTORY
        and not _directory_disappeared
    ):
        _directory_disappeared = True
        fixture = os.path.join(_source_log, _directory_disappears)
        os.rename(fixture, fixture + "-after-selection")
    replacement = _directory_becomes_link or _directory_becomes_special
    if (
        replacement
        and source == replacement
        and flags & os.O_DIRECTORY
        and not _directory_replaced
    ):
        _directory_replaced = True
        fixture = os.path.join(_source_log, replacement)
        target = fixture + "-after-selection"
        os.rename(fixture, target)
        if _directory_becomes_link:
            os.symlink(os.path.basename(target), fixture)
        else:
            os.mkfifo(fixture)
    if _race_open and _name(source) == _name(_race_open) and not _raced:
        _raced = True
        fixture = os.path.join(_source_root, _race_open.lstrip("/"))
        target = fixture + "-after-race"
        os.rename(fixture, target)
        os.symlink(os.path.basename(target), fixture)
    if _race_old_open and _name(source) == _name(_race_old_open) and not _raced_old:
        _raced_old = True
        fixture = os.path.join(_source_root, _race_old_open.lstrip("/"))
        os.rename(fixture, fixture + "-after-race")
        Path(fixture).write_bytes(b"older replacement bytes")
        cutoff_ns = 1_787_313_540_000_000_000
        os.utime(fixture, ns=(cutoff_ns - 1, cutoff_ns - 1))
    if _fail_open and _name(source) == _name(_fail_open):
        raise PermissionError("fixture selected-log open failure")
    descriptor = _original_open(path, flags, *args, **kwargs)
    if _fail_fstat and _name(source) == _name(_fail_fstat):
        _fstat_descriptor = descriptor
    if _fail_read and _name(source) == _name(_fail_read):
        _read_descriptor = descriptor
    return descriptor

def fstat(descriptor):
    global _fstat_descriptor
    if descriptor == _fstat_descriptor:
        _fstat_descriptor = None
        raise PermissionError("fixture opened-log stat failure")
    return _original_fstat(descriptor)

class _BrokenRead:
    def __init__(self, opened):
        self._opened = opened
    def __enter__(self):
        return self
    def __exit__(self, *unused):
        self._opened.close()
    def read(self, *unused):
        raise OSError("fixture log read failure")

def fdopen(descriptor, *args, **kwargs):
    global _read_descriptor
    opened = _original_fdopen(descriptor, *args, **kwargs)
    if descriptor == _read_descriptor:
        _read_descriptor = None
        return _BrokenRead(opened)
    return opened

def path_open(self, *args, **kwargs):
    if _fail_write_suffix and str(self).endswith(_fail_write_suffix):
        raise OSError("fixture log workspace write failure")
    return _original_path_open(self, *args, **kwargs)

_datetime.datetime = _FrozenDateTime
os.scandir = scandir
os.open = open
os.fstat = fstat
os.fdopen = fdopen
Path.open = path_open
""",
            encoding="utf-8",
        )

    @staticmethod
    def write_source_root_sitecustomize(path: Path) -> None:
        path.write_text(
            """import os
from pathlib import Path

_source_root = os.environ["REMOTE_COLLECTOR_FILE_FIXTURES"]
_original_open = os.open
_original_fdopen = os.fdopen
_original_path_open = Path.open
_original_scandir = os.scandir
_fail_stat = os.environ.get("REMOTE_COLLECTOR_FAIL_CEPH_STAT")
_fail_open = os.environ.get("REMOTE_COLLECTOR_FAIL_CEPH_OPEN")
_fail_read = os.environ.get("REMOTE_COLLECTOR_FAIL_CEPH_READ")
_fail_write_suffix = os.environ.get("REMOTE_COLLECTOR_FAIL_WRITE_SUFFIX")
_race_open = os.environ.get("REMOTE_COLLECTOR_RACE_CEPH_OPEN")
_read_descriptor = None
_raced = False

def _name(path):
    return os.fspath(path).rsplit("/", 1)[-1]

class _Entry:
    def __init__(self, entry):
        self._entry = entry
        self.name = entry.name
    def __getattr__(self, name):
        return getattr(self._entry, name)
    def stat(self, *args, **kwargs):
        if _fail_stat and self.name == _name(_fail_stat):
            raise PermissionError("fixture Ceph inspection failure")
        return self._entry.stat(*args, **kwargs)

class _Scanner:
    def __init__(self, scanner):
        self._scanner = scanner
        self._iterator = iter(scanner)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return self._scanner.__exit__(*args)
    def __iter__(self):
        return self
    def __next__(self):
        return _Entry(next(self._iterator))

def scandir(path):
    return _Scanner(_original_scandir(path))

def open(path, flags, *args, **kwargs):
    global _raced, _read_descriptor
    if os.fspath(path) == "/" and flags & os.O_DIRECTORY:
        return _original_open(_source_root, flags, *args, **kwargs)
    source = os.fspath(path)
    if not flags & os.O_DIRECTORY:
        if _fail_open and _name(source) == _name(_fail_open):
            raise PermissionError("fixture Ceph open failure")
        if _race_open and _name(source) == _name(_race_open) and not _raced:
            _raced = True
            fixture = os.path.join(_source_root, _race_open.lstrip("/"))
            target = fixture + "-after-race"
            os.rename(fixture, target)
            os.symlink(os.path.basename(target), fixture)
    descriptor = _original_open(path, flags, *args, **kwargs)
    if _fail_read and _name(source) == _name(_fail_read):
        _read_descriptor = descriptor
    return descriptor

class _BrokenRead:
    def __init__(self, opened):
        self._opened = opened
    def __enter__(self):
        return self
    def __exit__(self, *unused):
        self._opened.close()
    def read(self, *unused):
        raise OSError("fixture Ceph read failure")

def fdopen(descriptor, *args, **kwargs):
    global _read_descriptor
    opened = _original_fdopen(descriptor, *args, **kwargs)
    if descriptor == _read_descriptor:
        _read_descriptor = None
        return _BrokenRead(opened)
    return opened

def path_open(self, *args, **kwargs):
    if _fail_write_suffix and str(self).endswith(_fail_write_suffix):
        raise OSError("fixture Ceph workspace write failure")
    return _original_path_open(self, *args, **kwargs)

os.open = open
os.fdopen = fdopen
os.scandir = scandir
Path.open = path_open
""",
            encoding="utf-8",
        )

    def run_remote_collector(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(REMOTE_COLLECTOR), *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def read_hostname_capture(
        self, archive_bytes: bytes, root: Path
    ) -> tuple[set[str], bytes, bytes, dict[str, object]]:
        archive = root / "node.tar.gz"
        archive.write_bytes(archive_bytes)
        with tarfile.open(archive, "r:gz") as opened:
            names = {member.name for member in opened.getmembers()}
            stdout_file = opened.extractfile("node/probes/hostname/stdout")
            stderr_file = opened.extractfile("node/probes/hostname/stderr")
            result_file = opened.extractfile("node/probes/hostname/result.json")
            assert stdout_file is not None
            assert stderr_file is not None
            assert result_file is not None
            return (
                names,
                stdout_file.read(),
                stderr_file.read(),
                json.load(result_file),
            )

    @staticmethod
    def expected_node_probe_catalog() -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Return the hand-written #90 contract, independent of production."""
        expected = (
            ("hostname", ("hostname",)),
            (
                "current-utc",
                ("date", "-u", "+%Y-%m-%dT%H:%M:%SZ"),
            ),
            ("uname", ("uname", "-a")),
            ("uptime", ("uptime",)),
            ("lscpu", ("lscpu",)),
            ("free", ("free", "-h")),
            ("processes", ("ps", "auxfww")),
            ("df", ("df", "-hT")),
            (
                "lsblk",
                (
                    "lsblk",
                    "-a",
                    "-o",
                    "NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL",
                ),
            ),
            ("iostat", ("iostat", "-xz", "1", "3")),
            ("pvs", ("pvs", "--noheadings", "--separator", " ")),
            ("vgs", ("vgs", "--noheadings", "--separator", " ")),
            ("lvs", ("lvs", "--noheadings", "--separator", " ")),
            ("ip-address", ("ip", "addr", "show")),
            ("dmesg", ("dmesg", "-T")),
            (
                "failed-units",
                ("systemctl", "--failed", "--no-pager", "--plain"),
            ),
            ("podman-ps", ("podman", "ps", "-a")),
            ("docker-ps", ("docker", "ps", "-a")),
            ("chronyc-tracking", ("chronyc", "tracking")),
            ("chronyc-sources", ("chronyc", "sources", "-v")),
            ("ntpq-peers", ("ntpq", "-pn")),
            ("timedatectl-status", ("timedatectl", "status")),
            (
                "timedatectl-show-timesync",
                ("timedatectl", "show-timesync", "--all"),
            ),
            (
                "timedatectl-timesync-status",
                ("timedatectl", "timesync-status"),
            ),
            (
                "systemd-timesyncd-status",
                (
                    "systemctl",
                    "status",
                    "systemd-timesyncd",
                    "--no-pager",
                    "--plain",
                ),
            ),
        )
        return expected

    def test_node_probe_catalog_is_exactly_the_non_journal_baseline_and_time_catalog(
        self,
    ) -> None:
        self.assertEqual(NODE_PROBE_CATALOG, self.expected_node_probe_catalog())

    def test_hostname_probe_is_streamed_as_a_complete_node_archive(self) -> None:
        with TemporaryDirectory() as command_directory:
            fake_bin = Path(command_directory)
            self.write_successful_node_probe_commands(fake_bin)
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE_COLLECTOR),
                    "--since-seconds",
                    "86400",
                    "--probe-timeout-seconds",
                    "30",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        with TemporaryDirectory() as directory:
            archive = Path(directory) / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                names = {member.name for member in opened.getmembers()}
                stdout_file = opened.extractfile("node/probes/hostname/stdout")
                stderr_file = opened.extractfile("node/probes/hostname/stderr")
                result_file = opened.extractfile("node/probes/hostname/result.json")
                assert stdout_file is not None
                assert stderr_file is not None
                assert result_file is not None
                stdout = stdout_file.read()
                stderr = stderr_file.read()
                result = json.load(result_file)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertIn("node", names)
        self.assertIn("node/probes", names)
        self.assertIn("node/files", names)
        self.assertTrue(stdout)
        self.assertEqual(stderr, b"")
        self.assertEqual(set(result), {
            "argv",
            "started_at",
            "finished_at",
            "outcome",
            "exit_code",
            "error",
        })
        self.assertEqual(result["argv"], ["hostname"])
        self.assertEqual(result["outcome"], "exited")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["error"])
        self.assertRegex(result["started_at"], r"^\d{4}-\d\d-\d\dT.*Z$")
        self.assertRegex(result["finished_at"], r"^\d{4}-\d\d-\d\dT.*Z$")

    def test_external_subprocess_runs_the_full_catalog_in_documented_order(
        self,
    ) -> None:
        expected_catalog = self.expected_node_probe_catalog()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            event_log = root / "probe-events.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["PROBE_EVENT_LOG"] = str(event_log)

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )

            executed = tuple(
                tuple(json.loads(line))
                for line in event_log.read_text(encoding="utf-8").splitlines()
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                member_names = {member.name for member in opened.getmembers()}

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            executed[:-1], tuple(argv for _, argv in expected_catalog)
        )
        self.assertEqual(executed[-1][0], "journalctl")
        self.assertEqual(executed[-1][1], "--since")
        self.assertRegex(executed[-1][2], r"^\d{4}-\d\d-\d\dT.*Z$")
        self.assertEqual(
            executed[-1][3:],
            ("--no-pager", "--utc", "--output=short-iso-precise"),
        )
        for probe_name, _ in expected_catalog:
            self.assertIn(f"node/probes/{probe_name}/result.json", member_names)
        self.assertIn("node/probes/journal-system/result.json", member_names)

    def test_one_node_cutoff_selects_log_mtimes_and_the_journal_probe(self) -> None:
        cutoff = "2026-08-21T11:59:00Z"
        cutoff_ns = 1_787_313_540_000_000_000
        payloads = {
            "var/log/exact.log": b"complete file at exact cutoff\n",
            "var/log/adjacent-newer.log.gz": b"\x1f\x8bcompressed\x00\xff",
            "var/log/rotated.log.1": b"old record\nnew record\n",
            "var/log/binary.log": b"binary\x00\xff\n",
            "var/log/journal/raw.journal": b"LPKSHHRHraw-journal\x00\xfe",
        }
        omitted = "var/log/adjacent-older.log"

        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            journalctl = fake_bin / "journalctl"
            journalctl.write_text(
                f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys
with Path(os.environ["JOURNAL_EVENT_LOG"]).open("a", encoding="utf-8") as events:
    events.write(json.dumps([os.path.basename(__file__), *sys.argv[1:]]) + "\\n")
os.write(1, b"journal bytes\\x00\\xff")
""",
                encoding="utf-8",
            )
            journalctl.chmod(0o755)

            source_root = root / "sources"
            for relative, payload in payloads.items():
                fixture = source_root / relative
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_bytes(payload)
            old_fixture = source_root / omitted
            old_fixture.write_bytes(b"outside the evidence window")
            os.symlink("exact.log", source_root / "var/log/linked.log")
            os.mkfifo(source_root / "var/log/special.pipe")
            os.utime(source_root / "var/log/exact.log", ns=(cutoff_ns, cutoff_ns))
            os.utime(
                source_root / "var/log/adjacent-newer.log.gz",
                ns=(cutoff_ns + 1, cutoff_ns + 1),
            )
            os.utime(
                source_root / "var/log/binary.log",
                ns=(cutoff_ns + 2, cutoff_ns + 2),
            )
            os.utime(
                source_root / "var/log/rotated.log.1",
                ns=(cutoff_ns + 2, cutoff_ns + 2),
            )
            os.utime(
                source_root / "var/log/journal/raw.journal",
                ns=(cutoff_ns + 3, cutoff_ns + 3),
            )
            os.utime(old_fixture, ns=(cutoff_ns - 1, cutoff_ns - 1))

            self.write_log_fixture_sitecustomize(root / "sitecustomize.py")
            journal_events = root / "journal-events.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["PYTHONPATH"] = str(root)
            environment["REMOTE_COLLECTOR_FILE_FIXTURES"] = str(source_root)
            environment["JOURNAL_EVENT_LOG"] = str(journal_events)

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )

            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                members = {member.name: member for member in opened.getmembers()}
                archived_payloads = {}
                for relative in payloads:
                    member_name = f"node/files/{relative}"
                    payload_file = opened.extractfile(member_name)
                    assert payload_file is not None
                    archived_payloads[relative] = payload_file.read()
                journal_stdout_file = opened.extractfile(
                    "node/probes/journal-system/stdout"
                )
                journal_result_file = opened.extractfile(
                    "node/probes/journal-system/result.json"
                )
                assert journal_stdout_file is not None
                assert journal_result_file is not None
                journal_stdout = journal_stdout_file.read()
                journal_result = json.load(journal_result_file)
            journal_argv = json.loads(
                journal_events.read_text(encoding="utf-8").splitlines()[0]
            )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="backslashreplace"),
        )
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(archived_payloads, payloads)
        self.assertNotIn(f"node/files/{omitted}", members)
        self.assertNotIn("node/files/var/log/linked.log", members)
        self.assertNotIn("node/files/var/log/special.pipe", members)
        self.assertTrue(
            all(members[f"node/files/{relative}"].isreg() for relative in payloads)
        )
        expected_journal_argv = [
            "journalctl",
            "--since",
            cutoff,
            "--no-pager",
            "--utc",
            "--output=short-iso-precise",
        ]
        self.assertEqual(journal_argv, expected_journal_argv)
        self.assertEqual(journal_result["argv"], expected_journal_argv)
        self.assertEqual(journal_result["outcome"], "exited")
        self.assertEqual(journal_result["exit_code"], 0)
        self.assertEqual(journal_stdout, b"journal bytes\x00\xff")

    def test_log_file_failures_are_partial_and_do_not_stop_later_files(self) -> None:
        cutoff_ns = 1_787_313_540_000_000_000
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            source_root = root / "sources"
            log_root = source_root / "var/log"
            log_root.mkdir(parents=True)
            for name in (
                "fail-stat.log",
                "fail-fstat-open.log",
                "fail-open.log",
                "fail-read.log",
                "fail-write.log",
                "raced.log",
                "raced-old.log",
                "later.log",
            ):
                fixture = log_root / name
                fixture.write_bytes(f"{name} bytes".encode("utf-8"))
                os.utime(fixture, ns=(cutoff_ns, cutoff_ns))
            vanished = log_root / "vanished-dir/inside.log"
            vanished.parent.mkdir()
            vanished.write_bytes(b"selected directory evidence")
            os.utime(vanished, ns=(cutoff_ns, cutoff_ns))

            self.write_log_fixture_sitecustomize(root / "sitecustomize.py")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["PYTHONPATH"] = str(root)
            environment["REMOTE_COLLECTOR_FILE_FIXTURES"] = str(source_root)

            cases = (
                (
                    "REMOTE_COLLECTOR_FAIL_LOG_STAT",
                    "/var/log/fail-stat.log",
                    "cannot inspect log source /var/log/fail-stat.log",
                    "fail-stat.log",
                ),
                (
                    "REMOTE_COLLECTOR_FAIL_LOG_READ",
                    "/var/log/fail-read.log",
                    "cannot copy selected file /var/log/fail-read.log",
                    "fail-read.log",
                ),
                (
                    "REMOTE_COLLECTOR_FAIL_LOG_FSTAT",
                    "/var/log/fail-fstat-open.log",
                    "cannot inspect opened selected file "
                    "/var/log/fail-fstat-open.log",
                    "fail-fstat-open.log",
                ),
                (
                    "REMOTE_COLLECTOR_FAIL_LOG_OPEN",
                    "/var/log/fail-open.log",
                    "cannot open selected file /var/log/fail-open.log",
                    "fail-open.log",
                ),
                (
                    "REMOTE_COLLECTOR_FAIL_WRITE_SUFFIX",
                    "node/files/var/log/fail-write.log",
                    "cannot copy selected file /var/log/fail-write.log",
                    "fail-write.log",
                ),
            )
            results = []
            for setting, value, diagnostic, failed_name in cases:
                environment[setting] = value
                completed = self.run_remote_collector(
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    "30",
                    environment=environment,
                )
                archive = root / f"{setting}.tar.gz"
                archive.write_bytes(completed.stdout)
                with tarfile.open(archive, "r:gz") as opened:
                    member_names = {member.name for member in opened.getmembers()}
                results.append((completed, member_names, diagnostic, failed_name))
                environment.pop(setting)

            environment["REMOTE_COLLECTOR_RACE_OPEN"] = "/var/log/raced.log"
            raced = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            raced_archive = root / "raced.tar.gz"
            raced_archive.write_bytes(raced.stdout)
            with tarfile.open(raced_archive, "r:gz") as opened:
                raced_names = {member.name for member in opened.getmembers()}

            environment.pop("REMOTE_COLLECTOR_RACE_OPEN")
            environment["REMOTE_COLLECTOR_RACE_OLD_OPEN"] = (
                "/var/log/raced-old.log"
            )
            raced_old = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            raced_old_archive = root / "raced-old.tar.gz"
            raced_old_archive.write_bytes(raced_old.stdout)
            with tarfile.open(raced_old_archive, "r:gz") as opened:
                raced_old_names = {member.name for member in opened.getmembers()}

            environment.pop("REMOTE_COLLECTOR_RACE_OLD_OPEN")
            environment["REMOTE_COLLECTOR_LOG_DIRECTORY_DISAPPEARS"] = (
                "vanished-dir"
            )
            disappeared = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            disappeared_archive = root / "disappeared-directory.tar.gz"
            disappeared_archive.write_bytes(disappeared.stdout)
            with tarfile.open(disappeared_archive, "r:gz") as opened:
                disappeared_names = {
                    member.name for member in opened.getmembers()
                }

        for completed, member_names, diagnostic, failed_name in results:
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(diagnostic.encode("utf-8"), completed.stderr)
            self.assertNotIn(f"node/files/var/log/{failed_name}", member_names)
            self.assertIn(
                "node/files/var/log/later.log",
                member_names,
                diagnostic,
            )
            self.assertIn("node/probes/journal-system/result.json", member_names)
        self.assertEqual(
            raced.returncode,
            0,
            raced.stderr.decode("utf-8", errors="backslashreplace"),
        )
        self.assertEqual(raced.stderr, b"")
        self.assertNotIn("node/files/var/log/raced.log", raced_names)
        self.assertIn("node/files/var/log/later.log", raced_names)
        self.assertEqual(
            raced_old.returncode,
            0,
            raced_old.stderr.decode("utf-8", errors="backslashreplace"),
        )
        self.assertNotIn("node/files/var/log/raced-old.log", raced_old_names)
        self.assertIn("node/files/var/log/later.log", raced_old_names)
        self.assertNotEqual(disappeared.returncode, 0)
        self.assertIn(
            b"cannot open log directory /var/log/vanished-dir",
            disappeared.stderr,
        )
        self.assertNotIn(
            "node/files/var/log/vanished-dir/inside.log",
            disappeared_names,
        )
        self.assertIn("node/files/var/log/later.log", disappeared_names)

    def test_log_directory_type_races_are_normal_omissions(self) -> None:
        cutoff_ns = 1_787_313_540_000_000_000
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            source_root = root / "sources"
            log_root = source_root / "var/log"
            for name in ("linked-dir", "special-dir"):
                nested = log_root / name / "inside.log"
                nested.parent.mkdir(parents=True)
                nested.write_bytes(b"must not follow replacement")
                os.utime(nested, ns=(cutoff_ns, cutoff_ns))
            later = log_root / "later.log"
            later.write_bytes(b"sibling evidence")
            os.utime(later, ns=(cutoff_ns, cutoff_ns))
            self.write_log_fixture_sitecustomize(root / "sitecustomize.py")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["PYTHONPATH"] = str(root)
            environment["REMOTE_COLLECTOR_FILE_FIXTURES"] = str(source_root)

            results = []
            for setting, name in (
                (
                    "REMOTE_COLLECTOR_LOG_DIRECTORY_BECOMES_LINK",
                    "linked-dir",
                ),
                (
                    "REMOTE_COLLECTOR_LOG_DIRECTORY_BECOMES_SPECIAL",
                    "special-dir",
                ),
            ):
                environment[setting] = name
                completed = self.run_remote_collector(
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    "30",
                    environment=environment,
                )
                archive = root / f"{name}.tar.gz"
                archive.write_bytes(completed.stdout)
                with tarfile.open(archive, "r:gz") as opened:
                    names = {member.name for member in opened.getmembers()}
                results.append((completed, names, name))
                environment.pop(setting)

        for completed, names, replaced_name in results:
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode("utf-8", errors="backslashreplace"),
            )
            self.assertEqual(completed.stderr, b"")
            self.assertFalse(
                any(
                    name.startswith(f"node/files/var/log/{replaced_name}")
                    for name in names
                )
            )
            self.assertIn("node/files/var/log/later.log", names)

    def test_journal_failures_preserve_capture_and_continue_to_log_files(self) -> None:
        cutoff_ns = 1_787_313_540_000_000_000
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            journalctl = fake_bin / "journalctl"
            journalctl.write_text(
                f"""#!{sys.executable}
import os
import time
os.write(1, b"journal partial stdout\\x00\\xff")
os.write(2, b"journal partial stderr\\x00\\xfe")
if os.environ.get("JOURNAL_MODE") == "timeout":
    time.sleep(30)
raise SystemExit(9)
""",
                encoding="utf-8",
            )
            journalctl.chmod(0o755)
            source_root = root / "sources"
            later_log = source_root / "var/log/later.log"
            later_log.parent.mkdir(parents=True)
            later_log.write_bytes(b"log collection continued")
            os.utime(later_log, ns=(cutoff_ns, cutoff_ns))
            self.write_log_fixture_sitecustomize(root / "sitecustomize.py")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["PYTHONPATH"] = str(root)
            environment["REMOTE_COLLECTOR_FILE_FIXTURES"] = str(source_root)

            outcomes = []
            for mode, timeout in (("exit", "30"), ("timeout", "1")):
                environment["JOURNAL_MODE"] = mode
                completed = self.run_remote_collector(
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    timeout,
                    environment=environment,
                )
                archive = root / f"journal-{mode}.tar.gz"
                archive.write_bytes(completed.stdout)
                with tarfile.open(archive, "r:gz") as opened:
                    names = {member.name for member in opened.getmembers()}
                    stdout_file = opened.extractfile(
                        "node/probes/journal-system/stdout"
                    )
                    stderr_file = opened.extractfile(
                        "node/probes/journal-system/stderr"
                    )
                    result_file = opened.extractfile(
                        "node/probes/journal-system/result.json"
                    )
                    assert stdout_file is not None
                    assert stderr_file is not None
                    assert result_file is not None
                    outcomes.append(
                        (
                            completed,
                            names,
                            stdout_file.read(),
                            stderr_file.read(),
                            json.load(result_file),
                        )
                    )

            journalctl.unlink()
            environment.pop("JOURNAL_MODE")
            missing = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            missing_archive = root / "journal-missing.tar.gz"
            missing_archive.write_bytes(missing.stdout)
            with tarfile.open(missing_archive, "r:gz") as opened:
                missing_names = {member.name for member in opened.getmembers()}
                missing_result_file = opened.extractfile(
                    "node/probes/journal-system/result.json"
                )
                assert missing_result_file is not None
                missing_result = json.load(missing_result_file)

        exited, timed_out = outcomes
        for completed, names, stdout, stderr, _ in outcomes:
            self.assertNotEqual(completed.returncode, 0)
            self.assertEqual(stdout, b"journal partial stdout\x00\xff")
            self.assertEqual(stderr, b"journal partial stderr\x00\xfe")
            self.assertIn("node/files/var/log/later.log", names)
            self.assertIn(b"journal-system Probe failed", completed.stderr)
        self.assertEqual(exited[4]["outcome"], "exited")
        self.assertEqual(exited[4]["exit_code"], 9)
        self.assertIsNone(exited[4]["error"])
        self.assertEqual(timed_out[4]["outcome"], "timed_out")
        self.assertIsNone(timed_out[4]["exit_code"])
        self.assertEqual(timed_out[4]["error"]["kind"], "timeout")
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(missing_result["outcome"], "failed_to_start")
        self.assertIsNone(missing_result["exit_code"])
        self.assertIn("node/files/var/log/later.log", missing_names)
        self.assertIn(b"journal-system Probe failed", missing.stderr)

    def test_selected_ceph_source_runs_the_exact_catalog_then_ten_crash_details(
        self,
    ) -> None:
        crash_ids = [f"crash/id-{index}" for index in range(1, 13)]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            self.write_ceph_probe_command(fake_bin / "ceph")
            event_log = root / "ceph-events.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["CEPH_EVENT_LOG"] = str(event_log)
            environment["CEPH_CRASH_IDS"] = json.dumps(crash_ids)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE_COLLECTOR),
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    "30",
                    "--collect-ceph",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                names = {member.name for member in opened.getmembers()}
                observed_results = []
                for probe_name, unused_argv in EXPECTED_CEPH_PROBES:
                    result_file = opened.extractfile(
                        f"ceph/probes/{probe_name}/result.json"
                    )
                    assert result_file is not None
                    observed_results.append(
                        (probe_name, tuple(json.load(result_file)["argv"]))
                    )
                status_stdout_file = opened.extractfile(
                    "ceph/probes/status-json/stdout"
                )
                crash_stdout_file = opened.extractfile(
                    "ceph/probes/crash-info-000001/stdout"
                )
                crash_stderr_file = opened.extractfile(
                    "ceph/probes/crash-info-000001/stderr"
                )
                assert status_stdout_file is not None
                assert crash_stdout_file is not None
                assert crash_stderr_file is not None
                status_stdout = status_stdout_file.read()
                crash_stdout = crash_stdout_file.read()
                crash_stderr = crash_stderr_file.read()
            events = tuple(
                tuple(json.loads(line))
                for line in event_log.read_text(encoding="utf-8").splitlines()
            )

        expected_fixed = tuple(EXPECTED_CEPH_PROBES)
        expected_dynamic = tuple(
            (
                f"crash-info-{index:06d}",
                ("ceph", "crash", "info", crash_id),
            )
            for index, crash_id in enumerate(crash_ids[:10], start=1)
        )
        # Dependent details follow the complete fixed catalog, while their IDs
        # come from the earlier crash-list control capture in response order.
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(observed_results, list(expected_fixed))
        self.assertEqual(
            events,
            tuple(
                argv for unused_name, argv in expected_fixed + expected_dynamic
            ),
        )
        for probe_name, expected_argv in expected_dynamic:
            self.assertIn(f"ceph/probes/{probe_name}/result.json", names)
            self.assertNotIn(expected_argv[-1], "\n".join(names))
        self.assertNotIn("ceph/probes/crash-info-000011", names)
        self.assertEqual(status_stdout, b"not-json-is-still-raw\x00\xff")
        self.assertEqual(crash_stdout, b"crash raw stdout\x00\xff")
        self.assertEqual(crash_stderr, b"crash raw stderr\x00\xfe")

    def test_selected_ceph_source_includes_the_authorized_ceph_shape(self) -> None:
        with TemporaryDirectory() as command_directory:
            fake_bin = Path(command_directory)
            self.write_successful_node_probe_commands(fake_bin)
            self.write_ceph_probe_command(fake_bin / "ceph")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["CEPH_EVENT_LOG"] = str(fake_bin / "ceph-events.jsonl")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE_COLLECTOR),
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    "30",
                    "--collect-ceph",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                names = {member.name for member in opened.getmembers()}

        self.assertEqual(completed.returncode, 0)
        self.assertIn("ceph", names)
        self.assertIn("ceph/probes", names)

    def test_ceph_probe_failure_preserves_raw_bytes_and_later_probes_continue(
        self,
    ) -> None:
        failed_argv = ("ceph", "osd", "dump", "--format", "json-pretty")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            self.write_ceph_probe_command(fake_bin / "ceph")
            event_log = root / "ceph-events.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["CEPH_EVENT_LOG"] = str(event_log)
            environment["CEPH_FAIL_ARGV"] = json.dumps(failed_argv)
            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                "--collect-ceph",
                environment=environment,
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                failed_stdout_file = opened.extractfile(
                    "ceph/probes/osd-dump-json/stdout"
                )
                failed_stderr_file = opened.extractfile(
                    "ceph/probes/osd-dump-json/stderr"
                )
                failed_result_file = opened.extractfile(
                    "ceph/probes/osd-dump-json/result.json"
                )
                later_result_file = opened.extractfile(
                    "ceph/probes/orch-ps-text/result.json"
                )
                assert failed_stdout_file is not None
                assert failed_stderr_file is not None
                assert failed_result_file is not None
                assert later_result_file is not None
                failed_stdout = failed_stdout_file.read()
                failed_stderr = failed_stderr_file.read()
                failed_result = json.load(failed_result_file)
                later_result = json.load(later_result_file)
            events = tuple(
                tuple(json.loads(line))
                for line in event_log.read_text(encoding="utf-8").splitlines()
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            events,
            tuple(argv for unused_name, argv in EXPECTED_CEPH_PROBES),
        )
        self.assertEqual(failed_stdout, b"failed raw stdout\x00\xff")
        self.assertEqual(failed_stderr, b"failed raw stderr\x00\xfe")
        self.assertEqual(failed_result["outcome"], "exited")
        self.assertEqual(failed_result["exit_code"], 9)
        self.assertIsNone(failed_result["error"])
        self.assertEqual(later_result["argv"], ["ceph", "orch", "ps"])
        self.assertIn(b"osd-dump-json Probe failed", completed.stderr)

    def test_malformed_successful_crash_list_is_partial_without_detail_probes(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            self.write_ceph_probe_command(fake_bin / "ceph")
            event_log = root / "ceph-events.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["CEPH_EVENT_LOG"] = str(event_log)
            environment["CEPH_CRASH_LIST_TEXT"] = '{"crash_id":'
            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                "--collect-ceph",
                environment=environment,
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                names = {member.name for member in opened.getmembers()}
                later_result_file = opened.extractfile(
                    "ceph/probes/orch-ps-text/result.json"
                )
                assert later_result_file is not None
                later_result = json.load(later_result_file)
            events = tuple(
                tuple(json.loads(line))
                for line in event_log.read_text(encoding="utf-8").splitlines()
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            events,
            tuple(argv for unused_name, argv in EXPECTED_CEPH_PROBES),
        )
        self.assertFalse(
            any(name.startswith("ceph/probes/crash-info-") for name in names)
        )
        self.assertEqual(later_result["argv"], ["ceph", "orch", "ps"])
        self.assertIn(b"crash-ls-json control parse failed", completed.stderr)

    def test_nonzero_crash_list_does_not_schedule_detail_probes(self) -> None:
        failed_argv = ("ceph", "crash", "ls", "--format", "json-pretty")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            self.write_ceph_probe_command(fake_bin / "ceph")
            event_log = root / "ceph-events.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["CEPH_EVENT_LOG"] = str(event_log)
            environment["CEPH_FAIL_ARGV"] = json.dumps(failed_argv)
            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                "--collect-ceph",
                environment=environment,
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                names = {member.name for member in opened.getmembers()}
                later_result_file = opened.extractfile(
                    "ceph/probes/orch-ps-text/result.json"
                )
                assert later_result_file is not None
                later_result = json.load(later_result_file)
            events = tuple(
                tuple(json.loads(line))
                for line in event_log.read_text(encoding="utf-8").splitlines()
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(
            events,
            tuple(argv for unused_name, argv in EXPECTED_CEPH_PROBES),
        )
        self.assertFalse(
            any(name.startswith("ceph/probes/crash-info-") for name in names)
        )
        self.assertEqual(later_result["argv"], ["ceph", "orch", "ps"])
        self.assertIn(b"crash-ls-json Probe failed", completed.stderr)
        self.assertNotIn(b"control parse failed", completed.stderr)

    def test_malformed_crash_item_after_tenth_rejects_the_whole_control_set(
        self,
    ) -> None:
        response = [{"crash_id": f"crash-{index}"} for index in range(1, 11)]
        response.append({"wrong_field": "must-not-be-ignored"})
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            self.write_ceph_probe_command(fake_bin / "ceph")
            event_log = root / "ceph-events.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["CEPH_EVENT_LOG"] = str(event_log)
            environment["CEPH_CRASH_LIST_TEXT"] = json.dumps(response)
            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                "--collect-ceph",
                environment=environment,
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                names = {member.name for member in opened.getmembers()}

        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(
            any(name.startswith("ceph/probes/crash-info-") for name in names)
        )
        self.assertIn(
            b"item 11 has no nonempty string crash_id",
            completed.stderr,
        )

    def test_unrepresentable_crash_id_is_failed_to_start_and_later_work_continues(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            self.write_ceph_probe_command(fake_bin / "ceph")
            event_log = root / "ceph-events.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["CEPH_EVENT_LOG"] = str(event_log)
            environment["CEPH_CRASH_IDS"] = json.dumps(
                ["cannot\x00be-an-argv", "later-crash"]
            )
            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                "--collect-ceph",
                environment=environment,
            )
            self.assertTrue(completed.stdout, "collector must still stream an archive")
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                failed_result_file = opened.extractfile(
                    "ceph/probes/crash-info-000001/result.json"
                )
                later_result_file = opened.extractfile(
                    "ceph/probes/crash-info-000002/result.json"
                )
                journal_result_file = opened.extractfile(
                    "node/probes/journal-system/result.json"
                )
                assert failed_result_file is not None
                assert later_result_file is not None
                assert journal_result_file is not None
                failed_result = json.load(failed_result_file)
                later_result = json.load(later_result_file)
                journal_result = json.load(journal_result_file)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(failed_result["outcome"], "failed_to_start")
        self.assertIsNone(failed_result["exit_code"])
        self.assertEqual(failed_result["error"]["kind"], "ValueError")
        self.assertEqual(
            later_result["argv"],
            ["ceph", "crash", "info", "later-crash"],
        )
        self.assertEqual(later_result["outcome"], "exited")
        self.assertEqual(journal_result["outcome"], "exited")
        self.assertIn(
            b"crash-info-000001 Probe failed: outcome=failed_to_start",
            completed.stderr,
        )

    def test_noncanonical_or_repeated_remote_controls_are_rejected(self) -> None:
        invalid_arguments = (
            (
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "0",
            ),
            (
                "--since-seconds",
                "01",
                "--probe-timeout-seconds",
                "30",
            ),
            (
                "--since-seconds",
                "+60",
                "--probe-timeout-seconds",
                "30",
            ),
            (
                "--since-seconds",
                "60",
                "--since-seconds",
                "120",
                "--probe-timeout-seconds",
                "30",
            ),
            (
                "--since-s",
                "60",
                "--probe-timeout-seconds",
                "30",
            ),
            (
                "--since-seconds",
                "60",
                "--probe-timeout-s",
                "30",
            ),
            (
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                "--collect-c",
                "--collect-ceph",
            ),
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [sys.executable, str(REMOTE_COLLECTOR), *arguments],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, b"")
                self.assertIn(b"error:", completed.stderr)

    def test_unrepresentable_probe_timeout_fails_before_process_or_workspace(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            process_marker = root / "hostname-started"
            hostname = fake_bin / "hostname"
            hostname.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                f"Path({str(process_marker)!r}).write_bytes(b'started')\n",
                encoding="utf-8",
            )
            hostname.chmod(0o755)
            remote_temp = root / "remote-temp"
            remote_temp.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(remote_temp)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE_COLLECTOR),
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    "9" * 1000,
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            process_started = process_marker.exists()
            residue = list(remote_temp.glob("ceph-incident-node.*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"probe timeout exceeds the supported range", completed.stderr)
        self.assertFalse(process_started)
        self.assertEqual(residue, [])

    def test_failed_hostname_still_streams_capture_and_removes_remote_workspace(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            hostname = fake_bin / "hostname"
            hostname.write_text(
                f"""#!{sys.executable}
import os
os.write(1, b"partial-hostname\\x00\\n")
os.write(2, b"hostname diagnostic\\xff\\n")
raise SystemExit(7)
""",
                encoding="utf-8",
            )
            hostname.chmod(0o755)
            remote_temp = root / "remote-temp"
            remote_temp.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(remote_temp)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE_COLLECTOR),
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    "30",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                stdout_file = opened.extractfile("node/probes/hostname/stdout")
                stderr_file = opened.extractfile("node/probes/hostname/stderr")
                result_file = opened.extractfile("node/probes/hostname/result.json")
                assert stdout_file is not None
                assert stderr_file is not None
                assert result_file is not None
                probe_stdout = stdout_file.read()
                probe_stderr = stderr_file.read()
                result = json.load(result_file)
            residue = list(remote_temp.glob("ceph-incident-node.*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(probe_stdout, b"partial-hostname\x00\n")
        self.assertEqual(probe_stderr, b"hostname diagnostic\xff\n")
        self.assertEqual(result["outcome"], "exited")
        self.assertEqual(result["exit_code"], 7)
        self.assertIn(b"hostname Probe failed", completed.stderr)
        self.assertEqual(residue, [])

    def test_missing_probe_is_captured_with_empty_raw_streams_and_a_concrete_diagnostic(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            hostname = fake_bin / "hostname"
            hostname.write_bytes(b"not executable")
            hostname.chmod(0o644)
            date = fake_bin / "date"
            date.write_text(
                f"""#!{sys.executable}
print("2026-08-21T00:00:00Z")
""",
                encoding="utf-8",
            )
            date.chmod(0o755)
            remote_temp = root / "remote-temp"
            remote_temp.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["TMPDIR"] = str(remote_temp)

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            names, probe_stdout, probe_stderr, result = self.read_hostname_capture(
                completed.stdout, root
            )
            archive = root / "node.tar.gz"
            with tarfile.open(archive, "r:gz") as opened:
                current_utc_stdout_file = opened.extractfile(
                    "node/probes/current-utc/stdout"
                )
                current_utc_result_file = opened.extractfile(
                    "node/probes/current-utc/result.json"
                )
                assert current_utc_stdout_file is not None
                assert current_utc_result_file is not None
                current_utc_stdout = current_utc_stdout_file.read()
                current_utc_result = json.load(current_utc_result_file)
            residue = list(remote_temp.glob("ceph-incident-node.*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(probe_stdout, b"")
        self.assertEqual(probe_stderr, b"")
        self.assertEqual(
            set(result),
            {"argv", "started_at", "finished_at", "outcome", "exit_code", "error"},
        )
        self.assertEqual(result["argv"], ["hostname"])
        self.assertEqual(result["outcome"], "failed_to_start")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["error"]["kind"], "PermissionError")
        self.assertIn("Permission denied", result["error"]["message"])
        self.assertIn(b"PermissionError: ", completed.stderr)
        capture_members = {
            name
            for name in names
            if name == "node/probes/hostname"
            or name.startswith("node/probes/hostname/")
        }
        self.assertEqual(
            capture_members,
            {
                "node/probes/hostname",
                "node/probes/hostname/stdout",
                "node/probes/hostname/stderr",
                "node/probes/hostname/result.json",
            },
        )
        self.assertEqual(current_utc_stdout, b"2026-08-21T00:00:00Z\n")
        self.assertEqual(current_utc_result["outcome"], "exited")
        self.assertEqual(current_utc_result["exit_code"], 0)
        self.assertEqual(residue, [])

    def test_timeout_preserves_partial_streams_and_records_a_timeout_error(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            hostname = fake_bin / "hostname"
            hostname.write_text(
                f"""#!{sys.executable}
import os
import time
os.write(1, b"before-timeout\\x00\\xff")
os.write(2, b"still-running\\x00\\xfe")
time.sleep(30)
""",
                encoding="utf-8",
            )
            hostname.chmod(0o755)
            remote_temp = root / "remote-temp"
            remote_temp.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(remote_temp)

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "1",
                environment=environment,
            )
            _, probe_stdout, probe_stderr, result = self.read_hostname_capture(
                completed.stdout, root
            )
            archive = root / "node.tar.gz"
            with tarfile.open(archive, "r:gz") as opened:
                current_utc_stdout_file = opened.extractfile(
                    "node/probes/current-utc/stdout"
                )
                current_utc_result_file = opened.extractfile(
                    "node/probes/current-utc/result.json"
                )
                assert current_utc_stdout_file is not None
                assert current_utc_result_file is not None
                current_utc_stdout = current_utc_stdout_file.read()
                current_utc_result = json.load(current_utc_result_file)
            residue = list(remote_temp.glob("ceph-incident-node.*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(probe_stdout, b"before-timeout\x00\xff")
        self.assertEqual(probe_stderr, b"still-running\x00\xfe")
        self.assertEqual(result["outcome"], "timed_out")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(
            result["error"],
            {"kind": "timeout", "message": "hostname exceeded 1 seconds"},
        )
        self.assertIn(b"timeout: hostname exceeded 1 seconds", completed.stderr)
        self.assertTrue(current_utc_stdout)
        self.assertEqual(current_utc_result["outcome"], "exited")
        self.assertEqual(current_utc_result["exit_code"], 0)
        self.assertEqual(residue, [])

    def test_failed_probe_does_not_stop_the_next_fixed_probe(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            hostname = fake_bin / "hostname"
            hostname.write_text(
                f"""#!{sys.executable}
import os
os.write(1, b"failed hostname")
raise SystemExit(7)
""",
                encoding="utf-8",
            )
            hostname.chmod(0o755)
            date = fake_bin / "date"
            date.write_text(
                f"""#!{sys.executable}
import os
os.write(1, b"2026-08-21T00:00:00Z\\n")
""",
                encoding="utf-8",
            )
            date.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                hostname_result_file = opened.extractfile(
                    "node/probes/hostname/result.json"
                )
                current_utc_stdout_file = opened.extractfile(
                    "node/probes/current-utc/stdout"
                )
                current_utc_result_file = opened.extractfile(
                    "node/probes/current-utc/result.json"
                )
                assert hostname_result_file is not None
                assert current_utc_stdout_file is not None
                assert current_utc_result_file is not None
                hostname_result = json.load(hostname_result_file)
                current_utc_stdout = current_utc_stdout_file.read()
                current_utc_result = json.load(current_utc_result_file)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(hostname_result["outcome"], "exited")
        self.assertEqual(hostname_result["exit_code"], 7)
        self.assertEqual(current_utc_stdout, b"2026-08-21T00:00:00Z\n")
        self.assertEqual(current_utc_result["outcome"], "exited")
        self.assertEqual(current_utc_result["exit_code"], 0)
        self.assertIn(b"hostname Probe failed", completed.stderr)

    def test_fixed_configuration_files_are_copied_as_raw_regular_bytes(self) -> None:
        payloads = {
            "/etc/os-release": b'NAME="test"\n',
            "/etc/hosts": b"127.0.0.1 localhost\x00\xff\n",
            "/etc/resolv.conf": b"nameserver 192.0.2.53\n",
            "/etc/chrony.conf": b"keyfile /etc/chrony.keys\n",
            "/etc/chrony/chrony.conf": b"pool example.test\n",
            "/etc/systemd/timesyncd.conf": b"[Time]\n",
            "/etc/systemd/timesyncd.conf.d/direct.conf": (
                b"[Time]\nNTP=198.51.100.1\x00credential=secret\xff\n"
            ),
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            source_root = root / "sources"
            for source, payload in payloads.items():
                fixture = source_root / source.removeprefix("/")
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_bytes(payload)
            hard_link_target = source_root / "hard-link-target"
            hard_link_target.write_bytes(payloads["/etc/hosts"])
            hosts_fixture = source_root / "etc/hosts"
            hosts_fixture.unlink()
            os.link(hard_link_target, hosts_fixture)
            drop_in_directory = source_root / "etc/systemd/timesyncd.conf.d"
            (drop_in_directory / "ignored.txt").write_bytes(b"out of selection")
            (drop_in_directory / "nested.conf").mkdir()
            os.mkfifo(drop_in_directory / "special.conf")
            os.symlink("direct.conf", drop_in_directory / "linked.conf")
            sitecustomize = root / "sitecustomize.py"
            sitecustomize.write_text(
                """import os
from pathlib import Path

_source_root = os.environ[\"REMOTE_COLLECTOR_FILE_FIXTURES\"]
_original_open = os.open
_original_stat = os.stat
_original_fdopen = os.fdopen
_original_path_open = Path.open
_fail_lstat = os.environ.get(\"REMOTE_COLLECTOR_FAIL_LSTAT\")
_fail_open = os.environ.get(\"REMOTE_COLLECTOR_FAIL_OPEN\")
_fail_read = os.environ.get(\"REMOTE_COLLECTOR_FAIL_READ\")
_fail_write_suffix = os.environ.get(\"REMOTE_COLLECTOR_FAIL_WRITE_SUFFIX\")
_race_open = os.environ.get(\"REMOTE_COLLECTOR_RACE_OPEN\")
_drop_in_disappears = os.environ.get(
    \"REMOTE_COLLECTOR_DROP_IN_DISAPPEARS_AFTER_SELECTION\"
)
_raced = False
_drop_in_open_count = 0
_read_descriptor = None

def _name(path):
    return os.fspath(path).rsplit(\"/\", 1)[-1]

def stat(path, *args, **kwargs):
    if _fail_lstat and _name(path) == _name(_fail_lstat):
        raise PermissionError(\"fixture inspection failure\")
    return _original_stat(path, *args, **kwargs)

def open(path, flags, *args, **kwargs):
    global _raced, _drop_in_open_count, _read_descriptor
    source = os.fspath(path)
    if source == \"/\" and flags & os.O_DIRECTORY:
        return _original_open(_source_root, flags, *args, **kwargs)
    if _fail_open and _name(source) == _name(_fail_open):
        raise PermissionError(\"fixture open failure\")
    if _race_open and _name(source) == _name(_race_open) and not _raced:
        _raced = True
        fixture = os.path.join(_source_root, _race_open.lstrip(\"/\"))
        target = fixture + \"-after-race\"
        os.rename(fixture, target)
        os.symlink(os.path.basename(target), fixture)
    if _drop_in_disappears and source == \"timesyncd.conf.d\":
        _drop_in_open_count += 1
        if _drop_in_open_count == 2:
            fixture = os.path.join(_source_root, \"etc/systemd/timesyncd.conf.d\")
            os.rename(fixture, fixture + \"-after-selection\")
    descriptor = _original_open(path, flags, *args, **kwargs)
    if _fail_read and _name(source) == _name(_fail_read):
        _read_descriptor = descriptor
    return descriptor

class _BrokenRead:
    def __init__(self, opened):
        self._opened = opened
    def __enter__(self):
        return self
    def __exit__(self, *unused):
        self._opened.close()
    def read(self, *unused):
        raise OSError(\"fixture read failure\")

def fdopen(descriptor, *args, **kwargs):
    opened = _original_fdopen(descriptor, *args, **kwargs)
    if descriptor == _read_descriptor:
        return _BrokenRead(opened)
    return opened

def path_open(self, *args, **kwargs):
    if _fail_write_suffix and str(self).endswith(_fail_write_suffix):
        raise OSError(\"fixture workspace write failure\")
    return _original_path_open(self, *args, **kwargs)

os.stat = stat
os.open = open
os.fdopen = fdopen
Path.open = path_open
""",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["PYTHONPATH"] = str(root)
            environment["REMOTE_COLLECTOR_FILE_FIXTURES"] = str(source_root)

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            self.assertTrue(
                completed.stdout,
                completed.stderr.decode("utf-8", errors="backslashreplace"),
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                members = {member.name: member for member in opened.getmembers()}
                archived_payloads = {}
                copied_members = []
                for source in payloads:
                    member_name = f"node/files/{source.removeprefix('/')}"
                    member = members[member_name]
                    copied_members.append(member)
                    payload = opened.extractfile(member)
                    assert payload is not None
                    archived_payloads[source] = payload.read()

            environment["REMOTE_COLLECTOR_RACE_OPEN"] = "/etc/hosts"
            raced = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            race_archive = root / "race.tar.gz"
            race_archive.write_bytes(raced.stdout)
            with tarfile.open(race_archive, "r:gz") as opened:
                raced_names = {member.name for member in opened.getmembers()}

            hosts_fixture.unlink()
            hosts_fixture.write_bytes(payloads["/etc/hosts"])
            environment.pop("REMOTE_COLLECTOR_RACE_OPEN")
            environment["REMOTE_COLLECTOR_FAIL_OPEN"] = "/etc/hosts"
            failed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            failed_archive = root / "failed.tar.gz"
            failed_archive.write_bytes(failed.stdout)
            with tarfile.open(failed_archive, "r:gz") as opened:
                failed_names = {member.name for member in opened.getmembers()}

            failed_file_cases = (
                ("REMOTE_COLLECTOR_FAIL_LSTAT", "cannot inspect selected file"),
                ("REMOTE_COLLECTOR_FAIL_READ", "cannot copy selected file"),
                (
                    "REMOTE_COLLECTOR_FAIL_WRITE_SUFFIX",
                    "cannot copy selected file",
                ),
            )
            failed_file_results = []
            environment.pop("REMOTE_COLLECTOR_FAIL_OPEN")
            for setting, diagnostic in failed_file_cases:
                value = "/etc/hosts"
                if setting == "REMOTE_COLLECTOR_FAIL_WRITE_SUFFIX":
                    value = "node/files/etc/hosts"
                environment[setting] = value
                failed_case = self.run_remote_collector(
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    "30",
                    environment=environment,
                )
                failed_case_archive = root / f"{setting}.tar.gz"
                failed_case_archive.write_bytes(failed_case.stdout)
                with tarfile.open(failed_case_archive, "r:gz") as opened:
                    failed_case_names = {member.name for member in opened.getmembers()}
                failed_file_results.append(
                    (failed_case, failed_case_names, diagnostic)
                )
                environment.pop(setting)

            environment[
                "REMOTE_COLLECTOR_DROP_IN_DISAPPEARS_AFTER_SELECTION"
            ] = "1"
            selected_drop_in_failed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            selected_drop_in_archive = root / "selected-drop-in-failed.tar.gz"
            selected_drop_in_archive.write_bytes(selected_drop_in_failed.stdout)
            with tarfile.open(selected_drop_in_archive, "r:gz") as opened:
                selected_drop_in_names = {member.name for member in opened.getmembers()}
            os.rename(
                drop_in_directory.with_name("timesyncd.conf.d-after-selection"),
                drop_in_directory,
            )
            environment.pop("REMOTE_COLLECTOR_DROP_IN_DISAPPEARS_AFTER_SELECTION")

            environment["REMOTE_COLLECTOR_RACE_OPEN"] = (
                "/etc/systemd/timesyncd.conf.d"
            )
            drop_in_raced = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            drop_in_race_archive = root / "drop-in-race.tar.gz"
            drop_in_race_archive.write_bytes(drop_in_raced.stdout)
            with tarfile.open(drop_in_race_archive, "r:gz") as opened:
                drop_in_race_names = {member.name for member in opened.getmembers()}

            environment["REMOTE_COLLECTOR_RACE_OPEN"] = "/etc"
            parent_raced = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            parent_race_archive = root / "parent-race.tar.gz"
            parent_race_archive.write_bytes(parent_raced.stdout)
            with tarfile.open(parent_race_archive, "r:gz") as opened:
                parent_race_names = {member.name for member in opened.getmembers()}

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(archived_payloads, payloads)
        self.assertTrue(all(member.isreg() for member in copied_members))
        self.assertNotIn("node/files/etc/ntp.conf", members)
        self.assertNotIn("node/files/etc/systemd/timesyncd.conf.d/ignored.txt", members)
        self.assertNotIn("node/files/etc/systemd/timesyncd.conf.d/linked.conf", members)
        self.assertNotIn("node/files/etc/systemd/timesyncd.conf.d/nested.conf", members)
        self.assertNotIn("node/files/etc/systemd/timesyncd.conf.d/special.conf", members)
        self.assertEqual(raced.returncode, 0)
        self.assertEqual(raced.stderr, b"")
        self.assertNotIn("node/files/etc/hosts", raced_names)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn(b"cannot open selected file /etc/hosts", failed.stderr)
        self.assertNotIn("node/files/etc/hosts", failed_names)
        self.assertIn("node/files/etc/chrony.conf", failed_names)
        for failed_case, failed_case_names, diagnostic in failed_file_results:
            self.assertNotEqual(failed_case.returncode, 0)
            self.assertIn(diagnostic.encode("utf-8"), failed_case.stderr)
            self.assertIn(b"/etc/hosts", failed_case.stderr)
            self.assertNotIn("node/files/etc/hosts", failed_case_names)
            self.assertIn("node/probes/hostname/result.json", failed_case_names)
        self.assertNotEqual(selected_drop_in_failed.returncode, 0)
        self.assertIn(
            b"cannot open selected file /etc/systemd/timesyncd.conf.d/direct.conf",
            selected_drop_in_failed.stderr,
        )
        self.assertNotIn(
            "node/files/etc/systemd/timesyncd.conf.d/direct.conf",
            selected_drop_in_names,
        )
        self.assertEqual(
            drop_in_raced.returncode,
            0,
            drop_in_raced.stderr.decode("utf-8", errors="backslashreplace"),
        )
        self.assertNotIn(
            "node/files/etc/systemd/timesyncd.conf.d/direct.conf",
            drop_in_race_names,
        )
        self.assertEqual(parent_raced.returncode, 0)
        self.assertFalse(
            any(name.startswith("node/files/etc/") for name in parent_race_names)
        )

    def test_node_local_ceph_configuration_is_selected_without_daemon_data(self) -> None:
        included = {
            "etc/ceph/ceph.conf": b"[global]\nfsid = fixture\n",
            "etc/ceph/client.admin.keyring": b"[client.admin]\nkey = secret\x00\xff\n",
            "etc/ceph/nested/arbitrary-name": b"raw nested bytes\x00\xfe",
            "var/lib/ceph/mon/ceph-a/ceph.conf": b"monitor config\n",
            "var/lib/ceph/osd/ceph-0/config": b"osd config\n",
            "var/lib/ceph/osd/ceph-0/override.conf": b"override\n",
            "var/lib/ceph/mgr/ceph-a/module.config": b"module config\n",
        }
        excluded = {
            "var/lib/ceph/mon/ceph-a/keyring": b"must not copy daemon keyring\n",
            "var/lib/ceph/osd/ceph-0/block": b"must not copy block content\n",
            "var/lib/ceph/osd/ceph-0/store.db": b"must not copy daemon database\n",
            "var/lib/ceph/osd/ceph-0/override.conf.bak": b"must not copy backup\n",
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            source_root = root / "sources"
            for relative, payload in {**included, **excluded}.items():
                source = source_root / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(payload)
            os.symlink(
                "ceph.conf",
                source_root / "etc/ceph/linked.conf",
            )
            os.mkfifo(source_root / "etc/ceph/special.keyring")
            os.symlink(
                "ceph.conf",
                source_root / "var/lib/ceph/mon/ceph-a/linked.conf",
            )
            os.mkfifo(source_root / "var/lib/ceph/osd/ceph-0/special.config")
            outside = source_root / "outside"
            outside.mkdir()
            (outside / "escaped.keyring").write_bytes(b"must not traverse")
            os.symlink(outside, source_root / "etc/ceph/linked-directory")
            self.write_source_root_sitecustomize(root / "sitecustomize.py")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["PYTHONPATH"] = str(root)
            environment["REMOTE_COLLECTOR_FILE_FIXTURES"] = str(source_root)

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                names = {member.name for member in opened.getmembers()}
                payloads = {}
                for relative in included:
                    archived = opened.extractfile(f"node/files/{relative}")
                    assert archived is not None
                    payloads[relative] = archived.read()

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="backslashreplace"),
        )
        self.assertEqual(completed.stderr, b"")
        self.assertEqual(payloads, included)
        for relative in excluded:
            self.assertNotIn(f"node/files/{relative}", names)
        self.assertNotIn("node/files/etc/ceph/linked.conf", names)
        self.assertNotIn("node/files/etc/ceph/special.keyring", names)
        self.assertFalse(
            any(
                name.startswith("node/files/etc/ceph/linked-directory")
                for name in names
            )
        )
        self.assertNotIn(
            "node/files/var/lib/ceph/mon/ceph-a/linked.conf",
            names,
        )
        self.assertNotIn(
            "node/files/var/lib/ceph/osd/ceph-0/special.config",
            names,
        )
        self.assertFalse(any("listing" in name.lower() for name in names))

    def test_node_local_ceph_failures_are_partial_and_later_files_continue(self) -> None:
        selected = {
            "etc/ceph/fail-stat.keyring": b"stat failure fixture\n",
            "etc/ceph/fail-read.keyring": b"read failure fixture\n",
            "etc/ceph/fail-write.keyring": b"write failure fixture\n",
            "etc/ceph/raced.keyring": b"race fixture\n",
            "etc/ceph/z-later.keyring": b"later etc evidence\n",
            "var/lib/ceph/mon/ceph-a/fail-open.conf": b"open failure fixture\n",
            "var/lib/ceph/osd/ceph-0/z-later.config": b"later state config\n",
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            source_root = root / "sources"
            for relative, payload in selected.items():
                source = source_root / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(payload)
            self.write_source_root_sitecustomize(root / "sitecustomize.py")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["PYTHONPATH"] = str(root)
            environment["REMOTE_COLLECTOR_FILE_FIXTURES"] = str(source_root)

            cases = (
                (
                    "REMOTE_COLLECTOR_FAIL_CEPH_STAT",
                    "/etc/ceph/fail-stat.keyring",
                    b"cannot inspect node-local Ceph source "
                    b"/etc/ceph/fail-stat.keyring",
                    "etc/ceph/fail-stat.keyring",
                ),
                (
                    "REMOTE_COLLECTOR_FAIL_CEPH_OPEN",
                    "/var/lib/ceph/mon/ceph-a/fail-open.conf",
                    b"cannot open selected file "
                    b"/var/lib/ceph/mon/ceph-a/fail-open.conf",
                    "var/lib/ceph/mon/ceph-a/fail-open.conf",
                ),
                (
                    "REMOTE_COLLECTOR_FAIL_CEPH_READ",
                    "/etc/ceph/fail-read.keyring",
                    b"cannot copy selected file /etc/ceph/fail-read.keyring",
                    "etc/ceph/fail-read.keyring",
                ),
                (
                    "REMOTE_COLLECTOR_FAIL_WRITE_SUFFIX",
                    "node/files/etc/ceph/fail-write.keyring",
                    b"cannot copy selected file /etc/ceph/fail-write.keyring",
                    "etc/ceph/fail-write.keyring",
                ),
            )
            results = []
            for setting, value, diagnostic, omitted in cases:
                environment[setting] = value
                completed = self.run_remote_collector(
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    "30",
                    environment=environment,
                )
                archive = root / f"{setting}.tar.gz"
                archive.write_bytes(completed.stdout)
                with tarfile.open(archive, "r:gz") as opened:
                    names = {member.name for member in opened.getmembers()}
                results.append((completed, names, diagnostic, omitted))
                environment.pop(setting)

            environment["REMOTE_COLLECTOR_RACE_CEPH_OPEN"] = (
                "/etc/ceph/raced.keyring"
            )
            raced = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            raced_archive = root / "raced.tar.gz"
            raced_archive.write_bytes(raced.stdout)
            with tarfile.open(raced_archive, "r:gz") as opened:
                raced_names = {member.name for member in opened.getmembers()}

        for completed, names, diagnostic, omitted in results:
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(diagnostic, completed.stderr)
            self.assertNotIn(f"node/files/{omitted}", names)
            self.assertIn("node/files/etc/ceph/z-later.keyring", names)
            self.assertIn(
                "node/files/var/lib/ceph/osd/ceph-0/z-later.config",
                names,
            )
            self.assertIn("node/probes/journal-system/result.json", names)
        self.assertEqual(
            raced.returncode,
            0,
            raced.stderr.decode("utf-8", errors="backslashreplace"),
        )
        self.assertEqual(raced.stderr, b"")
        self.assertNotIn("node/files/etc/ceph/raced.keyring", raced_names)
        self.assertIn("node/files/etc/ceph/z-later.keyring", raced_names)

    def test_missing_node_local_ceph_roots_are_normal_omissions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            source_root = root / "sources"
            source_root.mkdir()
            self.write_source_root_sitecustomize(root / "sitecustomize.py")
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["PYTHONPATH"] = str(root)
            environment["REMOTE_COLLECTOR_FILE_FIXTURES"] = str(source_root)

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                names = {member.name for member in opened.getmembers()}

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertFalse(
            any(name.startswith("node/files/etc/ceph") for name in names)
        )
        self.assertFalse(
            any(name.startswith("node/files/var/lib/ceph") for name in names)
        )


if __name__ == "__main__":
    unittest.main()
