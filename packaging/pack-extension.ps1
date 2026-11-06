# Lint the browser extension (extension/) and zip it for the stores:
#   .\packaging\pack-extension.ps1             # lint (if npx is there) + zip
#   .\packaging\pack-extension.ps1 -SkipLint   # zip only
#
# Writes dist\bakin-extension-<version>.zip (version from manifest.json).
# The same zip goes to the Chrome Web Store, Edge Add-ons and Firefox AMO.
# The .md files (privacy policy, store texts) stay out of the zip: they are
# pasted into each store's listing, not shipped inside the extension.
#
# Lint: web-ext (Mozilla), pinned, through npx. Errors stop the script; the
# one expected warning is BACKGROUND_SERVICE_WORKER_IGNORED (one manifest
# for every browser: Chromium reads background.service_worker, Firefox
# background.scripts). Without npx (or offline) the lint is skipped with a
# warning.
param([switch]$SkipLint)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root 'extension'
$webExt = 'web-ext@10.8.0'

$manifest = Get-Content (Join-Path $src 'manifest.json') -Raw -Encoding utf8 | ConvertFrom-Json
$version = $manifest.version
Write-Host "extension $version"

if ($SkipLint) {
    Write-Host 'extension lint: skipped (-SkipLint)'
} elseif (-not (Get-Command npx -ErrorAction SilentlyContinue)) {
    Write-Warning 'extension lint: skipped (npx not found; install Node.js to lint)'
} else {
    $out = npx --yes $webExt lint --source-dir $src --no-input --output json 2>$null
    if (-not $out) {
        Write-Warning "extension lint: skipped ($webExt gave no output: offline?)"
    } else {
        $report = ($out -join "`n") | ConvertFrom-Json
        foreach ($kind in 'errors', 'warnings', 'notices') {
            foreach ($m in $report.$kind) { Write-Host "  $($kind.TrimEnd('s')): $($m.code) $($m.file) $($m.message)" }
        }
        $s = $report.summary
        Write-Host "extension lint: $($s.errors) errors, $($s.warnings) warnings, $($s.notices) notices"
        if ($s.errors -gt 0) { throw 'extension lint failed (web-ext lint)' }
    }
}

# Zip by hand: entry names with forward slashes (AMO rejects backslashes,
# which older Compress-Archive versions wrote).
Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Force $dist | Out-Null
$zipPath = Join-Path $dist "bakin-extension-$version.zip"
Remove-Item $zipPath -ErrorAction SilentlyContinue
$zip = [IO.Compression.ZipFile]::Open($zipPath, [IO.Compression.ZipArchiveMode]::Create)
try {
    $files = Get-ChildItem $src -Recurse -File | Where-Object { $_.Extension -ne '.md' } | Sort-Object FullName
    foreach ($f in $files) {
        $name = [IO.Path]::GetRelativePath($src, $f.FullName) -replace '\\', '/'
        [void][IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $f.FullName, $name, [IO.Compression.CompressionLevel]::Optimal)
    }
} finally {
    $zip.Dispose()
}
Write-Host "extension: $zipPath ($($files.Count) files)"
