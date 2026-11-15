# PATH-AI

Layanan AI untuk pengajar. Lima fitur, semuanya berbahasa Indonesia:

| # | Fitur | Endpoint | Keluaran |
|---|-------|----------|----------|
| 1 | Tanya Materi dengan AI | `POST /v1/materials/explain` | PDF materi |
| 2 | Membuat Materi dengan AI | `POST /v1/materials/generate` | PDF materi |
| 3 | Koreksi Jawaban Siswa dengan AI | `POST /v1/answer-keys/extract` → `POST /v1/assessments/grade` | Excel + JSON |
| 4 | Pemeriksaan Kemiripan Jawaban Antarsiswa | `POST /v1/assessments/similarity` | Excel + JSON |
| 5 | Pemeriksaan Penggunaan AI pada Jawaban Siswa | `POST /v1/assessments/ai-usage` | PDF + JSON |

Dokumentasi interaktif: `http://localhost:8001/docs`

---

## Menjalankan

### Docker (cara yang disarankan)

Windows:

```powershell
Copy-Item .env.example .env   # lalu isi nilainya
.\start.ps1                   # build + jalankan di port 8001
.\stop.ps1                    # hentikan
.\stop.ps1 -Remove            # hentikan + hapus image
```

Linux atau macOS:

```bash
cp .env.example .env          # lalu isi nilainya
./start.sh                    # build + jalankan di port 8001
./stop.sh                     # hentikan
./stop.sh --hapus             # hentikan + hapus image
```

### Lokal tanpa Docker

```powershell
.\start.ps1 -Local             # membuat .venv bila belum ada, lalu menjalankan
.\start.ps1 -Local -PasangOcr  # sekaligus memasang Tesseract bila belum ada
```

```bash
./start.sh --lokal
```

### Menjalankan di VPS

Skrip PowerShell tidak bisa dijalankan di server Linux, jadi pakai skrip shell
atau perintah Docker langsung.

```bash
cp .env.example .env && nano .env      # isi OPENROUTER_API_KEY dan PATH_AI_API_KEYS
docker compose up -d --build
docker compose logs -f
```

Yang perlu diketahui saat menaruhnya di server:

- **Tesseract sudah ada di dalam image**, beserta data bahasa Indonesia dan
  Inggris. Tidak ada yang perlu dipasang di server. Pemeriksaan bahasanya
  dilakukan saat build, sehingga image gagal dibangun kalau paketnya kurang,
  bukan gagal saat guru mengunggah berkas.
- **Sediakan minimal 2 vCPU dan 2 GB memori** bila guru akan mengunggah berkas
  hasil pindai atau foto. Pengenalan gambar adalah pekerjaan CPU: satu halaman
  A4 pada 300 dpi memakan waktu dan satu inti prosesor.
- **`PATH_AI_MAX_OCR_PAGES`** membatasi jumlah halaman bergambar per berkas.
  Bawaannya 20, dan halaman yang dilewati ikut dilaporkan di hasil, bukan
  disembunyikan.
- **`tmpfs /tmp` dibatasi 256 MB** di `docker-compose.yml`. Kalau muncul galat
  kehabisan ruang saat OCR, batas itu tempat pertama yang perlu diperiksa.
- **`.env` jangan masuk git.** Sudah ada di `.gitignore`.
- Keadaan OCR tercatat di log saat layanan menyala:
  `docker compose logs | grep -i ocr`

### Variabel wajib di `.env`

| Variabel | Keterangan |
|---|---|
| `PATH_AI_API_KEYS` | Daftar kunci dipisah koma. Setiap kunci punya anggaran sendiri. |
| `OPENROUTER_API_KEY` | Satu kunci untuk model berbayar maupun gratis. |
| `PATH_AI_MODEL` | Model teks berbayar. |
| `PATH_AI_MODEL_FREE` | Varian `:free` dari model yang sama. Kosongkan untuk menonaktifkan jalur gratis. |
| `PATH_AI_MODEL_IMAGE` | Model gambar untuk ilustrasi. Kosongkan untuk mematikan ilustrasi. |

---

## Cara memakai

### Autentikasi

Semua endpoint memerlukan header `X-API-Key`, kecuali `/health`.

```powershell
$h = @{ "X-API-Key" = "kunci-anda" }
```

