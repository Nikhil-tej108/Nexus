# syntax=docker/dockerfile:1

# ---- Stage 1: build the dependency virtualenv -------------------------------
# Compilers and headers live here only; they never reach the final image.
FROM python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Cached independently of the source tree: only requirements.txt busts it.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Stage 2: runtime -------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# Runtime-only native libs: OCR engine plus the shared objects PyMuPDF/Pillow need.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 nexus \
    && useradd -m -u 1000 -g 1000 nexus

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY . .
RUN mkdir -p /data/uploads && chown -R nexus:nexus /app /data/uploads

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
