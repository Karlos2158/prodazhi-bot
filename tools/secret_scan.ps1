# Block a commit if staged changes contain secrets (bot token, API keys).
# Usage (from repo root): powershell -ExecutionPolicy Bypass -File tools\secret_scan.ps1 [-Staged]
# NOTE: messages are in English on purpose: PowerShell 5.1 reads BOM-less
# UTF-8 files as ANSI, and Cyrillic text would turn into garbage.
param(
    [switch]$Staged
)

$ErrorActionPreference = 'Stop'
# repo root = parent of the folder that holds this script (tools\)
$repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repo

$patterns = @(
    'ghp_[A-Za-z0-9]{30,}',              # GitHub personal access token
    'github_pat_[A-Za-z0-9_]{20,}',      # GitHub fine-grained token
    'AIza[0-9A-Za-z_\-]{30,}',           # Google API key
    '\d{8,10}:[A-Za-z0-9_\-]{30,}',      # Telegram bot token
    'sk-[A-Za-z0-9]{20,}'                # common API secret key
)

if ($Staged) {
    $files = git diff --cached --name-only --diff-filter=ACM
} else {
    $files = git ls-files
}

$bad = @()
foreach ($f in $files) {
    if (-not (Test-Path -LiteralPath $f)) { continue }
    if ($f -match '\.(xlsx|jpg|jpeg|png|pdf|docx|zip|exe|dll)$') { continue }
    if ($Staged) {
        $content = (git show ":$f") -join "`n"
    } else {
        $content = Get-Content -LiteralPath $f -Raw -Encoding UTF8
    }
    if (-not $content) { continue }
    foreach ($p in $patterns) {
        if ($content -match $p) { $bad += "$f  (pattern: $p)" }
    }
}

if ($bad.Count -gt 0) {
    Write-Host "SECRETS FOUND - commit blocked:" -ForegroundColor Red
    $bad | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    Write-Host "Secrets belong to bot_config.json, which is in .gitignore." -ForegroundColor Yellow
    exit 1
}

Write-Host "No secrets found in changes." -ForegroundColor Green
exit 0
