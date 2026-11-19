import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import type { Box, Finding, Report } from "./types";
import "./style.css";

const classes: Record<string, string> = {
  confirmed: "Confirmed",
  suspected: "Suspected",
  expected_hidden: "Expected hidden",
  unverifiable: "Unverifiable",
};
const codes: Record<string, string> = {
  line_order: "Different token order",
  line_sequence: "Line not found as one sequence",
  unlinked_layer: "Unlocated text layer",
  painted_only: "Painted text missing from parser",
  ocr_gap: "Region not read by OCR",
  visual_only: "Text in the rendered page",
  parser_only: "Text in the parser output",
  searchable_layer: "Matching searchable layer",
  partial_layer: "Partial layer agreement",
  conflicting_layer: "Conflicting text layer",
  unmatched_layer: "Unmatched text layer",
  visual_coverage: "Incomplete visual evidence",
  ambiguous_layer: "Ambiguous parser location",
};
const policyText = {
  Block: "Hold ingestion",
  Review: "Human review required",
  Allow: "Ready for ingestion",
};

function Glyph({ name }: { name: string }) {
  const paths: Record<string, React.ReactNode> = {
    file: (
      <>
        <path d="M5 2h9l5 5v15H5z" />
        <path d="M14 2v6h5M8 13h8M8 17h6" />
      </>
    ),
    upload: (
      <>
        <path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5" />
      </>
    ),
    download: (
      <>
        <path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4" />
      </>
    ),
    layers: (
      <>
        <path d="m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5" />
      </>
    ),
    shield: (
      <>
        <path d="m12 2 8 3v7c0 5-4 8-8 10-4-2-8-5-8-10V5zM9 12l2 2 4-5" />
      </>
    ),
    eye: (
      <>
        <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12" />
        <circle cx="12" cy="12" r="2" />
      </>
    ),
    code: (
      <>
        <path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-14-2 18" />
      </>
    ),
  };
  return (
    <svg
      aria-hidden="true"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {paths[name]}
    </svg>
  );
}

function SelectionText({ text, finding }: { text: string; finding?: Finding }) {
  if (!finding?.range) return <>{text}</>;
  const chars = Array.from(text); // Python JSON ranges use Unicode code points.
  const [a, b] = finding.range;
  return (
    <>
      {chars.slice(0, a).join("")}
      <mark>{chars.slice(a, b).join("")}</mark>
      {chars.slice(b).join("")}
    </>
  );
}

