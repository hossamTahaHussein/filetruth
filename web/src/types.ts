export type Box = [number, number, number, number];
export type Page = {
  number: number;
  width: number;
  height: number;
  preview_jpeg: string;
  ocr: { text: string; quality: number; bbox: Box }[];
  warnings: string[];
};
export type Finding = {
  classification: string;
  code: string;
  detail: string;
  confidence: string;
  page: number | null;
  bbox: Box | null;
  segment: number | null;
  range: [number, number] | null;
  excerpt: string;
  structure?: string;
};
export type Report = {
  filename: string;
  sha256: string;
  format: string;
  parser: { name: string; version: string };
  renderer: string;
  ocr_language: string;
  policy: "Allow" | "Review" | "Block";
  pages: Page[];
  segments: { text: string; page: number | null; source: string }[];
  findings: Finding[];
  reconciled?: {
    status: "unchanged" | "reconciled" | "needs_review";
    text: string;
    removed: { text: string; reason: string; page: number | null }[];
    added: { text: string; source: string; page: number | null }[];
    unresolved: number[];
  };
};
