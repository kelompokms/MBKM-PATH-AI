<#
.SYNOPSIS
    Hentikan PATH-AI.

.EXAMPLE
    .\stop.ps1
    .\stop.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if ($Remove) {
    docker compose down --rmi local --volumes
} else {
    docker compose down
}

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host 'PATH-AI dihentikan.' -ForegroundColor Green