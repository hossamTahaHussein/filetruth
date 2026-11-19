"""Read rendered pages with the local Tesseract binary.

Results are lines of text with a box in page points and a quality score. The
score is the lowest word confidence on the line, so one doubtful word makes
the whole line doubtful.
"""

import csv
import io
import subprocess
from collections import defaultdict
from pathlib import Path

PAGE_IMAGE = Path("/tmp/page.png")
NO_WORDS = "Page has visible marks but OCR returned no words."
# A page counts as blank when no pixel is darker than this (0 black, 255 white).
BLANK_PAGE_DARKEST = 220
# Tesseract TSV rows at level 5 are single words.
WORD_LEVEL = "5"


def languages():
    """Language models Tesseract has installed. Empty if it cannot be run."""
    try:
        result = subprocess.run(
            ["tesseract", "--list-langs"],
            capture_output=True,
            text=True,
            timeout=4,
            check=True,
        )
        return set(result.stdout.splitlines()[1:])  # The first line is a heading.
    except (OSError, subprocess.SubprocessError):
        return set()


def run_tesseract(image, models, timeout):
    """OCR one image. Returns the TSV rows, or None if Tesseract failed."""
    image.save(PAGE_IMAGE)
    try:
        result = subprocess.run(
            [
                "tesseract",
                str(PAGE_IMAGE),
                "stdout",
                "-l",
                "+".join(models),
                "--psm",
                "3",  # Fully automatic page layout analysis.
                "tsv",
            ],
            capture_output=True,
            timeout=timeout,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return csv.DictReader(
        io.StringIO(result.stdout.decode("utf-8"), newline=""),
        delimiter="\t",
        quoting=csv.QUOTE_NONE,
    )


def group_into_lines(rows, points_per_pixel):
    """Collect Tesseract's words into lines, with boxes in page points."""
    across, down = points_per_pixel
    words_by_line = defaultdict(list)
    for row in rows:
        if row["level"] != WORD_LEVEL or not row["text"].strip():
            continue
        left, top, width, height = (
            int(row[name]) for name in ("left", "top", "width", "height")
        )
        line_id = (row["block_num"], row["par_num"], row["line_num"])
        words_by_line[line_id].append(
            {
                "text": row["text"],
                "quality": float(row["conf"]),
                "box": [
                    left * across,
                    top * down,
                    (left + width) * across,
                    (top + height) * down,
                ],
            }
        )
    lines = []
    for words in words_by_line.values():
        lefts, tops, rights, bottoms = zip(*(word["box"] for word in words))
        lines.append(
            {
                "text": " ".join(word["text"] for word in words),
                "quality": round(min(word["quality"] for word in words), 1),
                "bbox": [
                    round(min(lefts), 2),
                    round(min(tops), 2),
                    round(max(rights), 2),
                    round(max(bottoms), 2),
                ],
            }
        )
    return lines


def recognize(image, size, requested, installed, timeout=12):
    """Read one page image.

    `size` is the page size in points, `requested` a string such as "eng+ara",
    `installed` the available models. Returns (lines, warnings). A warning means
    the page was not fully read, which later becomes an "unverifiable" finding.
    """
    wanted = requested.split("+")
    models = [language for language in wanted if language in installed]
    warnings = [
        "Missing OCR model: " + language
        for language in wanted
        if language not in installed
    ]
    if not models:
        return [], warnings
    if timeout < 1:
        return [], warnings + ["OCR skipped: the job's time budget was exhausted."]

    rows = run_tesseract(image, models, timeout)
    if rows is None:
        return [], warnings + ["OCR failed or exceeded its deadline."]
    if not rows.fieldnames or not {"level", "text", "conf"} <= set(rows.fieldnames):
        return [], warnings + ["OCR returned no usable TSV output."]

    points_per_pixel = (size[0] / image.width, size[1] / image.height)
    lines = group_into_lines(rows, points_per_pixel)
    darkest_pixel = image.convert("L").getextrema()[0]
    if not lines and darkest_pixel < BLANK_PAGE_DARKEST:
        warnings.append(NO_WORDS)
    return lines, warnings
