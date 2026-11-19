"""Ingest a trusted pipeline's exact, digest-bound text segments."""

import json

from .sandbox import Rejected


def load(path, digest, pages):
    try:
        payload = json.loads(path.read_bytes())
        if payload["document_sha256"] != digest or payload["schema_version"] != 1:
            raise ValueError
        identity = payload["parser"]
        if not all(
            isinstance(identity[k], str) and 0 < len(identity[k]) <= 100
            for k in ("name", "version")
        ):
            raise ValueError
        parts = payload["segments"]
        if not isinstance(parts, list) or len(parts) > 4000:
            raise ValueError
        segments = []
        for part in parts:
            raw, page, source = part["text"], part["page"], part["source"]
            if (
                not isinstance(raw, str)
                or not isinstance(source, str)
                or len(source) > 200
            ):
                raise ValueError
            if page is not None and (type(page) is not int or not 1 <= page <= pages):
                raise ValueError
            segments.append({"text": raw, "page": page, "source": source})
        if any(s["page"] is None for s in segments) and any(
            s["page"] is not None for s in segments
        ):
            raise ValueError
        return {"name": identity["name"], "version": identity["version"]}, segments
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise Rejected(
            "Invalid capture, mixed page scopes or document digest mismatch."
        ) from exc
