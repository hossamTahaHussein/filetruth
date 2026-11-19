import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .sandbox import LANGUAGES, MAX_UPLOAD, PARSERS, Rejected, Unavailable, scan


def main():
    cli = argparse.ArgumentParser(
        description="FileTruth document ingestion integrity verification"
    )
    subs = cli.add_subparsers(dest="cmd", required=True)
    verify = subs.add_parser("scan")
    verify.add_argument("document", type=Path)
    verify.add_argument("--parser", choices=sorted(PARSERS), default="auto")
    verify.add_argument("--language", choices=sorted(LANGUAGES), default="eng+ara")
    verify.add_argument("--output", type=Path)
    verify.add_argument(
        "--capture", type=Path, help="Digest-bound JSON from your ingestion pipeline"
    )
    verify.add_argument(
        "--gate", action="store_true", help="Review exits 2. Block exits 3."
    )
    verify.add_argument(
        "--clean-text",
        type=Path,
        help="Write the reconciled text (hidden text removed, missed visible "
        "lines added). Not written when the report still needs review.",
    )
    serve = subs.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address. Change it only inside a container whose port is "
        "published to the host's loopback interface. The API has no authentication.",
    )
    args = cli.parse_args()
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(
            "filetruth.api:app", host=args.host, port=args.port, access_log=False
        )
        return
    try:
        captured = None
        if args.capture:
            with args.capture.open("rb") as stream:
                captured = stream.read((2 << 20) + 1)
        with args.document.open("rb") as f:
            report = scan(
                f.read(MAX_UPLOAD + 1),
                args.document.name,
                args.parser,
                args.language,
                capture=captured,
            )
        body = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            if args.output.resolve() in {
                args.document.resolve(),
                args.capture.resolve() if args.capture else None,
            }:
                raise Rejected("Report output must differ from the input files.")
            with tempfile.NamedTemporaryFile(
                dir=args.output.parent, delete=False, mode="w", encoding="utf-8"
            ) as tmp:
                try:
                    tmp.write(body)
                    tmp.close()
                    os.replace(tmp.name, args.output)
                finally:
                    Path(tmp.name).unlink(missing_ok=True)
        else:
            sys.stdout.write(body)
        if args.clean_text:
            clean = report["reconciled"]
            if clean["status"] == "needs_review":
                sys.stderr.write(
                    "FileTruth: clean text not written. "
                    f"{len(clean['unresolved'])} finding(s) need review.\n"
                )
            else:
                args.clean_text.write_text(clean["text"] + "\n", encoding="utf-8")
        if args.gate:
            raise SystemExit({"Allow": 0, "Review": 2, "Block": 3}[report["policy"]])
    except (OSError, Rejected, Unavailable) as exc:
        cli.exit(1, f"FileTruth: {exc}\n")


if __name__ == "__main__":
    main()
