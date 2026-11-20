# FileTruth

FileTruth checks a PDF or DOCX before you ingest it. It compares what a person sees on the rendered page with the text your ingestion parser extracts, and flags text that is hidden from the reader or missed by the parser. It returns a page-linked evidence report, an **Allow**, **Review**, or **Block** decision, and reconciled text that is limited to what the page confirms. The React workspace shows the raster, parser output and OCR side by side. All document processing runs locally.

**Try the example first.** Open [the offline evidence viewer](examples/demo.html) directly in a browser. Select the warehouse handover. The rendered footer says “Retain signed receipts”. The chosen python-docx paragraph pipeline omits the footer. Select the finding to see its region, evidence and policy decision. The example selector also includes an invoice with an invisible text line that only the parser reads, a matching searchable PDF, a stale OCR layer and an Arabic/English handover record. The offline viewer reads packaged reports. The running service accepts new documents.

## Run on Linux

Use Python 3.12+, Node 22.12+ or 24, npm, make, and a Linux host with unprivileged user namespaces and Bubblewrap supporting `--disable-userns` and sized tmpfs mounts. On Debian install the native dependencies below. Use a dedicated runtime environment, since the worker reads system libraries, fonts and the Python installation.

```sh
sudo apt-get update
sudo apt-get install bubblewrap util-linux libreoffice-writer tesseract-ocr \
  tesseract-ocr-eng tesseract-ocr-ara fonts-dejavu-core fonts-liberation fonts-noto-core
python3.12 -m venv .venv
. .venv/bin/activate
make install
make demo
```

