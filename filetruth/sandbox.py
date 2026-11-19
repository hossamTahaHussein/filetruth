"""Linux-only job runner. No document libraries are imported by the parent."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

MAX_UPLOAD = 12 * 1024 * 1024
MAX_REPORT = 24 * 1024 * 1024
PARSERS = {"auto", "pypdf", "pymupdf", "python-docx"}
LANGUAGES = {"eng", "ara", "eng+ara"}


class Rejected(ValueError):
    """Invalid or unsupported document."""


class Unavailable(RuntimeError):
    """Worker infrastructure or budget failure."""


def command(
    source: Path, parser: str, language: str, capture: Path | None = None
) -> list[str]:
    # unshare creates an empty network namespace with loopback DOWN. Bubblewrap
    # adds a private mount tree, PID/IPC namespaces and disables nested userns.
    args = [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "bwrap",
        "--unshare-user",
        "--disable-userns",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--uid",
        "1000",
        "--gid",
        "1000",
        "--cap-drop",
        "ALL",
        "--new-session",
        "--die-with-parent",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for name in ("bin", "sbin", "lib", "lib64"):
        path = Path("/") / name
        if path.is_symlink():
            args += ["--symlink", os.readlink(path), str(path)]
        elif path.exists():
            args += ["--ro-bind", str(path), str(path)]
    for name in (
        "/etc/fonts",
        "/etc/ld.so.cache",
        "/etc/localtime",
        "/var/cache/fontconfig",
    ):
        if Path(name).exists():
            args += ["--ro-bind", name, name]
    for prefix in sorted(
        {os.path.realpath(sys.prefix), os.path.realpath(sys.base_prefix)}
    ):
        if not Path(prefix).is_relative_to("/usr"):
            args += ["--ro-bind", prefix, prefix]
    # Trusted operator paths only. Never taken from a request.
    office = os.getenv("FILETRUTH_OFFICE_ROOT")
    soffice = "/usr/bin/soffice"
    if office:
        args += ["--ro-bind", str(Path(office).resolve(strict=True)), "/office"]
        soffice = "/office/program/soffice"
    data = os.getenv("FILETRUTH_TESSDATA")
    if data:
        args += [
            "--ro-bind",
            str(Path(data).resolve(strict=True)),
            "/tessdata",
            "--setenv",
            "TESSDATA_PREFIX",
            "/tessdata",
        ]
    if capture is not None:
        args += ["--ro-bind", str(capture), "/input/capture.json"]
    return args + [
        "--ro-bind",
        str(Path(__file__).resolve().parent),
        "/app/filetruth",
        "--ro-bind",
        str(source),
        "/input/document",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        str(160 << 20),
        "--tmpfs",
        "/tmp",
        "--chdir",
        "/tmp",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "PYTHONPATH",
        "/app",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "OMP_THREAD_LIMIT",
        "1",
        "--setenv",
        "OPENBLAS_NUM_THREADS",
        "1",
        "--setenv",
        "SAL_USE_VCLPLUGIN",
        "svp",
        "--",
        "/usr/bin/prlimit",
        "--as=3221225472",
        "--cpu=45",
        "--nofile=128",
        "--nproc=96",
        "--core=0",
        f"--fsize={MAX_REPORT}",
        "--",
        str(Path(sys.prefix).resolve() / "bin" / Path(sys.executable).name),
        "-m",
        "filetruth.worker",
        parser,
        language,
        soffice,
    ]


def container_command(source, capture, parser, language, name):
    args = [
        "prlimit",
        f"--fsize={MAX_REPORT}",
        "--",
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--pull=never",
        "--network",
        "none",
        "--read-only",
        "--log-driver",
        "none",
        "--user",
        "65532:65532",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "96",
        "--memory",
        "1g",
        "--memory-swap",
        "1g",
        "--cpus",
        "1.5",
        "--ulimit",
        "cpu=45:45",
        "--ulimit",
        "nofile=128:128",
        "--ulimit",
        "core=0:0",
        "--ulimit",
        f"fsize={MAX_REPORT}:{MAX_REPORT}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=160m,mode=1777",
        "--mount",
        f"type=bind,src={source},dst=/input/document,readonly",
    ]
    if capture is not None:
        args += ["--mount", f"type=bind,src={capture},dst=/input/capture.json,readonly"]
    return args + ["filetruth-worker:local", parser, language, "/usr/bin/soffice"]


def scan(
    data: bytes,
    name: str,
    parser: str = "auto",
    language: str = "eng+ara",
    *,
    capture: bytes | None = None,
) -> dict:
    if not 0 < len(data) <= MAX_UPLOAD:
        raise Rejected("Upload must contain 1 byte through 12 MiB.")
    if capture is not None and (len(capture) > 2 << 20 or parser != "auto"):
        raise Rejected("Capture limit is 2 MiB and requires the auto parser setting.")
    if parser not in PARSERS or language not in LANGUAGES:
        raise Rejected("Unsupported parser or OCR language.")
    suffix = Path(name).suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise Rejected("Select a PDF or DOCX file.")
    if not data.startswith(b"%PDF-" if suffix == ".pdf" else b"PK\x03\x04"):
        raise Rejected("File signature disagrees with the extension.")
    with tempfile.TemporaryDirectory(prefix="filetruth-") as directory:
        source = Path(directory) / "document"
        source.write_bytes(data)
        captured = None
        if capture is not None:
            captured = Path(directory) / "capture.json"
            captured.write_bytes(capture)
        backend = os.environ.get("FILETRUTH_SANDBOX", "bubblewrap")
        if backend not in {"bubblewrap", "docker"}:
            raise Unavailable("Unknown sandbox backend.")
        container = "filetruth-" + uuid.uuid4().hex if backend == "docker" else None
        if container:
            # Bind individual files into the unprivileged container. Their
            # enclosing host directory remains private (0700).
            source.chmod(0o644)
            if captured:
                captured.chmod(0o644)
            args = container_command(source, captured, parser, language, container)
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                in {
                    "HOME",
                    "PATH",
                    "XDG_RUNTIME_DIR",
                    "DOCKER_HOST",
                    "DOCKER_CONTEXT",
                    "DOCKER_CONFIG",
                }
            }
        else:
            args, env = (
                command(source, parser, language, captured),
                {"PATH": "/usr/bin:/bin"},
            )
        with tempfile.TemporaryFile() as output:
            try:
                proc = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    env=env,
                    close_fds=True,
                )
            except OSError as exc:
                raise Unavailable("Could not start the isolated worker.") from exc
            try:
                proc.wait(timeout=65)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                if container:
                    try:
                        subprocess.run(
                            ["docker", "rm", "-f", container],
                            timeout=10,
                            env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=True,
                        )
                    except (OSError, subprocess.SubprocessError):
                        raise Unavailable(
                            "Docker timeout; worker removal could not be verified."
                        ) from None
                raise Unavailable("Worker deadline exceeded.") from None
            if proc.returncode or output.tell() >= MAX_REPORT:
                raise Unavailable("Sandbox unavailable or worker budget exceeded.")
            output.seek(0)
            try:
                result = json.load(output)
            except (ValueError, RecursionError):
                raise Unavailable("Invalid worker response.") from None
    if "error" in result:
        if result.get("kind") == "rejected":
            raise Rejected(result["error"])
        raise Unavailable(result["error"])
    result["filename"] = Path(name.replace("\\", "/")).name[:160]
    return result
