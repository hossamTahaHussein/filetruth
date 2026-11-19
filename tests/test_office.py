import io
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from filetruth.office import validate
from filetruth.sandbox import Rejected


class OfficeTests(unittest.TestCase):
    def modified(self, *, extra=None, swap=None):
        source = Path(__file__).resolve().parents[1] / "fixtures/minutes.docx"
        out = io.BytesIO()
        with ZipFile(source) as old, ZipFile(out, "w", ZIP_DEFLATED) as new:
            for name in old.namelist():
                original = old.read(name)
                new.writestr(name, swap[1] if swap and name == swap[0] else original)
            if extra:
                new.writestr(*extra)
        with tempfile.NamedTemporaryFile() as f:
            f.write(out.getvalue())
            f.flush()
            validate(f.name)

    def test_benign(self):
        self.modified()

    def test_opaque_part(self):
        with self.assertRaises(Rejected):
            self.modified(extra=("word/embeddings/object.bin", b"placeholder"))

    def test_macro_label(self):
        with self.assertRaises(Rejected):
            self.modified(extra=("word/vbaProject.bin", b"placeholder"))

    def test_external_relationship(self):
        with self.assertRaises(Rejected):
            self.modified(
                swap=(
                    "_rels/.rels",
                    b'<Relationships><Relationship TargetMode="External"/></Relationships>',
                )
            )

    def test_dtd_and_oversize(self):
        with self.assertRaises(Rejected):
            self.modified(extra=("sample.xml", b"<!DOCTYPE data><data/>"))
        with self.assertRaises(Rejected):
            self.modified(extra=("sample.xml", b"a" * 500000))

    def test_unsafe_names(self):
        with self.assertRaises(Rejected):
            self.modified(extra=("../anything.xml", b"<unused/>"))