Scanning does not run natively on Windows or macOS. Use [Run everything through Docker](#run-everything-through-docker) there. The offline viewer works anywhere.

Visit [the local workspace](http://127.0.0.1:8000). Select **Inspect document**, choose the ingestion parser and OCR languages, and run the inspection. The initial screen contains a labeled example. Each worker handles one document. The service retains no upload history. JSON exports include original parser text and rendered previews.

### Docker worker

A Linux host with a working Docker CLI and a local daemon can use a fresh one-shot container per document. The host runs the CLI, REST API and viewer. The image packages the converters, fonts, OCR models and Python dependencies. The worker receives two individual read-only files at most and has no Docker socket. Prefer a dedicated rootless daemon. A remote daemon cannot use the host's temporary bind paths.

```sh
make worker-image
export FILETRUTH_SANDBOX=docker
make demo
```

The runner supplies network isolation, dropped capabilities, a read-only root, private tmpfs, memory/CPU/PID limits and timeouts. Select the backend explicitly. A failed sandbox returns an error. Local validation covered the native namespace backend. The Docker build and execution are configured in [GitHub Actions](.github/workflows/ci.yml).

### Run everything through Docker

On Windows or macOS with Docker Desktop, a second image plays the Linux host. It runs the API, CLI and tests, and starts one worker container per document through the Docker socket. Build the viewer once with Node (`npm --prefix web ci` and `npm --prefix web run build`), then from the repository root:

```sh
docker build -t filetruth-worker:local .
docker build -t filetruth-runner:local -f Dockerfile.runner .
docker run --rm -p 127.0.0.1:8000:8000 -v "${PWD}:/work" -v /tmp/ftshare:/tmp/ftshare \
  -v /var/run/docker.sock:/var/run/docker.sock \
  filetruth-runner:local python -m filetruth.cli serve --host 0.0.0.0
```

Visit [the local workspace](http://127.0.0.1:8000). Replace the last line's command with `python -m unittest discover -s tests -v`, `python -m scripts.benchmark --examples --output examples/benchmark.json` or `python -m filetruth.cli scan ...` to run those instead. Keep the port published on `127.0.0.1` only: the API has no authentication. Mounting the Docker socket gives the runner container control of the Docker daemon, so use this setup on a personal machine, not a shared host. The `/tmp/ftshare` mount must use the same path on both sides, because the worker binds each document by its host path.

## CLI and REST API

```sh
filetruth scan fixtures/searchable.pdf --parser pypdf --language eng --output report.json
filetruth scan fixtures/handover.docx --language eng --output handover.json --gate
```

With `--gate`, Allow exits 0, Review exits 2 and Block exits 3. An invalid input, failed worker or unavailable sandbox exits 1. Without `--gate`, a completed report exits 0 regardless of policy.

```sh
curl -fsS -H 'Content-Type: application/octet-stream' \
  --data-binary @fixtures/revised.pdf \
  'http://127.0.0.1:8000/api/scans?filename=revised.pdf&parser=pypdf&language=eng' \
  -o revised.json
```

| Interface | Behavior |
|---|---|
| `POST /api/scans` | Raw bounded body. Required query field `filename`. Optional `parser` and `language`. Returns the full report synchronously. |
| `GET /api/demo?sample=handover` | Synthetic saved report. The sample key is any fixture's file name without its extension, for example `hidden-instruction`, `white-text` or `revised`. |
| `GET /api/health` | Service liveness. Run a real fixture to verify converter readiness. |
| `GET /openapi.json` | Machine-readable routes. |
| Errors | 408 upload deadline, 413 size, 415 media type, 422 rejected input, 429 concurrency limit, 503 unavailable worker or exhausted execution budget. |

Treat every non-200 scan response as incomplete verification. Clients should gate ingestion on the returned policy for the matching SHA-256. A report contains evidence for one input, parser configuration and renderer.

## Match your production ingestion path

| Adapter | Exact stream measured |
|---|---|
| `pypdf` | `PdfReader(...).pages[i].extract_text()` in default plain mode, one segment per page. Default for PDFs. |
| `pymupdf` | `page.get_text("text", sort=False)`, one segment per page. |
| `python-docx` | Ordered `Document(...).paragraphs[i].text` from the original DOCX. Tables, headers and footers fall outside that parser call. Default for DOCX. |
| Captured output | Exact text segments from your own chunker, parser or ingestion service, supplied through the CLI or Python API. |

For FileNet, SharePoint, Docling or another RAG connector, capture the strings at the handoff you want to measure. Write a UTF-8 JSON object using the following shape. Replace the digest with the SHA-256 of the input bytes. Keep segment order, whitespace and raw Unicode. Supply either real 1-based page numbers throughout or `null` throughout. A capture is an assertion from the integration that produced it. The digest binds it to the document. It does not authenticate the producer.

```json
{
  "schema_version": 1,
  "document_sha256": "<64 hexadecimal characters>",
  "parser": {"name": "my-ingestion-service", "version": "build-135"},
  "segments": [{"page": 1, "source": "chunk-0", "text": "Exact chunk text"}]
}
```

```sh
filetruth scan original.pdf --capture pipeline-output.json --output verified.json
```

Python integrations can call `filetruth.sandbox.scan(data, filename, capture=capture_bytes)`. Captured input replaces the built-in text extraction stream. Rendering still uses the original document. Overlapping chunks can legitimately generate surplus occurrences. The REST upload interface exposes the built-in adapters.

## Policy and evidence

| Finding | Evidence required in this MVP | Policy |
|---|---|---|
| Confirmed | Independent rendered OCR and spatially matching painted text substantiate an omitted line, or triangulate a conflicting non-painting layer linked to the parser. | Block |
| Suspected | Text the file draws where it cannot be seen (white on white, under a flat shape, off the page, under 4 pt) that the parser still extracts, invisible Unicode tag characters, surplus parser tokens, unsupported local layer text, a sufficiently legible OCR-only difference, or a legible rendered line whose tokens the parser never emits together in that order (values exchanged between lines, or a different reading sequence). | Review |
| Expected hidden | A non-painting searchable span agrees with the OCR text in the same region. | Allow when no higher-priority finding exists |
| Unverifiable | Missing OCR models, unrecognized visible marks, low-confidence OCR surplus, painted text that OCR left unread, pages left unread when the time budget ran out, non-painting text the parser emitted but that cannot be located uniquely or as one run, or inconsistent page geometry. | Review |
| No findings | Supported checks completed with no reported differences. | Allow |

### Reconciled text

Every report also carries `reconciled`: the parser's own wording, limited to what the rendered page confirms. Use it as the text to ingest.

| `reconciled.status` | Meaning |
|---|---|
| `unchanged` | The page confirms the parser text. `text` equals the parser output. |
| `reconciled` | Parser spans with no visible support (hidden layers, unconfirmed text) were removed and are listed in `removed`. Legible lines the parser lacked entirely were added from OCR and are listed in `added`. |
| `needs_review` | A conflict, partial line, order difference or unread region needs a person. `text` is provisional and must not be ingested automatically. |

```sh
filetruth scan fixtures/hidden-instruction.pdf --language eng --output report.json --clean-text clean.txt
```

`--clean-text` writes the file only when the status is not `needs_review`. Added lines are appended after the parser text of their page, because the parser stream has no coordinates to place them. Removal is conservative: visible text that OCR failed to read is removed too, and listed. The viewer shows the same result in the **Clean text** tab.

Allow means the supported checks found nothing. It is not proof of equivalence: the sequence check covers legible ASCII lines of two or more tokens, so reordering of whole lines, single-token lines and right-to-left text can still pass. Text that exists only as pixels depends on OCR alone. FileTruth does not detect malware and does not judge whether content is true or malicious.

Confidence labels describe evidence strength. They are uncalibrated. The engine makes no claim about author intent or prompt injection. It does not rewrite the input, sanitize it or call an LLM.

## Reproduce the evaluation

```sh
make test
make benchmark
make web
python -m scripts.offline_demo
```

The repository includes exact fixture bytes, labels and [per-file results](examples/benchmark.json). To regenerate the synthetic documents, run `python -m scripts.fixtures` and then rerun the commands above. Native tool versions, installed fonts, OCR models and platform can change the results. Optional browser verification uses `python -m pip install playwright`, `python -m playwright install chromium` and `python -m scripts.browser_smoke` with the service running. Set `CHROMIUM_PATH` to use an existing compatible browser.

### Concealment techniques covered

Each row is a fixture with the same visible invoice and one concealed line. All return Review, and the concealed line is removed from the reconciled text.

| Technique | Fixture | Finding |
|---|---|---|
| PDF invisible render mode | `hidden-instruction.pdf` | `unmatched_layer` |
| White text on a white page | `white-text.pdf`, `white-run.docx` | `concealed_text` |
| Text one point tall | `tiny-text.pdf` | `concealed_text` |
| Text covered by a flat shape | `covered-text.pdf` | `concealed_text` |
| Text outside the page | `offpage-text.pdf` | `parser_only` |
| Word hidden-text formatting | `hidden-run.docx` | `parser_only` |
| Invisible Unicode tag characters | `tag-characters.docx` | `invisible_unicode`, with the decoded message |

Three ordinary look-alikes (`small-print.pdf`, `gray-text.pdf`, `shaded-box.pdf`) must return Allow, so the checks are also tested for false alarms. Not covered: text hidden under a photo or textured image, PDF optional-content layers, comments and tracked changes, zero-width characters other than tag characters, and formats other than PDF and DOCX.

OCR often misreads one letter. When positioned painted text sits in the same place and differs from OCR by a single letter in a word of four or more letters, FileTruth treats it as an OCR slip. Anything containing a digit, and any shorter word, must match exactly. The cost of this rule: a one-letter change made by covering real text with an image of near-identical text would not be flagged.

**Measured here:** on the Docker backend, 19 scored synthetic documents gave 12 true positives, 0 false positives, 7 true negatives and 0 false negatives. One additional bilingual PDF was inspected separately after its parser output was examined. The original native-backend run sent the benign bilingual DOCX to Review when OCR missed its Arabic line. This small development corpus cannot estimate production performance, and results move with OCR, font and LibreOffice versions.

License: [GNU AGPL v3](LICENSE).

Created by Hossam Taha · [hossamtaha.me](https://hossamtaha.me)
