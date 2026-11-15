<#
.SYNOPSIS
    Jalankan PATH-AI.

.DESCRIPTION
    Tanpa argumen: build image lalu jalankan container (port 8001).
    Dengan -Local: jalankan langsung dari virtualenv, tanpa Docker.
    Mode lokal memudahkan iterasi karena tidak perlu build image setiap kali.

    Keadaan mesin OCR (tesseract) dilaporkan saat mode lokal dijalankan, supaya
    berkas hasil pindai yang tidak terbaca tidak baru ketahuan saat guru
    mengunggahnya. Pada mode container, keadaan itu tercatat di log container:
        docker compose logs | Select-String -Pattern OCR

.EXAMPLE
    .\start.ps1
    .\start.ps1 -Local
    .\start.ps1 -Local -Port 8002
    .\start.ps1 -Local -PasangOcr
#>
[CmdletBinding()]
param(
    [switch]$Local,
    [int]$Port = 8001,
    [switch]$NoReload,
    # Pasang Tesseract lewat winget bila belum terdeteksi. Hanya untuk Windows.
    [switch]$PasangOcr
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Get-KeadaanOcr {
    param([Parameter(Mandatory)][string]$Python)

    # Pemeriksaan dijalankan lewat modul Python-nya, bukan dengan `python -c`.
    # Di PowerShell, tanda kutip di dalam argumen `-c` ikut terpotong sehingga
    # kode yang dikirim rusak. Selain itu, cara ini menjaga agar hanya ada satu
    # tempat yang tahu di mana tesseract dicari.
    $hasil = & $Python -m app.ingest.ocr 2>$null
    if (-not $hasil) { return $null }

    $bagian = ($hasil | Select-Object -First 1) -split '\|', 2

    return [pscustomobject]@{
        Status     = $bagian[0]
        Keterangan = $bagian[1]
    }
}

function Install-Tesseract {
    $winget = Get-Command winget -ErrorAction SilentlyContinue

    if (-not $winget) {
        Write-Host 'winget tidak ada di komputer ini.' -ForegroundColor Yellow
        Write-Host 'Pasang Tesseract manual: https://github.com/UB-Mannheim/tesseract/wiki' -ForegroundColor Yellow
        return
    }

    Write-Host 'Memasang Tesseract, mode per pengguna sehingga tidak meminta izin administrator...' -ForegroundColor Yellow
    winget install --id UB-Mannheim.TesseractOCR -e --scope user `
        --accept-package-agreements --accept-source-agreements

    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Pemasangan per pengguna tidak berhasil. Coba jalankan tanpa --scope user,' -ForegroundColor Yellow
        Write-Host 'yang mungkin meminta izin administrator:' -ForegroundColor Yellow
        Write-Host '  winget install --id UB-Mannheim.TesseractOCR -e' -ForegroundColor Yellow
    }
}

if (-not (Test-Path -LiteralPath '.env')) {
    Write-Host 'Berkas .env belum ada. Menyalin dari .env.example...' -ForegroundColor Yellow
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
    Write-Host 'Isi nilainya terlebih dahulu (terutama OPENROUTER_API_KEY dan PATH_AI_API_KEYS), lalu jalankan ulang skrip ini.' -ForegroundColor Yellow
    exit 1
}

if ($Local) {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Host 'Virtualenv belum ada. Membuat dan memasang dependensi...' -ForegroundColor Yellow
        python -m venv .venv
        & $python -m pip install --upgrade pip --quiet
        & $python -m pip install -r requirements.txt
    }

    $env:PYTHONPATH = $PSScriptRoot

    $ocr = Get-KeadaanOcr -Python $python

    if ($ocr -and $ocr.Status -eq 'siap') {
        Write-Host ("OCR: " + $ocr.Keterangan) -ForegroundColor Cyan
    } else {
        if ($ocr) {
            Write-Host ("OCR belum siap: " + $ocr.Keterangan) -ForegroundColor Yellow
        } else {
            Write-Host 'OCR tidak dapat diperiksa. Periksa isi .env lebih dulu.' -ForegroundColor Yellow
        }

        if ($PasangOcr) {
            Install-Tesseract
            $ocr = Get-KeadaanOcr -Python $python
            if ($ocr -and $ocr.Status -eq 'siap') {
                Write-Host ("OCR: " + $ocr.Keterangan) -ForegroundColor Green
            }
        } else {
            Write-Host 'Halaman hasil pindai tidak akan terbaca. Pasang dengan: .\start.ps1 -Local -PasangOcr' -ForegroundColor Yellow
        }
    }

    $reloadArgs = if ($NoReload) { @() } else { @('--reload') }

    Write-Host "PATH-AI berjalan lokal di http://127.0.0.1:$Port (workers 1)" -ForegroundColor Green
    Write-Host ("Dokumentasi: http://127.0.0.1:{0}/docs" -f $Port)
    & $python -m uvicorn app.main:app --host 127.0.0.1 --port $Port --workers 1 @reloadArgs
    exit $LASTEXITCODE
}

if ($PasangOcr) {
    Write-Host 'Mode container: Tesseract sudah ada di dalam image, tidak perlu dipasang di Windows.' -ForegroundColor Cyan
}

Write-Host 'Membangun dan menjalankan container path-ai-mbkm...' -ForegroundColor Green
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ''
Write-Host 'Container berjalan. Perintah berguna:' -ForegroundColor Cyan
Write-Host '  docker compose logs -f'
Write-Host '  docker compose logs | Select-String -Pattern OCR'
Write-Host '  docker compose ps'
Write-Host '  .\stop.ps1'
Write-Host ''
Write-Host 'Layanan:' -ForegroundColor Cyan
Write-Host '  http://localhost:8001/health'
Write-Host '  http://localhost:8001/docs'
