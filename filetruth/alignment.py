"""Compare what a parser extracted with what the rendered page shows.

Three kinds of evidence meet here:

* parser segments: the exact strings the ingestion parser produced
* OCR lines: what Tesseract read from the rendered pixels
* hidden spans: text the PDF stores but never paints

The comparison works on tokens. Every token OCR sees on a page "uses up" one
matching token from the parser. Whatever is left over on either side becomes a
finding. Nothing here guesses at intent, and nothing rewrites the input.
"""

import re
import unicodedata
from collections import defaultdict, deque

TOKENS = re.compile(r"\w+|[^\w\s]", re.UNICODE)

# OCR line quality is Tesseract's lowest word confidence on the line (0-100).
# These thresholds are heuristics. They have not been calibrated on real traffic.
LAYER_QUALITY = 60  # good enough to explain a searchable layer
LEGIBLE_QUALITY = 65  # good enough to suspect a difference
STRONG_QUALITY = 80  # good enough to confirm, or to check word order

# Two boxes are "the same place" when they share this much of the smaller one.
OVERLAP_SHARE = 0.45

# Findings are listed most serious first.
SEVERITY = {"confirmed": 0, "suspected": 1, "unverifiable": 2, "expected_hidden": 3}


def normalize(token):
    """Fold case and compatibility forms so 'ﬁ' matches 'fi' and 'A' matches 'a'."""
    return unicodedata.normalize("NFKC", token).casefold()


def key(text):
    """The normalized tokens of a string, in order. Whitespace is ignored."""
    return tuple(normalize(match.group()) for match in TOKENS.finditer(text))


def overlaps(first, second):
    """Whether two [x0, y0, x1, y1] boxes cover roughly the same place."""
    width = min(first[2], second[2]) - max(first[0], second[0])
    height = min(first[3], second[3]) - max(first[1], second[1])
    shared = max(0, width) * max(0, height)
    smaller = min(
        (first[2] - first[0]) * (first[3] - first[1]),
        (second[2] - second[0]) * (second[3] - second[1]),
    )
    return shared / max(0.01, smaller) >= OVERLAP_SHARE


def occurrences(tokens, positions, wanted):
    """Where the list `wanted` appears as one unbroken run inside `tokens`.

    `positions` maps each token to the places it occurs. Starting from the
    rarest wanted token keeps the search cheap in long parser output.
    """
    if not wanted or any(token not in positions for token in wanted):
        return []
    rarest = min(range(len(wanted)), key=lambda i: len(positions[wanted[i]]))
    return [
        place - rarest
        for place in positions[wanted[rarest]]
        if place >= rarest
        and tokens[place - rarest : place - rarest + len(wanted)] == wanted
    ]


def finding(
    kind,
    code,
    detail,
    *,
    page=None,
    bbox=None,
    segment=None,
    span=None,
    excerpt="",
    confidence="limited",
):
    """One entry of the report's `findings` list."""
    return {
        "classification": kind,
        "code": code,
        "detail": detail,
        "page": page,
        "bbox": bbox,
        "segment": segment,
        "range": span,
        "excerpt": excerpt,
        "confidence": confidence,
    }


