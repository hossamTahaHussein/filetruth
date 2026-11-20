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

from PIL import ImageOps

PAGE_IMAGE = Path("/tmp/page.png")
NO_WORDS = "Page has visible marks but OCR returned no words."
# A page counts as blank when no pixel is darker than this (0 black, 255 white).
BLANK_PAGE_DARKEST = 220
# Tesseract TSV rows at level 5 are single words.
WORD_LEVEL = "5"
# Page segmentation modes. Full-page layout analysis can drop a whole line,
# most often a short right-to-left one. Reading a cropped region as one raw
# line skips that analysis.
FULL_PAGE = "3"
RAW_LINE = "13"
# Margin around a cropped line: page points kept from the page, then white pixels.
REGION_MARGIN_POINTS = 6
REGION_BORDER_PIXELS = 40


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


def run_tesseract(image, models, timeout, layout=FULL_PAGE):
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
                layout,
                # A crop carries no resolution, so state one for raw-line reads.
                *(("--dpi", "300") if layout == RAW_LINE else ()),
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


def recognize_region(image, size, box, requested, installed, timeout=12):
    """Read one region of the page as a single line.

    `box` is in page points. Returns one line placed at `box`, or None when
    nothing was read. Used for places where the file draws text that the
    full-page pass returned nothing for.
    """
    models = [language for language in requested.split("+") if language in installed]
    if not models or timeout < 1:
        return None
    across, down = image.width / size[0], image.height / size[1]
    crop = image.crop(
        (
            max(0, int((box[0] - REGION_MARGIN_POINTS) * across)),
            max(0, int((box[1] - REGION_MARGIN_POINTS) * down)),
            min(image.width, int((box[2] + REGION_MARGIN_POINTS) * across)),
            min(image.height, int((box[3] + REGION_MARGIN_POINTS) * down)),
        )
    )
    if crop.width < 2 or crop.height < 2:
        return None
    # Tesseract reads text touching the image edge poorly, so add a white frame.
    framed = ImageOps.expand(crop, border=REGION_BORDER_PIXELS, fill="white")
    rows = run_tesseract(framed, models, timeout, layout=RAW_LINE)
    if rows is None or not rows.fieldnames or "text" not in rows.fieldnames:
        return None
    words = [
        (row["text"], float(row["conf"]))
        for row in rows
        if row["level"] == WORD_LEVEL and row["text"].strip()
    ]
    if not words:
        return None
    return {
        "text": " ".join(text for text, _ in words),
        "quality": round(min(confidence for _, confidence in words), 1),
        "bbox": [round(value, 2) for value in box],
        "region_read": True,
    }


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