### Fitur 1 & 2 — materi

```powershell
curl -X POST http://localhost:8001/v1/materials/generate `
  -H "X-API-Key: kunci-anda" `
  -F "judul=Hukum Archimedes" `
  -F "deskripsi_materi=Materi pengantar gaya apung untuk SMP kelas VIII" `
  -F "jenjang=SMP kelas VIII" `
  -F "kombinasi=true"
```

Balasannya `202` berisi `job_id`. Pantau lalu unduh:

```powershell
curl http://localhost:8001/v1/jobs/<job_id> -H "X-API-Key: kunci-anda"
curl -O -J http://localhost:8001/v1/jobs/<job_id>/artifact -H "X-API-Key: kunci-anda"
```

`kombinasi=true` menyalakan tabel, diagram, dan ilustrasi sekaligus. Bila
`false`, yang berlaku adalah flag `tabel`, `diagram`, `ilustrasi` satu per satu.

### Fitur 3 — koreksi jawaban

Dua tahap, dan tahap pertama tidak boleh dilewati tanpa sadar.

**Tahap 1: ekstraksi kunci.**

```powershell
curl -X POST http://localhost:8001/v1/answer-keys/extract `
  -H "X-API-Key: kunci-anda" `
  -F "berkas_kunci=@kunci-jawaban.pdf" `
  -F "judul_tugas=Ulangan Harian 1" `
  -F "kelas=VII A"
```

Hasilnya memuat `answer_key_id`, `items`, `issues`, dan `confidence`.
Status job `needs_review` berarti ada bagian yang tidak terbaca dengan yakin.
Tahap ini ada karena kunci jawaban yang dibaca salah akan membuat **seluruh**
nilai satu kelas salah tanpa ada yang menyadari.

**Tahap 2: penilaian.** Jawaban dikirim satu berkas per siswa, dengan email dan
nama sejajar menurut urutannya:

```powershell
curl -X POST http://localhost:8001/v1/assessments/grade `
  -H "X-API-Key: kunci-anda" `
  -F "jawaban_siswa=@001.txt" -F "email=ani@sekolah.id" -F "nama=Ani Pratiwi" `
  -F "jawaban_siswa=@002.txt" -F "email=budi@sekolah.id" -F "nama=Budi Santoso" `
  -F "jawaban_siswa=@003.txt" -F "email=citra@sekolah.id" -F "nama=Citra Dewi" `
  -F "answer_key_id=<answer_key_id>" `
  -F "pengajar=Ratna Kusumawardani" `
  -F "mata_pelajaran=Biologi" `
  -F "kelas=VII A" `
  -F "judul_tugas=Ulangan Harian 1"
