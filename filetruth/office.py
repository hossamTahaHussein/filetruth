"""Bounded passive OOXML subset and a fresh LibreOffice profile."""

import subprocess
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from docx import Document
from lxml import etree

from .sandbox import Rejected, Unavailable

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
DOC_MAIN = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)


def validate(path):
    with ZipFile(path) as archive:
        items = archive.infolist()
        if len(items) > 600 or sum(i.file_size for i in items) > 40 << 20:
            raise Rejected("OOXML package exceeds the inspection budget.")
        names = {i.filename for i in items}
        if (
            len(items) != len(names)
            or not {"[Content_Types].xml", "word/document.xml", "_rels/.rels"} <= names
        ):
            raise Rejected("Invalid OOXML package structure.")
        for item in items:
            name = item.filename
            part = PurePosixPath(name)
            if (
                part.is_absolute()
                or ".." in part.parts
                or "\\" in name
                or item.flag_bits & 1
                or item.file_size > 12 << 20
                or item.file_size > max(1, item.compress_size) * 250
            ):
                raise Rejected("Unsupported compressed or invalid package part.")
            if item.is_dir():
                continue
            if part.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                continue
            if not name.lower().endswith((".rels", ".xml")):
                raise Rejected("Only passive XML, JPEG and PNG parts are supported.")
            xml = etree.fromstring(
                archive.read(item),
                etree.XMLParser(
                    resolve_entities=False,
                    load_dtd=False,
                    no_network=True,
                    huge_tree=False,
                ),
            )
            if name == "word/document.xml" and xml.tag != W + "document":
                raise Rejected("Unexpected main document type.")
            if xml.getroottree().docinfo.doctype:
                raise Rejected("DTD declarations are unsupported.")
            if name == "[Content_Types].xml":
                if not any(
                    el.get("PartName") == "/word/document.xml"
                    and el.get("ContentType") == DOC_MAIN
                    for el in xml
                ):
                    raise Rejected("Standard macro-free DOCX main part required.")
                for el in xml:
                    value = el.get("ContentType", "").lower()
                    if any(
                        s in value
                        for s in ("macro", "vba", "activex", "oleobject", "script")
                    ):
                        raise Rejected("Active package content is unsupported.")
            if name.lower().endswith(".rels"):
                for rel in xml:
                    if rel.get("TargetMode", "").lower() == "external" or any(
                        s in rel.get("Type", "").lower()
                        for s in (
                            "vbaproject",
                            "oleobject",
                            "activex",
                            "attachedtemplate",
                            "afchunk",
                            "script",
                        )
                    ):
                        raise Rejected(
                            "External or active relationships are unsupported."
                        )
            if any(
                el.tag
                in {
                    W + "instrText",
                    W + "fldSimple",
                    W + "altChunk",
                    W + "object",
                    W + "control",
                }
                for el in xml.iter()
            ):
                raise Rejected(
                    "Fields and embedded controls are outside the passive DOCX subset."
                )


def render_docx(path, soffice):
    profile = Path("/tmp/profile/user")
    profile.mkdir(parents=True)
    (profile / "registrymodifications.xcu").write_text(
        '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
        '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
        '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
        '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
        "</item></oor:items>"
    )
    src = Path("/tmp/input.docx")
    src.write_bytes(path.read_bytes())
    pdf = Path("/tmp/input.pdf")
    try:
        job = subprocess.run(
            [
                soffice,
                "-env:UserInstallation=file:///tmp/profile",
                "--headless",
                "--infilter=Office Open XML Text",
                "--norestore",
                "--nodefault",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                "/tmp",
                str(src),
            ],
            timeout=25,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unavailable("LibreOffice is unavailable or timed out.") from exc
    if job.returncode or not pdf.is_file():
        raise Unavailable("LibreOffice could not render this package.")
    return pdf


def paragraphs(path):
    # This adapter deliberately mirrors pipelines using Document(...).paragraphs.
    # No reconstructed PDF text is ever substituted for target-parser output.
    return [p.text for p in Document(path).paragraphs]