class ParserText:
    """The parser's output, indexed for the checks in this module.

    A segment's page is its scope. Segments without a page (DOCX paragraphs,
    page-less captures) belong to the whole document, so every lookup tries the
    page first and the document second.
    """

    def __init__(self, segments):
        self.segments = segments
        # (page, token) -> occurrences that no visible text has claimed yet.
        self.unclaimed = defaultdict(deque)
        # Every token occurrence as [segment, start, end, claimed].
        self.all_occurrences = []
        self.segment_ids = defaultdict(list)
        # Per segment: tokens, their character spans, and token -> positions.
        self.segment_tokens = []
        # Per page: all its tokens in segment order, and token -> positions.
        self.page_streams = defaultdict(lambda: ([], defaultdict(list)))
        for segment_id, segment in enumerate(segments):
            self._index(segment_id, segment)
        # Frozen before any claiming, so "did the parser emit this at all?"
        # keeps a stable answer.
        self.emitted = set(self.unclaimed)

    def _index(self, segment_id, segment):
        page = segment["page"]
        self.segment_ids[page].append(segment_id)
        tokens, spans, positions = [], [], defaultdict(list)
        stream, stream_positions = self.page_streams[page]
        for match in TOKENS.finditer(segment["text"]):
            token = normalize(match.group())
            occurrence = [segment_id, match.start(), match.end(), False]
            self.unclaimed[(page, token)].append(occurrence)
            self.all_occurrences.append(occurrence)
            positions[token].append(len(tokens))
            tokens.append(token)
            spans.append(match.span())
            stream_positions[token].append(len(stream))
            stream.append(token)
        self.segment_tokens.append((tokens, spans, positions))

    def emits(self, page, token):
        """Whether the parser produced this token on the page or document-wide."""
        return (page, token) in self.emitted or (None, token) in self.emitted

    def claim(self, page, text):
        """Match visible text against parser tokens, one occurrence each.

        Returns the visible tokens the parser has no occurrence left for.
        """
        missing = []
        for match in TOKENS.finditer(text):
            token = normalize(match.group())
            waiting = self.unclaimed[(page, token)] or self.unclaimed[(None, token)]
            if waiting:
                waiting.popleft()[3] = True
            else:
                missing.append(match.group())
        return missing

    def locate(self, page, words):
        """Every place `words` appears as one run inside a single segment.

        Returns (segment, start, end) character ranges. Working on tokens means
        the parser's own line breaks and spacing cannot hide a match.
        """
        places = []
        for segment_id in self.segment_ids[page] + self.segment_ids[None]:
            tokens, spans, positions = self.segment_tokens[segment_id]
            for first in occurrences(tokens, positions, words):
                start, end = spans[first][0], spans[first + len(words) - 1][1]
                places.append((segment_id, start, end))
        return places

    def has_run(self, page, words):
        """Whether `words` stay together, in order, somewhere in the page's
        parser output (segments joined in order) or in the document's."""
        return bool(
            occurrences(*self.page_streams[page], words)
            or occurrences(*self.page_streams[None], words)
        )


def layer_agrees(words, nearby_lines):
    """Whether a hidden span says the same thing as the OCR around it.

    A word-level searchable layer is a run inside one OCR line. A paragraph-level
    span equals several OCR lines read top to bottom.
    """
    usable = [line for line in nearby_lines if line["quality"] >= LAYER_QUALITY]
    for line in usable:
        seen = list(key(line["text"]))
        positions = defaultdict(list)
        for place, token in enumerate(seen):
            positions[token].append(place)
        if occurrences(seen, positions, words):
            return True
    top_to_bottom = sorted(usable, key=lambda line: (line["bbox"][1], line["bbox"][0]))
    joined = [token for line in top_to_bottom for token in key(line["text"])]
    return len(usable) > 1 and joined == words


def reordered_lines(segments, pages):
    """Find an OCR line and a parser line made of the same words in another order.

    Deliberately strict: ASCII only, at least three tokens, and exactly one
    parser line with that set of words. It does not try to infer column order,
    and it does not apply a left-to-right reading order to bidirectional text.
    """
    parser_lines = defaultdict(list)
    for segment_id, segment in enumerate(segments):
        for match in re.finditer(r"[^\r\n]+", segment["text"]):
            words = key(match.group())
            if match.group().isascii() and len(words) >= 3:
                same_words = (segment["page"], tuple(sorted(words)))
                parser_lines[same_words].append((segment_id, match.span(), words))
    results = []
    for page in pages:
        for line in page["ocr"]:
            if not line["text"].isascii() or line["quality"] < STRONG_QUALITY:
                continue
            words = key(line["text"])
            same_words = tuple(sorted(words))
            candidates = parser_lines.get(
                (page["number"], same_words), []
            ) + parser_lines.get((None, same_words), [])
            if len(candidates) != 1 or candidates[0][2] == words:
                continue
            segment_id, (start, end), _ = candidates[0]
            item = finding(
                "suspected",
                "line_order",
                "The same tokens appear in a different sequence in this OCR line and parser span.",
                page=page["number"],
                bbox=line["bbox"],
                segment=segment_id,
                span=[start, end],
                excerpt=segments[segment_id]["text"][start:end],
                confidence="indicative",
            )
            item["visual_evidence"] = [line]
            results.append(item)
    return results


