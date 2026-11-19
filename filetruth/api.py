import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .sandbox import MAX_UPLOAD, Rejected, Unavailable, scan

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="FileTruth", version="0.1.0", docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
slots = asyncio.Semaphore(2)


@app.middleware("http")
async def headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
        "object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    return response


@app.get("/api/health")
def health():
    return {"service": "FileTruth", "version": "0.1.0"}


@app.get("/api/demo")
def demo(sample: str = "handover"):
    if sample not in {
        "handover",
        "hidden-instruction",
        "revised",
        "searchable",
        "bilingual-footer",
    }:
        raise HTTPException(404, "Unknown example.")
    path = ROOT / "examples" / (sample + ".json")
    if not path.is_file():
        raise HTTPException(404, "Generate the demo report first.")
    return FileResponse(path, media_type="application/json")


@app.post("/api/scans")
async def inspect(
    request: Request, filename: str, parser: str = "auto", language: str = "eng+ara"
):
    if (
        request.headers.get("content-type", "").split(";")[0]
        != "application/octet-stream"
    ):
        raise HTTPException(
            415, "Use application/octet-stream with a raw document body."
        )
    if len(filename) > 200:
        raise HTTPException(422, "Filename too long.")
    length = request.headers.get("content-length")
    if length is not None and (
        not length.isdecimal() or len(length) > 10 or int(length) > MAX_UPLOAD
    ):
        raise HTTPException(413, "Upload exceeds 12 MiB.")
    if slots.locked():
        raise HTTPException(429, "Both scan slots are busy.")
    async with slots:
        data = bytearray()
        try:
            async with asyncio.timeout(30):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > MAX_UPLOAD:
                        raise HTTPException(413, "Upload exceeds 12 MiB.")
                    data.extend(chunk)
            return await run_in_threadpool(
                scan, bytes(data), filename, parser, language
            )
        except TimeoutError:
            raise HTTPException(408, "Upload timed out.") from None
        except Rejected as exc:
            raise HTTPException(422, str(exc)) from None
        except Unavailable as exc:
            raise HTTPException(503, str(exc)) from None


if (ROOT / "web" / "dist").is_dir():
    app.mount(
        "/", StaticFiles(directory=ROOT / "web" / "dist", html=True), name="viewer"
    )
