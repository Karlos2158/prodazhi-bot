# Publish the next bot version: scan secrets -> commit -> tag -> push.
# Usage: powershell -ExecutionPolicy Bypass -File tools\publish.ps1 -Message "what changed"
param(
    [Parameter(Mandatory = $true)]
    [string]$Message
)

$ErrorActionPreference = 'Stop'
# repo root = parent of the folder that holds this script (tools\)
$repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repo

# 1. stage everything that is not ignored
git add -A
$staged = git diff --cached --name-only
if (-not $staged) {
    Write-Host "Nothing to publish - no changes." -ForegroundColor Yellow
    exit 0
}

Write-Host "Changes to publish:" -ForegroundColor Cyan
$staged | ForEach-Object { Write-Host "  $_" }

# 2. secrets gate
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repo 'tools\secret_scan.ps1') -Staged
if ($LASTEXITCODE -ne 0) {
    Write-Host "Publish cancelled: secrets detected." -ForegroundColor Red
    exit 1
}

# 3. commit
git commit -m $Message | Out-Host
if ($LASTEXITCODE -ne 0) { throw "git commit failed" }

# 4. next version tag: latest vX.Y -> vX.(Y+1), start at v1.0
$last = (git tag --list 'v*' --sort=-v:refname | Select-Object -First 1)
if (-not $last) {
    $next = 'v1.0'
} else {
    $v = $last.TrimStart('v').Split('.')
    $major = [int]$v[0]
    $minor = if ($v.Count -gt 1) { [int]$v[1] + 1 } else { 1 }
    $next = "v$major.$minor"
}
git tag -a $next -m $Message
Write-Host "New version: $next" -ForegroundColor Green

# 5. push branch and tags
git push origin main
if ($LASTEXITCODE -ne 0) { throw "git push failed" }
git push origin $next
if ($LASTEXITCODE -ne 0) { throw "tag push failed" }

Write-Host "Published $next" -ForegroundColor Green
git log --oneline -3

# 6. GitHub release (so "Latest" on the Releases page is really the newest version)
if (Get-Command gh -ErrorAction SilentlyContinue) {
    try {
        gh release create $next --title "Bot $next" --notes $Message | Out-Host
        Write-Host "Release $next created" -ForegroundColor Green
    } catch {
        Write-Host "Release not created (tag is pushed): $_" -ForegroundColor Yellow
    }
} else {
    Write-Host "gh CLI not found - release not created, tag is pushed." -ForegroundColor Yellow
}
