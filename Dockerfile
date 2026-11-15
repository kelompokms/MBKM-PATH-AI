# PATH-AI — image runtime
#
# Yang sengaja TIDAK ada di image ini:
# - Node.js / Chromium: diagram digambar langsung sebagai vektor di kanvas
#   PDF, bukan dirender lewat mesin browser. Menghemat ±300 MB.
# - Pango / Cairo / GDK-Pixbuf: PDF disusun dengan reportlab, bukan mesin
#   render HTML. Tidak ada lapisan grafis yang perlu dipasang.
# - Database client: layanan ini tidak memakai database.
# - tiktoken: estimasi token cukup heuristik, tidak perlu unduhan model BPE.
#
# Satu-satunya dependensi sistem adalah Tesseract, dan hanya dipakai untuk
# halaman hasil pindai.

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# tesseract + data bahasa Indonesia/Inggris untuk OCR halaman pindaian.
#
# Kedua bahasa diperiksa saat build, bukan saat dipakai. Tanpa pemeriksaan ini,
# paket bahasa yang gagal terpasang baru ketahuan ketika guru mengunggah berkas
# hasil pindai, dan pesannya waktu itu tidak menyebut penyebabnya.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-ind \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && tesseract --version | head -n 1 \
    && tesseract --list-langs | grep -qx 'ind' \
    && tesseract --list-langs | grep -qx 'eng'

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/jobs \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8001

# Satu worker, bukan pilihan gaya: job store dan buku besar anggaran ada di
# memori proses. Dengan lebih dari satu worker, klien yang memantau job bisa
# mendarat di worker lain dan menerima 404.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8001", \
     "--workers", "1", \
     "--proxy-headers", \
     "--no-server-header"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
r=urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=4); \
sys.exit(0 if r.status == 200 else 1)"