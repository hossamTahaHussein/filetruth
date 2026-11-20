import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from filetruth.api import app
from filetruth.sandbox import Rejected, Unavailable, scan

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"


def verify(name, parser="auto", language="eng", **kwargs):
    return scan((FIX / name).read_bytes(), name, parser, language, **kwargs)


class PipelineTests(unittest.TestCase):
    def test_native_parsers(self):
        for parser in ("pypdf", "pymupdf"):
            with self.subTest(parser=parser):
                r = verify("dispatch.pdf", parser)
                self.assertEqual(r["policy"], "Allow")
                self.assertEqual(r["parser"]["name"], parser)
                self.assertIn("Warehouse delivery", r["segments"][0]["text"])

    def test_searchable(self):
        r = verify("searchable.pdf")
        self.assertEqual(r["policy"], "Allow")
        self.assertEqual(r["findings"][0]["classification"], "expected_hidden")
        self.assertTrue(r["findings"][0]["visual_evidence"])
        self.assertTrue(r["pages"][0]["preview_jpeg"])

    def test_revised(self):
        r = verify("revised.pdf")
        self.assertEqual(r["policy"], "Review")
        self.assertIn("east", r["segments"][0]["text"])
        self.assertIn("west", r["pages"][0]["ocr"][0]["text"])
        self.assertIsNotNone(r["findings"][0]["bbox"])

    def test_docx_footer(self):
        r = verify("handover.docx")
        self.assertEqual(r["policy"], "Block")
        self.assertFalse(any("Retain signed" in s["text"] for s in r["segments"]))
        f = next(f for f in r["findings"] if f["excerpt"] == "Retain signed receipts")
        self.assertEqual(f["classification"], "confirmed")
        self.assertEqual(f["page"], 1)

    def test_bilingual(self):
        r = verify("bilingual-footer.docx", language="eng+ara")
        self.assertIn("محضر الاجتماع", [s["text"] for s in r["segments"]])
        self.assertIn("ara", r["tools"]["ocr_models_available"])
        self.assertTrue(any("السجلات" in f["excerpt"] for f in r["findings"]))

    def test_concealed_text_never_reaches_the_clean_text(self):
        # Same visible invoice every time. Only the concealment technique changes.
        expected = {
            "hidden-instruction.pdf": "unmatched_layer",
            "white-text.pdf": "concealed_text",
            "tiny-text.pdf": "concealed_text",
            "covered-text.pdf": "concealed_text",
            "offpage-text.pdf": "parser_only",
            "hidden-run.docx": "parser_only",
            "white-run.docx": "concealed_text",
            "tag-characters.docx": "invisible_unicode",
        }
        for name, code in expected.items():
            with self.subTest(file=name):
                r = verify(name)
                self.assertEqual(r["policy"], "Review")
                self.assertIn(code, [f["code"] for f in r["findings"]])
                clean = r["reconciled"]
                self.assertEqual(clean["status"], "reconciled")
                self.assertIn("Pay to Muscat Supplies", clean["text"])
                for leaked in ("Ignore", "approve", "50000", "\U000e0061"):
                    self.assertNotIn(leaked, clean["text"])

    def test_ordinary_look_alikes_are_allowed(self):
        for name in ("small-print.pdf", "gray-text.pdf", "shaded-box.pdf"):
            with self.subTest(file=name):
                r = verify(name)
                self.assertEqual(r["findings"], [])
                self.assertEqual(r["policy"], "Allow")
                self.assertEqual(r["reconciled"]["status"], "unchanged")

    def test_capture_and_wrong_digest(self):
        path = FIX / "dispatch.pdf"
        cap = {
            "schema_version": 1,
            "document_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "parser": {"name": "Partner pipeline", "version": "fixture-1"},
            "segments": [
                {"text": "Independent target output", "source": "chunk 3", "page": 1}
            ],
        }
        r = verify(path.name, capture=json.dumps(cap).encode())
        self.assertEqual(r["parser_mode"], "captured")
        self.assertEqual(r["segments"], cap["segments"])
        cap["document_sha256"] = "0" * 64
        with self.assertRaises(Rejected):
            verify(path.name, capture=json.dumps(cap).encode())

    def test_no_parser_fallback(self):
        with self.assertRaises(Rejected):
            verify("dispatch.pdf", "python-docx")

    def test_page_budget(self):
        import pymupdf

        with pymupdf.open() as d:
            for _ in range(11):
                d.new_page()
            with self.assertRaises(Rejected):
                scan(d.tobytes(), "long.pdf")

    def test_cli_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.json"
            p = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "filetruth.cli",
                    "scan",
                    str(FIX / "revised.pdf"),
                    "--gate",
                    "--output",
                    str(out),
                    "--language",
                    "eng",
                ],
                cwd=ROOT,
                timeout=80,
            )
            self.assertEqual(p.returncode, 2)
            self.assertEqual(json.loads(out.read_text())["policy"], "Review")


class ApiTests(unittest.TestCase):
    c = TestClient(app, base_url="http://localhost")
    endpoint = "/api/scans?filename=dispatch.pdf&language=eng"
    headers = {"content-type": "application/octet-stream"}

    def test_api_scan(self):
        res = self.c.post(
            self.endpoint,
            content=(FIX / "dispatch.pdf").read_bytes(),
            headers=self.headers,
        )
        self.assertEqual(res.status_code, 200, res.text[:250])
        self.assertEqual(res.json()["policy"], "Allow")
        self.assertEqual(res.headers["cache-control"], "no-store")

    def test_request_limits(self):
        self.assertEqual(self.c.post(self.endpoint, content=b"bad").status_code, 415)
        self.assertEqual(
            self.c.post(
                self.endpoint, content=b"bad", headers=self.headers
            ).status_code,
            422,
        )
        self.assertEqual(
            self.c.post(
                self.endpoint,
                content=b"x",
                headers={**self.headers, "content-length": "99999999"},
            ).status_code,
            413,
        )

    def test_unavailable_never_allows(self):
        with patch(
            "filetruth.api.scan", side_effect=Unavailable("Sandbox unavailable.")
        ):
            res = self.c.post(self.endpoint, content=b"%PDF-", headers=self.headers)
            self.assertEqual(res.status_code, 503)
            self.assertNotIn("policy", res.json())
