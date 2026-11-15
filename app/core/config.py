"""PATH-AI — konfigurasi terpusat.

Semua pembacaan environment terjadi di sini dan hanya di sini. Modul lain
mengimpor `settings` dan tidak pernah memanggil `os.environ` langsung, supaya
daftar konfigurasi tetap satu sumber kebenaran.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Identitas ----------------------------------------------------------
    app_name: str = "PATH-AI"

    # --- Kunci akses --------------------------------------------------------
    # Kunci bersama server-ke-server. Wajib ada pada setiap permintaan, dan
    # JANGAN pernah dikirim ke browser. Identitas pengajar menyusul lewat
    # header `X-Teacher-Id` yang diisi backend setelah pengajar login —
    # layanan ini sendiri tidak mengurus akun.
    api_keys: str = Field(..., alias="PATH_AI_API_KEYS")

    # --- OpenRouter ---------------------------------------------------------
    openrouter_api_key: str = Field(..., alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    openrouter_referer: str = Field(
        default="https://path-ai.local", alias="PATH_AI_OPENROUTER_REFERER"
    )
    openrouter_title: str = Field(default="PATH-AI", alias="PATH_AI_OPENROUTER_TITLE")

    # --- Model --------------------------------------------------------------
    # Tanpa nilai bawaan: model yang tidak disengaja tidak boleh masuk produksi.
    model_paid: str = Field(..., alias="PATH_AI_MODEL")
    model_free: str | None = Field(default=None, alias="PATH_AI_MODEL_FREE")
    model_image: str | None = Field(
        default="google/gemini-3.1-flash-lite-image", alias="PATH_AI_MODEL_IMAGE"
    )
    max_illustrations: int = Field(default=3, alias="PATH_AI_MAX_ILLUSTRATIONS")

    # Model penglihatan untuk membaca halaman yang berupa gambar: hasil pindai,
    # foto halaman buku, dan foto lembar jawaban. Kosong berarti jalur ini mati,
    # dan halaman bergambar dibaca memakai OCR lokal (tesseract).
    vision_model: str | None = Field(default=None, alias="PATH_AI_MODEL_VISION")

    # Urutan pemakaian mesin pembaca halaman bergambar:
    #
    #   "local" → OCR lokal lebih dulu, dan model penglihatan hanya dipakai bila
    #             OCR lokal tidak tersedia. Tidak pernah mengeluarkan biaya
    #             model, tetapi memakai CPU server. Tulisan tangan dan foto
    #             miring hasilnya lebih kasar.
    #   "model" → model penglihatan lebih dulu. CPU server tetap lega, tetapi
    #             berbayar per halaman.
    #
    # Bawaannya "local" karena di server, tesseract sudah tersedia di dalam
    # image dan tidak menambah biaya apa pun.
    image_reader_order: str = Field(
        default="local", alias="PATH_AI_IMAGE_READER_ORDER"
    )

    # --- Anggaran -----------------------------------------------------------
    # Batas untuk pengajar dan panel admin.
    budget_tokens_per_day: int = Field(
        default=200_000, alias="PATH_AI_BUDGET_TOKENS_PER_DAY"
    )
    # Batas untuk pelajar, dipisah karena pemakaiannya berbeda bentuk:
    # banyak permintaan kecil, bukan beberapa batch besar.
    student_budget_tokens_per_day: int = Field(
        default=50_000, alias="PATH_AI_STUDENT_BUDGET_TOKENS_PER_DAY"
    )
    # Batas ilustrasi per pelajar per hari. Token bukan pengendali biaya yang
    # baik untuk gambar: satu ilustrasi Recraft hanya 4.175 token tetapi
    # ditagih $0,007, sedangkan 4.175 token teks jauh lebih murah. Tanpa
    # batas terpisah, jatah token yang "cukup" tetap bisa menghasilkan
    # ratusan gambar sehari.
    max_images_per_student_per_day: int = Field(
        default=2, alias="PATH_AI_MAX_IMAGES_PER_STUDENT_PER_DAY"
    )
    max_tokens_per_student: int = Field(
        default=8_000, alias="PATH_AI_MAX_TOKENS_PER_STUDENT"
    )

    # --- Bahan rujukan ------------------------------------------------------
    # Batas teks rujukan yang dikirim ke model untuk fitur 1 dan 2. Nilai ini
    # dulu tertanam di kode sebesar 6.000, dan akibatnya dokumen 39 halaman
    # hanya terbaca seperenam: cukup untuk halaman judul, tidak untuk isinya.
    max_reference_tokens: int = Field(
        default=30_000, alias="PATH_AI_MAX_REFERENCE_TOKENS"
    )

    # --- Ringkasan dokumen panjang ------------------------------------------
    # Dokumen panjang dibaca bertahap: dipotong menjadi beberapa bagian,
    # masing-masing diringkas, lalu ringkasannya digabung menjadi satu telaah.
    doc_chunk_tokens: int = Field(default=6_000, alias="PATH_AI_DOC_CHUNK_TOKENS")
    doc_chunk_overlap_tokens: int = Field(
        default=200, alias="PATH_AI_DOC_CHUNK_OVERLAP_TOKENS"
    )
    # Batas jumlah bagian yang diproses. Sengaja longgar: 30 bagian x 6.000
    # token setara sekitar 175 halaman, sehingga untuk hampir semua berkas yang
    # diunggah guru, yang menentukan bukan batas ini melainkan anggaran token.
    # Batas ini hanya jaring pengaman terakhir; dokumen yang lebih panjang
    # darinya tetap diterima, dan laporannya menyebutkan bagian mana yang
    # dibaca, bukan diam-diam memotong.
    doc_max_chunks: int = Field(default=30, alias="PATH_AI_DOC_MAX_CHUNKS")
    doc_chunk_concurrency: int = Field(
        default=4, alias="PATH_AI_DOC_CHUNK_CONCURRENCY"
    )

    # --- Batas batch & file -------------------------------------------------
    max_students_per_batch: int = Field(
        default=100, alias="PATH_AI_MAX_STUDENTS_PER_BATCH"
    )
    max_file_size_mb: int = Field(default=20, alias="PATH_AI_MAX_FILE_SIZE_MB")
    max_zip_uncompressed_mb: int = Field(
        default=500, alias="PATH_AI_MAX_ZIP_UNCOMPRESSED_MB"
    )
    zip_max_ratio: float = Field(default=100.0, alias="PATH_AI_ZIP_MAX_RATIO")
    max_ocr_pages: int = Field(default=20, alias="PATH_AI_MAX_OCR_PAGES")
    max_answer_key_pages: int = Field(
        default=20, alias="PATH_AI_MAX_ANSWER_KEY_PAGES"
    )

    # --- Ambang label -------------------------------------------------------
    label_sangat_baik_min: float = Field(
        default=86.0, alias="PATH_AI_LABEL_SANGAT_BAIK_MIN"
    )
    label_baik_min: float = Field(default=71.0, alias="PATH_AI_LABEL_BAIK_MIN")
    label_cukup_min: float = Field(default=56.0, alias="PATH_AI_LABEL_CUKUP_MIN")

    # --- Kemiripan ----------------------------------------------------------
    sim_llm_low: float = Field(default=0.25, alias="PATH_AI_SIM_LLM_LOW")
    sim_llm_high: float = Field(default=0.75, alias="PATH_AI_SIM_LLM_HIGH")
    sim_llm_max_pairs: int = Field(default=3, alias="PATH_AI_SIM_LLM_MAX_PAIRS")
    sim_shingle_size: int = Field(default=5, alias="PATH_AI_SIM_SHINGLE_SIZE")

    # --- Indikasi AI --------------------------------------------------------
    ai_low: float = Field(default=35.0, alias="PATH_AI_AI_LOW")
    ai_high: float = Field(default=65.0, alias="PATH_AI_AI_HIGH")

    # --- Job ----------------------------------------------------------------
    job_dir: str = Field(default="", alias="PATH_AI_JOB_DIR")
    job_ttl_minutes: int = Field(default=60, alias="PATH_AI_JOB_TTL_MINUTES")
    max_concurrent_jobs: int = Field(default=2, alias="PATH_AI_MAX_CONCURRENT_JOBS")

    # Kunci jawaban yang sudah diekstraksi disimpan sementara agar guru bisa
    # memeriksa sebelum dipakai menilai.
    answer_key_ttl_minutes: int = Field(
        default=30, alias="PATH_AI_ANSWER_KEY_TTL_MINUTES"
    )
    grade_concurrency: int = Field(default=4, alias="PATH_AI_GRADE_CONCURRENCY")

    # --- Jaringan -----------------------------------------------------------
    request_timeout: float = Field(default=120.0, alias="PATH_AI_REQUEST_TIMEOUT")
    max_retries: int = Field(default=2, alias="PATH_AI_MAX_RETRIES")
    free_rate_per_minute: int = Field(
        default=20, alias="PATH_AI_FREE_RATE_PER_MINUTE"
    )
    max_request_mb: int = Field(default=50, alias="PATH_AI_MAX_REQUEST_MB")
    cors_origins: str = Field(default="", alias="PATH_AI_CORS_ORIGINS")

    # --- OCR ----------------------------------------------------------------
    tesseract_cmd: str | None = Field(default=None, alias="TESSERACT_CMD")

    # --- Logging ------------------------------------------------------------
    log_level: str = Field(default="INFO", alias="PATH_AI_LOG_LEVEL")

    # --- Turunan ------------------------------------------------------------
    @field_validator("openrouter_base_url")
    @classmethod
    def _strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator(
        "tesseract_cmd",
        "model_free",
        "model_image",
        "vision_model",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        keys = self.api_key_list
        if not keys:
            raise ValueError("PATH_AI_API_KEYS tidak boleh kosong")

        if len({k for k in keys}) != len(keys):
            raise ValueError("PATH_AI_API_KEYS mengandung kunci duplikat")

        # Label harus menurun tajam, kalau tidak urutan band jadi ambigu.
        if not (
            self.label_sangat_baik_min
            > self.label_baik_min
            > self.label_cukup_min
            >= 0
        ):
            raise ValueError(
                "Ambang label harus menurun: sangat_baik > baik > cukup >= 0"
            )

        if not 0 <= self.sim_llm_low < self.sim_llm_high <= 1:
            raise ValueError("PATH_AI_SIM_LLM_LOW harus < PATH_AI_SIM_LLM_HIGH (0..1)")

        if not 0 <= self.ai_low < self.ai_high <= 100:
            raise ValueError("PATH_AI_AI_LOW harus < PATH_AI_AI_HIGH (0..100)")

        if self.model_free and self.model_free == self.model_paid:
            raise ValueError(
                "PATH_AI_MODEL_FREE harus berbeda dari PATH_AI_MODEL, "
                "atau dikosongkan untuk menonaktifkan jalur gratis"
            )

        if self.sim_shingle_size < 1:
            raise ValueError("PATH_AI_SIM_SHINGLE_SIZE minimal 1")

        if self.image_reader_order not in {"local", "model"}:
            raise ValueError(
                "PATH_AI_IMAGE_READER_ORDER harus 'local' atau 'model'"
            )

        return self

    @model_validator(mode="after")
    def _check_bahan_rujukan(self) -> Settings:
        """Periksa pengaturan pembacaan berkas dan ringkasan dokumen.

        Dipisah dari pemeriksaan di atas bukan demi baris, melainkan supaya
        satu validator tidak mencampur pengaturan penilaian dengan pengaturan
        pembacaan berkas yang tidak berhubungan.
        """
        if self.max_reference_tokens < 1_000:
            raise ValueError("PATH_AI_MAX_REFERENCE_TOKENS minimal 1000")

        if self.doc_chunk_tokens < 500:
            raise ValueError("PATH_AI_DOC_CHUNK_TOKENS minimal 500")

        if self.doc_chunk_overlap_tokens >= self.doc_chunk_tokens:
            raise ValueError(
                "PATH_AI_DOC_CHUNK_OVERLAP_TOKENS harus lebih kecil dari "
                "PATH_AI_DOC_CHUNK_TOKENS"
            )

        if self.doc_max_chunks < 1:
            raise ValueError("PATH_AI_DOC_MAX_CHUNKS minimal 1")

        if self.doc_chunk_concurrency < 1:
            raise ValueError("PATH_AI_DOC_CHUNK_CONCURRENCY minimal 1")

        return self

    @property
    def api_key_list(self) -> list[str]:
        return [k.strip() for k in self.api_keys.split(",") if k.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        """Daftar origin yang diizinkan. Kosong berarti CORS dimatikan."""
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def job_root(self) -> Path:
        return Path(self.job_dir) if self.job_dir.strip() else BASE_DIR / "jobs"

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def max_zip_uncompressed_bytes(self) -> int:
        return self.max_zip_uncompressed_mb * 1024 * 1024

    @property
    def max_request_bytes(self) -> int:
        return self.max_request_mb * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Baca konfigurasi sekali saja, lalu simpan untuk seluruh proses."""
    # Nilainya datang dari environment, bukan dari argumen, sehingga
    # pemeriksa tipe menganggap argumen wajibnya tidak terisi.
    return Settings()


settings = get_settings()