function Workspace({
  r,
  onUpload,
  busy,
  offline,
}: {
  r: Report;
  onUpload: () => void;
  busy: boolean;
  offline: boolean;
}) {
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<number | null>(
    r.findings.length ? 0 : null,
  );
  const [tab, setTab] = useState<"parser" | "ocr" | "clean">("parser");
  const clean = r.reconciled;
  const cleanStatus = {
    unchanged: "Parser text confirmed by the page. Nothing changed.",
    reconciled: "Safe-to-ingest text. Unconfirmed parser text removed, missed visible lines added.",
    needs_review:
      "Not final. Some findings need a person's decision before this text is used.",
  };
  const [marks, setMarks] = useState(true);
  const [zoom, setZoom] = useState(false);
  const p = r.pages[page - 1];
  const active = selected === null ? undefined : r.findings[selected];
  useEffect(() => {
    if (r.findings[0]?.page) setPage(r.findings[0].page);
  }, [r]);
  function select(i: number) {
    setSelected(i);
    const f = r.findings[i];
    if (f.page) setPage(f.page);
    setTab("parser");
    setMarks(true);
  }
  function exportJson() {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(r, null, 2)], { type: "application/json" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = r.filename + ".filetruth.json";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function boxStyle(b: Box) {
    return {
      left: (b[0] / p.width) * 100 + "%",
      top: (b[1] / p.height) * 100 + "%",
      width: ((b[2] - b[0]) / p.width) * 100 + "%",
      height: ((b[3] - b[1]) / p.height) * 100 + "%",
    };
  }
  return (
    <main>
      <div className="intro">
        <div>
          <div className="overline">DOCUMENT INTEGRITY</div>
          <h1>Evidence workspace</h1>
        </div>
        {!offline && (
          <button className="primary" onClick={onUpload} disabled={busy}>
            <Glyph name="upload" /> Inspect document
          </button>
        )}
      </div>
      <section className="record">
        <div className="docIcon">
          <Glyph name="file" />
        </div>
        <div className="docInfo">
          <h2>{r.filename}</h2>
          <small>
            {r.format} <b>·</b> {r.pages.length}{" "}
            {r.pages.length === 1 ? "page" : "pages"} <b>·</b>{" "}
            <span title={r.sha256}>SHA-256 {r.sha256.slice(0, 12)}</span>
          </small>
        </div>
        <div className={"verdict " + r.policy}>
          <span className="dot" />
          <strong>{r.policy}</strong>
          <span>{policyText[r.policy]}</span>
        </div>
        <button className="export" onClick={exportJson}>
          <Glyph name="download" /> JSON
        </button>
      </section>
      <div className="sectionHead">
        <h2>Document comparison</h2>
        <label>
          <input
            type="checkbox"
            checked={marks}
            onChange={(e) => setMarks(e.target.checked)}
          />{" "}
          Show highlights
        </label>
      </div>
      <div className="comparison">
        <section className="pane">
          <div className="paneHead">
            <div>
              <Glyph name="eye" />
              <h3>Rendered page</h3>
            </div>
            <span>{r.renderer}</span>
          </div>
          <div className={"canvas " + (zoom ? "zoom" : "")}>
            <div
              className="paper"
              style={{
                aspectRatio: p.width + "/" + p.height,
                width: zoom
                  ? "100%"
                  : "min(100%, " + (302 * p.width) / p.height + "px)",
              }}
            >
              <img
                src={"data:image/jpeg;base64," + p.preview_jpeg}
                alt={"Rendered document, page " + page}
              />
              {marks &&
                r.findings.map(
                  (f, i) =>
                    f.page === page &&
                    f.bbox && (
                      <button
                        key={i}
                        className={"box " + (selected === i ? "active" : "")}
                        title={codes[f.code]}
                        aria-label={"Inspect finding " + (i + 1)}
                        onClick={() => select(i)}
                        style={boxStyle(f.bbox)}
                      />
                    ),
                )}
            </div>
          </div>
          <div className="paneFoot">
            <span>
              Page {page} of {r.pages.length}
            </span>
            <div>
              <button className="zoomButton" onClick={() => setZoom(!zoom)}>
                {zoom ? "Fit page" : "Fit width"}
              </button>
              <button
                aria-label="Previous page"
                disabled={page === 1}
                onClick={() => {
                  setPage(page - 1);
                  setSelected(null);
                }}
              >
                ‹
              </button>
              <button
                aria-label="Next page"
                disabled={page === r.pages.length}
                onClick={() => {
                  setPage(page + 1);
                  setSelected(null);
                }}
              >
                ›
              </button>
            </div>
          </div>
        </section>
        <section className="pane">
          <div className="paneHead">
            <div>
              <Glyph name="code" />
              <h3>Extraction view</h3>
            </div>
            <span>
              {r.parser.name} {r.parser.version}
            </span>
          </div>
          <div className="tabs">
            <button
              className={tab === "parser" ? "current" : ""}
              onClick={() => setTab("parser")}
            >
              Parser output
            </button>
            <button
              className={tab === "ocr" ? "current" : ""}
              onClick={() => setTab("ocr")}
            >
              Visual OCR
            </button>
            {clean && (
              <button
                className={tab === "clean" ? "current" : ""}
                onClick={() => setTab("clean")}
              >
                Clean text
              </button>
            )}
          </div>
          <div className="transcript">
            {tab === "clean" && clean && (
              <>
                <p>{cleanStatus[clean.status]}</p>
                <div className="line">
                  <span>✓</span>
                  <pre dir="auto">{clean.text || "(no confirmed text)"}</pre>
                </div>
                {clean.removed.map((x, i) => (
                  <div className="line" key={"r" + i}>
                    <span>−</span>
                    <pre dir="auto" title={x.reason}>
                      <del>{x.text}</del>
                      {"\n" + x.reason}
                    </pre>
                  </div>
                ))}
                {clean.added.map((x, i) => (
                  <div className="line" key={"a" + i}>
                    <span>+</span>
                    <pre dir="auto">
                      <ins>{x.text}</ins>
                      {"\nAdded from " + x.source + "."}
                    </pre>
                  </div>
                ))}
              </>
            )}
            {tab === "clean"
              ? null
              : tab === "parser"
              ? r.segments.map(
                  (s, i) =>
                    (s.page === null || s.page === page) && (
                      <div className="line" key={i}>
                        <span>{String(i + 1).padStart(2, "0")}</span>
                        <pre dir="auto" title={s.source}>
                          <SelectionText
                            text={s.text}
                            finding={
                              marks && active?.segment === i
                                ? active
                                : undefined
                            }
                          />
                        </pre>
                      </div>
                    ),
                )
              : p.ocr.map((o, i) => (
                  <div className="line" key={i}>
                    <span>{i + 1}</span>
                    <pre dir="auto">{o.text}</pre>
                  </div>
                ))}
            {tab === "ocr" && !p.ocr.length && (
              <p>No OCR words on this page.</p>
            )}
            {tab === "parser" &&
              !r.segments.some(
                (s) => (s.page === null || s.page === page) && s.text.trim(),
              ) && <p>The parser returned no text.</p>}
          </div>
          {marks && tab === "parser" && active?.code === "visual_only" && (
            <div className="absent">
              <small>Selected visual evidence</small>
              <span dir="auto">{active.excerpt}</span>
            </div>
          )}
          <div className="paneFoot">
            <span>
              {tab === "clean"
                ? "Reconciled text · whole document"
                : tab === "ocr"
                ? "OCR language · " + r.ocr_language
                : r.format === "DOCX"
                  ? "Document-wide paragraph stream"
                  : "Page-scoped parser output"}
            </span>
          </div>
        </section>
      </div>
      <div className="sectionHead">
        <h2>
          Findings <span className="count">{r.findings.length}</span>
        </h2>
      </div>
      <section className="findings">
        {!r.findings.length && (
          <div className="clear">
            <Glyph name="shield" /> No differences found within the supported
            checks.
          </div>
        )}
        {r.findings.map((f, i) => (
          <div
            className={"finding " + (selected === i ? "chosen" : "")}
            key={i}
          >
            <button onClick={() => select(i)} aria-expanded={selected === i}>
              <span className={"badge " + f.classification}>
                {classes[f.classification]}
              </span>
              <strong>{codes[f.code] || f.code}</strong>
              <small>
                {f.page ? "Page " + f.page : "Document"} <b>·</b> {f.confidence}
              </small>
              <span>⌄</span>
            </button>
            {selected === i && (
              <div className="details">
                <p>{f.detail}</p>
                {f.excerpt && <blockquote dir="auto">{f.excerpt}</blockquote>}
                <small>
                  {f.structure && f.structure + " · "}
                  {f.bbox
                    ? "Page box (pt) " + f.bbox.join(", ")
                    : "No verified page coordinates"}
                  {f.range && " · Parser offsets " + f.range.join("–")}
                </small>
              </div>
            )}
          </div>
        ))}
      </section>
    </main>
  );
}

const exampleNames = [
  "handover",
  "hidden-instruction",
  "revised",
  "searchable",
  "bilingual-footer",
];
const exampleLabels: Record<string, string> = {
  "hidden-instruction": "hidden-instruction.pdf (invisible text)",
};
const embedded = document.getElementById("filetruth-examples");
const samples = embedded
  ? (JSON.parse(embedded.textContent || "{}") as Record<string, Report>)
  : null;

function App() {
  const [report, setReport] = useState<Report | null>(
      samples?.handover ?? null,
    ),
    [dialog, setDialog] = useState(false),
    [busy, setBusy] = useState(false),
    [err, setErr] = useState("");
  const [file, setFile] = useState<File | null>(null),
    [parser, setParser] = useState("auto"),
    [language, setLanguage] = useState("eng+ara");
  const [generation, setGeneration] = useState(0);
  const [example, setExample] = useState("handover");
  const ver = useRef(0),
    modal = useRef<HTMLDialogElement>(null);
  async function loadSample(name: string) {
    const request = ++ver.current;
    try {
      const r = samples
        ? samples[name]
        : await fetch("/api/demo?sample=" + encodeURIComponent(name)).then(
            (res) => {
              if (!res.ok) throw Error();
              return res.json() as Promise<Report>;
            },
          );
      if (request === ver.current) {
        setReport(r);
        setExample(name);
        setGeneration((g) => g + 1);
        setErr("");
      }
    } catch {
      if (request === ver.current)
        setErr("The example report could not be loaded.");
    }
  }
  useEffect(() => {
    if (!samples) void loadSample("handover");
  }, []);
  useEffect(() => {
    if (dialog) modal.current?.showModal();
    else modal.current?.close();
  }, [dialog]);
  async function inspect(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    const form = e.currentTarget as HTMLFormElement;
    if (file.size > 12 * 1024 * 1024) {
      setErr("Select a file no larger than 12 MiB.");
      return;
    }
    setBusy(true);
    setErr("");
    ver.current++;
    try {
      const query = new URLSearchParams({
        filename: file.name,
        parser,
        language,
      });
      const res = await fetch("/api/scans?" + query, {
        method: "POST",
        body: file,
        headers: { "Content-Type": "application/octet-stream" },
      });
      const data = await res.json().catch(() => null);
      if (!res.ok || !data)
        throw Error(
          typeof data?.detail === "string"
            ? data.detail
            : "Scan failed (HTTP " + res.status + "). No verification result.",
        );
      setReport(data);
      setExample("");
      setGeneration((n) => n + 1);
      setDialog(false);
      // Clear the native input together with the state, so the same file can
      // be chosen again for a different parser or language.
      form.querySelector<HTMLInputElement>("input[type=file]")!.value = "";
      setFile(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Scan failed.");
    } finally {
      setBusy(false);
    }
  }
  function open() {
    setErr("");
    setDialog(true);
  }
  return (
    <>
      <header>
        <div className="brand">
          <span>
            <Glyph name="layers" />
          </span>
          FileTruth
        </div>
        <span className="headlabel">Integrity console</span>
        <div className="sampleSelect">
          <label htmlFor="sample">Example report</label>
          <select
            id="sample"
            value={example}
            onChange={(e) => void loadSample(e.target.value)}
          >
            {!example && <option value="">Uploaded document</option>}
            {exampleNames.map((n) => (
              <option key={n} value={n}>
                {exampleLabels[n] ??
                  n +
                    (["handover", "bilingual-footer"].includes(n)
                      ? ".docx"
                      : ".pdf")}
              </option>
            ))}
          </select>
        </div>
        <div className="local">
          <i /> {samples ? "Offline demo" : "Local processing"}
        </div>
      </header>
      {report ? (
        <Workspace
          key={generation}
          r={report}
          busy={busy}
          offline={!!samples}
          onUpload={open}
        />
      ) : (
        <main>
          <h1>Evidence workspace</h1>
          <button className="primary" onClick={open}>
            Inspect document
          </button>
          {!dialog && <p>{err}</p>}
        </main>
      )}
      <dialog
        ref={modal}
        onCancel={(e) => {
          if (busy) e.preventDefault();
          else setDialog(false);
        }}
      >
        <form onSubmit={inspect}>
          <h2>Inspect document</h2>
          <label>
            PDF or DOCX · up to 12 MiB
            <input
              type="file"
              accept=".pdf,.docx"
              required
              disabled={busy}
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
          </label>
          <label>
            Ingestion parser
            <select value={parser} onChange={(e) => setParser(e.target.value)}>
              <option value="auto">Default for format</option>
              <option>pypdf</option>
              <option>pymupdf</option>
              <option>python-docx</option>
            </select>
          </label>
          <label>
            OCR language
            <select
              value={language}
              onChange={(e) => setLanguage(e.target.value)}
            >
              <option value="eng+ara">English + Arabic</option>
              <option value="eng">English</option>
              <option value="ara">Arabic</option>
            </select>
          </label>
          {err && <p role="alert">{err}</p>}
          <div className="actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => setDialog(false)}
            >
              Cancel
            </button>
            <button className="primary" disabled={busy || !file}>
              {busy ? "Inspecting…" : "Run inspection"}
            </button>
          </div>
        </form>
      </dialog>
    </>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