```

Jumlah berkas, email, dan nama harus sama banyak. Bila tidak, permintaannya
ditolak dengan pesan yang menyebut ketiga angkanya. Email yang muncul dua kali
juga ditolak, karena itu hampir pasti salah kirim.

Bila guru sudah membetulkan kunci, kirim lewat `kunci_jawaban` (JSON) alih-alih
`answer_key_id`. Kunci yang dikirim langsung oleh guru dianggap sudah
diverifikasi.

`on_low_confidence=stop` (bawaan) menolak menilai bila kunci belum
diverifikasi. Pakai `continue` hanya bila guru memang sadar dan menerimanya.

### Cara mengirim jawaban siswa

Ada dua cara, dan hanya salah satu yang dipakai dalam satu permintaan.

**Satu berkas per siswa** (jalur utama). Kolom `jawaban_siswa` diulang sebanyak
siswa, dengan `email` dan `nama` sejajar menurut urutannya. Ini yang dipakai
aplikasi pengajar, karena daftar kelasnya sudah ada di sana. Bentuk ini juga
dipakai `/v1/assessments/similarity` dan `/v1/assessments/ai-usage`.

**Satu ZIP berisi `manifest.csv`** (jalur alternatif), dikirim lewat kolom
`jawaban_zip`. Berguna bila satu kelas ingin dikirim sekaligus:

```
jawaban.zip
├── manifest.csv
├── 001.pdf
├── 002.docx
└── 003.txt
```

```csv
email,nama,nama_file
ani@sekolah.id,Ani Pratiwi,001.pdf
budi@sekolah.id,Budi Santoso,002.docx
```

Format manifest yang diterima:

- pemisah `,` atau `;` (ekspor Excel Indonesia) — dideteksi otomatis,
- BOM UTF-8 diizinkan,
- header boleh berspasi (`Nama File`) atau tanpa spasi (`nama_file`),
- satu berkas per siswa, jadi seluruh jawaban siswa ada dalam satu berkas itu.

Format berkas yang didukung: **`.pdf`, `.docx`, `.txt`**. `.doc` lama ditolak
dengan pesan yang menyebutkan cara mengonversinya.

### Hasil asesmen berupa Excel

Koreksi dan kemiripan tidak lagi menghasilkan PDF. Keduanya mengisi template
Excel milik sekolah, yang disimpan sebagai berkas bawaan di
`app/assets/template-rekap-koreksi.xlsx`:

- **Koreksi** mengisi lembar capaian: nomor, nama, email, nilai, keterangan
  capaian, dan saran untuk siswa.
- **Kemiripan** mengisi lembar plagiasi: nomor, nama, email, dan persentase
  kemiripan **tertinggi** setiap siswa terhadap teman mana pun.

Siswa yang berkasnya tidak bisa dibaca tetap ditulis, dengan nilai 0 dan
alasannya di kolom keterangan. Menghilangkannya akan membuat rekap satu kelas
tampak lengkap padahal ada siswa yang tidak ikut dinilai.

Nilai ditulis sebagai angka, bukan teks, supaya bisa langsung dihitung di
spreadsheet. Judul, blok identitas, dan lebar kolom diambil dari template apa
adanya, sehingga berkasnya bisa langsung dimasukkan ke sistem nilai.

Posisi sel dicari lewat label, bukan koordinat tetap. Template boleh disunting
kapan saja tanpa mengubah kode, selama kalimat labelnya tetap.

Berkas JSON tetap dibuat sebagai rincian: nilai per butir, kutipan bukti, daftar
pasangan yang mirip, dan siswa yang tidak dapat dinilai. Indikasi penggunaan AI
tetap menghasilkan PDF seperti sebelumnya, karena templatenya tidak punya lembar
untuk itu.

---

## Berkas hasil pindai

Sebagian berkas yang diunggah berupa gambar, bukan teks: hasil pindai, foto
halaman buku, dan foto lembar jawaban siswa. Halaman seperti itu dibaca dengan
pengenalan gambar, dan ada dua mesin yang bisa dipakai.

**OCR lokal (tesseract).** Gratis, berjalan di server, dan sudah tersedia di
dalam image Docker. Lokasi berkasnya dicari otomatis: PATH diperiksa lebih dulu,
lalu lokasi pemasangan yang lazim. Di Windows itu
`%LOCALAPPDATA%\Programs\Tesseract-OCR` dan `Program Files`; di Linux `/usr/bin`
dan `/usr/local/bin`. Jadi `TESSERACT_CMD` biasanya tidak perlu diisi.

**Model penglihatan.** Halaman dikirim ke model yang bisa melihat gambar.
Biayanya sekitar 0,0007 dolar per halaman. CPU server tetap lega, dan hasilnya
lebih baik untuk tulisan tangan serta foto miring.

Urutan pemakaiannya diatur satu variabel:

| Nilai | Artinya |
|---|---|
| `local` (bawaan) | OCR lokal lebih dulu. Model penglihatan hanya dipakai bila OCR lokal tidak tersedia. Tidak pernah mengeluarkan biaya model. |
| `model` | Model penglihatan lebih dulu. Berbayar per halaman, CPU server lega. |

Memeriksa keadaannya, dari tiga tempat:

```bash
docker compose logs | grep -i ocr     # saat layanan menyala
python -m app.ingest.ocr              # dari dalam virtualenv
```

```bash
curl -H "X-API-Key: <kunci>" http://127.0.0.1:8001/v1/ops/usage | grep -o '"ocr[^,]*'
```

Yang terjadi bila ada masalah, dan semuanya sengaja dibuat terlihat:

- Halaman yang gagal dibaca **tidak dihilangkan**. Di dalam teksnya muncul
  penanda, misalnya `[halaman 2: teks tidak dapat dibaca dari gambar]`,
  sehingga laporan dan penilaian tahu ada bagian yang tidak terbaca.
- Anggaran habis di tengah dokumen menghentikan pembacaan halaman berikutnya,
  dan sisanya ditandai dengan alasan itu, bukan diisi teks kosong.
- Hanya halaman tanpa lapisan teks yang dikenai OCR. PDF biasa tidak memakai
  OCR sama sekali, jadi tidak ada tambahan waktu maupun biaya.

---

## Keputusan yang membentuk layanan ini

Bagian ini menjelaskan mengapa beberapa hal dikerjakan dengan cara yang tidak
paling sederhana, supaya tidak ada yang "memperbaikinya" menjadi salah.

**Model tidak pernah menghitung.** Ia memberi skor per butir dan kutipan
bukti. Total, label, dan persentase dihitung server. Nilai siswa harus bisa
dipertanggungjawabkan, dan model bahasa tidak menghasilkan angka yang sama
dua kali.

**Nilai akhir memakai satu rumus untuk semua bentuk kunci.**
`Σ(skor × bobot) / Σ(bobot)`. Skor tiap butir sudah dalam skala 0–100, sehingga
kunci campuran objektif dan esai berjalan tanpa perlakuan khusus. `bobot`
diambil dari kunci bila guru menyebutkannya, bila tidak bernilai 1.

**Bukti yang tidak bisa diverifikasi dibuang.** Kutipan yang dikembalikan
model dicek terhadap teks jawaban siswa. Kutipan yang tidak ditemukan
dihapus, sehingga laporan tidak pernah memuat bukti karangan.

**Butir yang tidak dinilai bukan nol.** Bila model tidak menilai sebagian
butir, sistem mengulang sekali. Bila tetap tidak lengkap, siswa tersebut
dilaporkan sebagai tidak dapat dinilai dan tidak muncul di tabel — bukan
diberi angka karangan.

**Kemiripan dihitung deterministik.** Jaccard pada potongan lima kata dan
cosine TF-IDF. Model hanya menjadi wasit untuk pasangan di pita abu-abu
(25%–75%), dan nilainya dijepit ke ±15 poin dari angka sistem.

**Indikasi AI bersifat heuristik lebih dulu.** Ciri teks dihitung tanpa
biaya untuk semua siswa. Hanya yang berada di band 35–65 yang dikirim ke
model, dan nilainya dijepit ke ±12 poin. Laporan selalu menyebut bahwa ini
indikasi, bukan bukti.

**Model gratis hanya sebagai cadangan, dan tidak untuk penilaian.**
Anggaran berbayar dipakai lebih dulu. Task `pair_judge`, `ai_detect`, dan
`class_summary` boleh turun ke model gratis; `key_extract` dan `item_grade`
tidak pernah. Satu laporan nilai yang sebagian dihasilkan model berbeda
adalah masalah akademik, bukan masalah teknis. Job yang turun kualitas
ditandai `degraded_tasks` di JSON, tidak dicetak di PDF guru.

**Diagram digambar langsung sebagai vektor.** Bukan dirender lewat mesin
browser, juga bukan dirasterisasi. Tinggi diagram diketahui sebelum halaman
disusun, sehingga tidak pernah terpotong.

**Rekap asesmen mengisi template Excel sekolah, bukan membuat berkas baru.**
Templatenya disimpan di `app/assets`, lalu diisi apa adanya memakai openpyxl,
sehingga lebar kolom, gaya tulisan, dan kalimat headernya sama persis dengan
yang dipakai sekolah. Posisi selnya dicari lewat label, bukan koordinat tetap,
supaya template bisa disunting tanpa mengubah kode.

**PDF materi disusun dengan reportlab, bukan mesin render HTML.** Hasilnya
deterministik dan identik di Windows maupun Linux, tanpa pustaka grafis
sistem. Konsekuensinya gaya ditulis sebagai kode, bukan CSS.

---

## Batas yang berlaku

| Batas | Nilai bawaan | Variabel |
|---|---|---|
| Siswa per batch | 100 | `PATH_AI_MAX_STUDENTS_PER_BATCH` |
| Ukuran per berkas | 20 MB | `PATH_AI_MAX_FILE_SIZE_MB` |
| Total isi ZIP setelah ekstraksi | 500 MB | `PATH_AI_MAX_ZIP_UNCOMPRESSED_MB` |
| Rasio kompresi ZIP | 100× | `PATH_AI_ZIP_MAX_RATIO` |
| Ukuran satu request | 50 MB | `PATH_AI_MAX_REQUEST_MB` |
| Token berbayar per kunci per hari | 200.000 | `PATH_AI_BUDGET_TOKENS_PER_DAY` |
| Token per berkas jawaban siswa | 8.000 | `PATH_AI_MAX_TOKENS_PER_STUDENT` |
| Halaman OCR per dokumen | 20 | `PATH_AI_MAX_OCR_PAGES` |
| Ilustrasi per dokumen | 3 | `PATH_AI_MAX_ILLUSTRATIONS` |

Berkas siswa yang melebihi batas token **tidak** menggagalkan batch. Siswa itu
dikeluarkan dari penilaian dengan alasan yang bisa dibaca, dan sisanya tetap
diproses.

## Label nilai

| Rentang | Label |
|---|---|
| 86–100 | Sangat Baik |
| 71–85 | Baik |
| 56–70 | Cukup |
| 0–55 | Perlu Bimbingan |

Ambang dapat diubah lewat `PATH_AI_LABEL_*_MIN`.

---

## Operasional

### Pemantauan

```powershell
curl http://localhost:8001/v1/ops/usage -H "X-API-Key: kunci-anda"
```

Menampilkan sisa anggaran harian, sisa kuota model gratis, jumlah panggilan
dan biaya per model, serta jumlah job yang turun kualitas.

### Batasan yang harus diketahui

- **Satu worker.** Job store, buku besar anggaran, dan penyimpanan kunci
  jawaban ada di memori proses. Menjalankan dengan lebih dari satu worker
  membuat klien yang memantau job bisa mendarat di worker lain dan menerima
  404. Perintah di `Dockerfile` dan `start.ps1` sudah memaksa `--workers 1`.
- **Job hilang saat restart.** Tidak ada database. Job kedaluwarsa setelah
  60 menit dan berkas sementaranya dihapus otomatis.
- **Kunci jawaban hasil ekstraksi berlaku 30 menit.** Bila proses restart di
  antara ekstraksi dan penilaian, guru perlu mengunggah ulang kuncinya.
- **OCR terbatas.** Tesseract bekerja baik pada cetakan dan buruk pada tulisan
  tangan. Halaman pindaian yang tidak terbaca akan muncul di `issues` pada
  tahap ekstraksi, dan guru dapat membetulkannya sebelum menilai.

### Privasi

- Email dan nama siswa tidak pernah masuk log. Log hanya memuat indeks siswa.
- Berkas unggahan dan hasil ditulis ke direktori per job dan dihapus otomatis
  setelah masa berlaku habis.
- Kunci API tidak pernah disimpan; yang dipakai sebagai identitas pemilik
  anggaran adalah sidik jari SHA-256 yang dipotong.
- CORS tidak memakai `*`. Origin didaftarkan lewat `PATH_AI_CORS_ORIGINS`.

---

## Pengembangan

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest        # 66 test
.\.venv\Scripts\python.exe -m ruff check app tests
```

### Susunan kode

```
app/
├── main.py              titik masuk, lifespan, middleware, router
├── jobs.py              job store, runner, semaphore
├── store.py             penyimpanan sementara kunci jawaban
├── api/                 deps, handler galat, middleware, router
├── core/                config, llm, tasks, errors, jsonio, tokens, ratelimit
├── domain/              model data, skema keluaran AI, aritmetika nilai
├── flows/               satu modul per fitur, menyusun seluruh alur
├── ingest/              berkas jawaban (per siswa dan ZIP), dokumen, OCR, vision
├── engines/             tata letak diagram
├── prompts/             semua prompt, satu modul per kelompok fitur
├── render/              tema, flowable, penyusunan PDF, pengisian template Excel
├── assets/              template Excel milik sekolah
└── observability/       log, buku besar anggaran, biaya, kuota gratis
```

Aturan yang dipegang di seluruh kode: keputusan yang mahal atau mudah salah
tidak disebar. Pemilihan model hanya di `core/llm.py`, aritmetika nilai hanya
di `domain/scoring.py`, penegakan flag hanya di `flows/materials.py`, dan
pembacaan konfigurasi hanya di `core/config.py`.