def check_hidden_span(parser, page, span):
    """Judge one piece of text the PDF stores but does not paint.

    Returns (finding, covered range). The covered range is the (segment, start,
    end) of parser text this span accounts for, so it is not reported a second
    time as surplus parser text. Either part can be None.
    """
    number = page["number"]
    text, box = span["text"], span["bbox"]
    words = list(key(text))
    if not words:
        return None, None
    places = parser.locate(number, words)
    nearby = [line for line in page["ocr"] if overlaps(line["bbox"], box)]
    agrees = layer_agrees(words, nearby)

    def report(kind, code, detail, confidence, **location):
        item = finding(kind, code, detail, confidence=confidence, **location)
        item["structure"] = span["reason"]
        item["visual_evidence"] = nearby
        return item

    if len(places) != 1:
        # The span cannot be pinned to one place in the parser output.
        emitted = all(parser.emits(number, token) for token in words)
        if not places and not emitted:
            return None, None  # The parser did not emit this span. Nothing to compare.
        here = dict(page=number, bbox=box, excerpt=text)
        if agrees:
            return report(
                "expected_hidden",
                "searchable_layer",
                "Non-painting text matches OCR in the same region.",
                "corroborated",
                **here,
            ), None
        if places:
            return report(
                "unverifiable",
                "ambiguous_layer",
                "Repeated parser text prevents a unique link to this non-painting span.",
                "limited",
                **here,
            ), None
        return report(
            "unverifiable",
            "unlinked_layer",
            "The parser emitted every token of this non-painting span, but not as one run. Its location and local visual agreement could not be established.",
            "limited",
            **here,
        ), None

    segment_id, start, end = places[0]
    here = dict(
        page=number,
        bbox=box,
        segment=segment_id,
        span=[start, end],
        excerpt=parser.segments[segment_id]["text"][start:end],
    )
    covered = (segment_id, start, end)
    if agrees:
        return report(
            "expected_hidden",
            "searchable_layer",
            "Non-painting text matches OCR in the same region.",
            "corroborated",
            **here,
        ), covered

    # A conflict needs strong, independent proof of what is visible there:
    # high-quality OCR that also matches positioned painted text.
    visible_text_differs = any(
        line["quality"] >= STRONG_QUALITY
        and line.get("painted_match")
        and key(line["text"]) != key(text)
        for line in nearby
    )
    some_line_matches = any(key(text) == key(line["text"]) for line in nearby)
    if visible_text_differs and not page["warnings"] and not some_line_matches:
        # Sharing all words in one direction is a segmentation difference
        # between OCR and the span, not a contradiction.
        hidden = set(key(text))
        visible = set(key(" ".join(line["text"] for line in nearby)))
        if hidden <= visible or visible <= hidden:
            return report(
                "suspected",
                "partial_layer",
                "The non-painting span and local OCR only partially align.",
                "limited",
                **here,
            ), covered
        return report(
            "confirmed",
            "conflicting_layer",
            "The parser emitted a non-painting span. High-quality OCR reads different text in its region.",
            "corroborated",
            **here,
        ), covered
    return report(
        "suspected",
        "unmatched_layer",
        "The selected parser emitted non-painting text without a reliable local visual match.",
        "limited",
        **here,
    ), covered


