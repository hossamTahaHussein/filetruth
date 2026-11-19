FROM python:3.12-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer tesseract-ocr tesseract-ocr-eng tesseract-ocr-ara fonts-dejavu-core \
    fonts-liberation fonts-noto-core && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/filetruth
COPY pyproject.toml LICENSE requirements.lock ./
COPY filetruth ./filetruth
RUN pip install --no-cache-dir -c requirements.lock . && mkdir -p /input && touch /input/document /input/capture.json && rm /input/capture.json
ENV HOME=/tmp TMPDIR=/tmp LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 OMP_THREAD_LIMIT=1 OPENBLAS_NUM_THREADS=1 SAL_USE_VCLPLUGIN=svp
WORKDIR /tmp
USER 65532:65532
ENTRYPOINT ["timeout","--kill-after=2","60","python","-m","filetruth.worker"]
