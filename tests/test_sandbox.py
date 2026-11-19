import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from filetruth.sandbox import MAX_UPLOAD, Rejected, command, container_command, scan


class IsolationTests(unittest.TestCase):
    @unittest.skipIf(
        os.getenv("FILETRUTH_SANDBOX") == "docker", "Docker backend selected"
    )
    def test_worker_boundary(self):
        # Probe the exact worker prefix, substituting a trusted diagnostic program.
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sentinel"
            p.write_text("test")
            cmd = command(p, "pypdf", "eng")
            diagnostic = """
from pathlib import Path
import os, sys, resource
assert not Path(sys.argv[1]).exists()
assert not Path('/home/agent').exists()
assert Path('/input/document').read_text() == 'test'
assert list(Path('/sys').glob('class/net/*')) == []
routes = Path('/proc/net/route').read_text().splitlines()
assert not [row for row in routes if not row.startswith('Iface')], routes
import errno, socket
with socket.socket() as sock:
    sock.settimeout(1)
    assert sock.connect_ex(('192.0.2.1', 9)) in {errno.ENETUNREACH, errno.EACCES, errno.EPERM}
assert set(line.split(':')[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:]) <= {'lo'}
assert int(next(l.split()[1] for l in Path('/proc/self/status').read_text().splitlines() if l.startswith('CapEff:')),16) == 0
assert resource.getrlimit(resource.RLIMIT_CPU)[0] == 45
assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == 128
assert not os.access('/input/document',os.W_OK)
assert not os.access('/app/filetruth',os.W_OK)
assert Path('/tmp').stat().st_dev != Path('/usr').stat().st_dev
"""
            proc = subprocess.run(
                cmd[: cmd.index("-m")] + ["-c", diagnostic, str(p)],
                capture_output=True,
                timeout=8,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_container_has_required_restrictions(self):
        args = container_command(Path("/source"), None, "pypdf", "eng", "test")
        for opt, value in (
            ("--network", "none"),
            ("--cap-drop", "ALL"),
            ("--pids-limit", "96"),
            ("--user", "65532:65532"),
        ):
            self.assertEqual(args[args.index(opt) + 1], value)
        self.assertIn("--read-only", args)

    def test_size_and_type(self):
        for data, name in [
            (b"", "x.pdf"),
            (b"%PDF-" + b" " * MAX_UPLOAD, "x.pdf"),
            (b"random", "x.pdf"),
            (b"PK", "x.docm"),
        ]:
            with self.subTest(name=name), self.assertRaises(Rejected):
                scan(data, name)