def check_ocr_line(parser, page, line, already_reported):
    """Compare one visible line with the parser output. Returns a finding or None."""
    number = page["number"]
    words = list(key(line["text"]))
    quality = line["quality"]
    missing = parser.claim(number, line["text"])
    if missing:
        # Block-level proof that the parser omits a whole visible line: legible
        # OCR, matching painted text, a clean page, and not one of the line's
        # tokens anywhere in the parser's output for this scope.
        whole_line_omitted = (
            quality >= STRONG_QUALITY
            and line.get("painted_match")
            and not page["warnings"]
            and len(missing) >= 2
            and len(missing) == len(words)
            and not any(parser.emits(number, token) for token in words)
        )
        if whole_line_omitted:
            kind, confidence = "confirmed", "corroborated"
            detail = "PDFium-rendered OCR and positioned, painted MuPDF text agree. The selected parser omits this line."
        else:
            legible = quality >= LEGIBLE_QUALITY
            kind = "suspected" if legible else "unverifiable"
            confidence = "indicative" if legible else "limited"
            detail = "OCR tokens exceed the selected parser's token counts: " + " ".join(
                missing
            )
        item = finding(
            kind,
            "visual_only",
            detail,
            page=number,
            bbox=line["bbox"],
            excerpt=line["text"],
            confidence=confidence,
        )
        item["missing"] = missing
        item["visual_evidence"] = [line]
        return item

    # Every token was found. Counting alone cannot see values exchanged between
    # lines, so a legible line must also exist as one run of the parser output,
    # whatever line breaks the parser chose. Bidirectional text is left out:
    # its visual and logical orders legitimately differ.
    worth_checking = (
        len(words) >= 2
        and quality >= STRONG_QUALITY
        and line["text"].isascii()
        and id(line) not in already_reported
    )
    if worth_checking and not parser.has_run(number, words):
        item = finding(
            "suspected",
            "line_sequence",
            "Every token of this OCR line exists in the parser output, but never together in this order. Values may be exchanged between lines, or the parser reads the layout in a different sequence.",
            page=number,
            bbox=line["bbox"],
            excerpt=line["text"],
            confidence="indicative",
        )
        item["visual_evidence"] = [line]
        return item
    return None


def check_unread_line(parser, page, painted):
    """Report positioned painted text that OCR never read.

    Its pixels were not independently confirmed, so it can never support Allow
    or Block. It still claims parser tokens, which keeps an OCR gap from being
    reported as surplus parser text.
    """
    missing = parser.claim(page["number"], painted["text"])
    if missing:
        kind, code = "suspected", "painted_only"
        detail = (
            "Positioned painted text is absent from the selected parser's output, and OCR returned no line for this region: "
            + " ".join(missing)
        )
    else:
        kind, code = "unverifiable", "ocr_gap"
        detail = "Painted text is positioned here and the parser emits it, but OCR returned no line for this region. Its visibility was not independently confirmed."
    item = finding(
        kind,
        code,
        detail,
        page=page["number"],
        bbox=painted["bbox"],
        excerpt=painted["text"],
    )
    item["painted_source"] = painted
    return item


def surplus_parser_text(parser, covered):
    """Report parser text that nothing visible claimed.

    Neighbouring leftover tokens are merged into one finding per stretch.
    `covered` holds the ranges already explained by hidden-span findings.
    """
    stretches = defaultdict(list)
    for segment_id, start, end, claimed in parser.all_occurrences:
        explained = any(a <= start and end <= b for a, b in covered[segment_id])
        if claimed or explained:
            continue
        text = parser.segments[segment_id]["text"]
        current = stretches[segment_id]
        if current and not text[current[-1][1] : start].strip():
            current[-1][1] = end  # Only whitespace in between: same stretch.
        else:
            current.append([start, end])
    results = []
    for segment_id, ranges in stretches.items():
        segment = parser.segments[segment_id]
        for start, end in ranges:
            results.append(
                finding(
                    "suspected",
                    "parser_only",
                    "No remaining OCR token occurrence aligns with this parser span.",
                    page=segment["page"],
                    segment=segment_id,
                    span=[start, end],
                    excerpt=segment["text"][start:end],
                )
            )
    return results


def compare(segments, pages, hidden_spans):
    """Produce the findings for one document, most serious first.

    `segments` is the parser output, `pages` the rendered pages with their OCR
    lines, and `hidden_spans` maps a page number to its non-painting text.
    """
    findings = reordered_lines(segments, pages)
    already_reported = {id(item["visual_evidence"][0]) for item in findings}
    parser = ParserText(segments)
    covered = defaultdict(list)
    for page in pages:
        number = page["number"]
        for warning in page["warnings"]:
            findings.append(
                finding("unverifiable", "visual_coverage", warning, page=number)
            )
        for span in hidden_spans.get(number, []):
            item, explained = check_hidden_span(parser, page, span)
            if item:
                findings.append(item)
            if explained:
                segment_id, start, end = explained
                covered[segment_id].append((start, end))
        for line in page["ocr"]:
            item = check_ocr_line(parser, page, line, already_reported)
            if item:
                findings.append(item)
        for painted in page.get("unread", []):
            findings.append(check_unread_line(parser, page, painted))
    findings += surplus_parser_text(parser, covered)
    return sorted(findings, key=lambda item: SEVERITY[item["classification"]])


