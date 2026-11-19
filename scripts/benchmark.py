"""Document-level labeled evaluation. Failures remain in the denominator."""

import argparse
import json
from pathlib import Path

from filetruth.sandbox import Rejected, Unavailable, scan

ROOT = Path(__file__).resolve().parents[1]


def ratio(a, b):
    return round(a / b, 4) if b else None


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--output", type=Path)
    cli.add_argument("--examples", action="store_true")
    args = cli.parse_args()
    rows = []
    for item in json.loads((ROOT / "fixtures/labels.json").read_text()):
        path = ROOT / "fixtures" / item["file"]
        row = dict(item)
        try:
            r = scan(path.read_bytes(), path.name, language=item["language"])
            positive = any(
                f["classification"] in {"confirmed", "suspected"} for f in r["findings"]
            )
            row.update(
                policy=r["policy"],
                predicted_mismatch=positive,
                parser=r["parser"],
                classes=[f["classification"] for f in r["findings"]],
                sha256=r["sha256"],
            )
            if args.examples and path.stem in {
                "searchable",
                "revised",
                "handover",
                "bilingual-footer",
                "hidden-instruction",
            }:
                (ROOT / "examples" / (path.stem + ".json")).write_text(
                    json.dumps(r, ensure_ascii=False, indent=2) + "\n"
                )
        except (Rejected, Unavailable) as exc:
            row.update(error=str(exc), predicted_mismatch=None)
        rows.append(row)
    scored = [r for r in rows if r.get("scored", True)]
    tp, fp, tn, fn = [
        sum(r["mismatch"] == gold and r["predicted_mismatch"] is pred for r in scored)
        for gold, pred in [(True, True), (False, True), (False, False), (True, False)]
    ]
    errors = sum(r["predicted_mismatch"] is None for r in scored)
    pos = sum(r["mismatch"] for r in scored)
    result = {
        "unit": "document",
        "size": len(scored),
        "executed": len(rows),
        "exploratory": len(rows) - len(scored),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "failed": errors,
        "precision": ratio(tp, tp + fp),
        "recall_including_failures": ratio(tp, pos),
        "false_positive_rate": ratio(fp, len(scored) - pos),
        "negative_documents_sent_to_review_or_block": sum(
            not r["mismatch"] and r.get("policy") in {"Review", "Block"} for r in scored
        ),
        "coverage": ratio(len(scored) - errors, len(scored)),
        "cases": rows,
    }
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output)
    print(output)
    raise SystemExit(1 if any("error" in r for r in rows) else 0)


if __name__ == "__main__":
    main()