def outcome(findings):
    """The document's policy: the strictest action any finding requires."""
    kinds = {item["classification"] for item in findings}
    if "confirmed" in kinds:
        return "Block"
    if kinds & {"suspected", "unverifiable"}:
        return "Review"
    return "Allow"


# Parser text with no visual support is taken out of the reconciled text.
REMOVAL_REASONS = {
    "unmatched_layer": "Hidden text layer with no matching visible text.",
    "conflicting_layer": "Hidden text layer that conflicts with the visible text.",
    "partial_layer": "Hidden text layer that only partly matches the visible text.",
    "parser_only": "Parser text that no visible text confirms.",
}
# These two are also disagreements about what the page says. A person decides.
CONFLICTS = {"conflicting_layer", "partial_layer"}


def cut_ranges(text, ranges):
    """Remove character ranges from a string. Overlapping ranges are merged."""
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = text[:start] + text[end:]
    return text


def reconcile(segments, findings):
    """Text that is safe to ingest: the parser's wording, limited to what the
    rendered page confirms.

    Parser spans without visual support are removed and listed. A legible line
    the parser lacks entirely is added from OCR and listed. Anything that needs
    a judgement (conflicts, partial lines, order differences, unread regions)
    is left unresolved and sets the status to needs_review. Nothing is guessed.
    """
    cuts = defaultdict(list)
    removed, added, unresolved = [], [], []
    for index, item in enumerate(findings):
        code = item["code"]
        if code == "searchable_layer":
            continue  # Hidden text that matches the page is fine as it is.
        located = item["segment"] is not None and item["range"]
        if code in REMOVAL_REASONS and located:
            cuts[item["segment"]].append(tuple(item["range"]))
            removed.append(
                {
                    "finding": index,
                    "segment": item["segment"],
                    "range": item["range"],
                    "page": item["page"],
                    "text": item["excerpt"],
                    "reason": REMOVAL_REASONS[code],
                }
            )
            if code in CONFLICTS:
                unresolved.append(index)
            continue
        whole_line_missing = len(item.get("missing", ())) == len(key(item["excerpt"]))
        if (
            code == "visual_only"
            and item["classification"] != "unverifiable"
            and whole_line_missing
        ):
            added.append(
                {
                    "finding": index,
                    "page": item["page"],
                    "bbox": item["bbox"],
                    "text": item["excerpt"],
                    "source": "OCR of the rendered page",
                    "quality": item["visual_evidence"][0]["quality"],
                }
            )
            continue
        unresolved.append(index)

    # Put the pieces in reading order. The parser stream has no coordinates, so
    # an added line goes after the parser text of its page (or, for a page-less
    # document, after all parser text).
    paged = any(segment["page"] is not None for segment in segments)
    placed = []
    for segment_id, segment in enumerate(segments):
        text = segment["text"]
        if cuts[segment_id]:
            text = cut_ranges(text, cuts[segment_id]).strip()
            if not text:
                continue
        order = (segment["page"], 0, segment_id) if paged else (0, 0, segment_id)
        entry = {
            "page": segment["page"],
            "source": segment.get("source"),
            "text": text,
        }
        placed.append((order, entry))
    for line in added:
        top = line["bbox"][1]
        order = (line["page"], 1, top) if paged else (1, line["page"], top)
        entry = {"page": line["page"], "source": line["source"], "text": line["text"]}
        placed.append((order, entry))
    clean = [entry for _, entry in sorted(placed, key=lambda pair: pair[0])]

    if unresolved:
        status = "needs_review"
    elif removed or added:
        status = "reconciled"
    else:
        status = "unchanged"
    return {
        "status": status,
        "text": "\n".join(entry["text"] for entry in clean),
        "segments": clean,
        "removed": removed,
        "added": added,
        "unresolved": unresolved,
    }
