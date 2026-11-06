# Build a signed Bakin release and (with -Publish) publish it for the updater.
#
#   .\packaging\release.ps1            # build + sign, writes latest.json and the installer's .sha256,
#                                      # lints and zips the browser extension; publishes nothing
#   .\packaging\release.ps1 -Publish   # also creates the GitHub release in BladeHoly/bakin-releases
#   -SkipSmoke                         # skip the UI smoke test (ui-smoke.ps1); not for real releases
#
# Before a release: bump the version everywhere (tests check they agree) and
# add a "## <version> — <date>" section on top of CHANGELOG.md.
#
# Signing key: %USERPROFILE%\.tauri\bakin-updater.key (never in the repo).
# Its password lives only in Windows Credential Manager
# (service "bakin-release", user "updater-key-password").
# BACK UP THE KEY FILE: without it no installed Bakin will accept an update.
param([switch]$Publish, [switch]$SkipSmoke)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
# Releases are built by the public repo BladeHoly/bakin (.github/workflows/release.yml).
# This manual path only serves the move: the first public release is also uploaded here, to
# the old releases repo, so copies installed before the move find it (their updater looks
# here) and from then on look in BladeHoly/bakin.
$repo = 'BladeHoly/bakin-releases'
Set-Location $root

$conf = Get-Content desktop\src-tauri\tauri.conf.json -Raw | ConvertFrom-Json
$version = $conf.version
$tag = "v$version"
Write-Host "Bakin $version"

& $py packaging\changelog.py
& $py -m pytest -q tests
if ($LASTEXITCODE) { throw 'tests failed' }
if (-not $SkipSmoke) {
    & (Join-Path $PSScriptRoot 'ui-smoke.ps1')
    if ($LASTEXITCODE) { throw 'UI smoke test failed (packaging\ui-smoke.ps1)' }
}
# The browser extension: web-ext lint (errors stop the release; skipped with
# a warning without npx or offline) and its store zip in dist\.
& (Join-Path $PSScriptRoot 'pack-extension.ps1')

if ($Publish) {
    gh release view $tag --repo $repo *> $null
    if ($LASTEXITCODE -eq 0) { throw "$tag is already published in ${repo}: bump the version first" }
}

& (Join-Path $PSScriptRoot 'build-engine.ps1')

$key = Join-Path $env:USERPROFILE '.tauri\bakin-updater.key'
if (-not (Test-Path $key)) { throw "signing key not found: $key" }
$env:TAURI_SIGNING_PRIVATE_KEY = $key
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = & $py -c "import keyring; print(keyring.get_password('bakin-release', 'updater-key-password') or '', end='')"
try {
    Push-Location desktop
    npx tauri build
    if ($LASTEXITCODE) { throw 'tauri build failed' }
} finally {
    Pop-Location
    Remove-Item Env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD, Env:TAURI_SIGNING_PRIVATE_KEY -ErrorAction SilentlyContinue
}

$bundle = Join-Path $root 'desktop\src-tauri\target\release\bundle\nsis'
$setup = Join-Path $bundle "Bakin_${version}_x64-setup.exe"
$sig = "$setup.sig"
if (-not (Test-Path $setup) -or -not (Test-Path $sig)) { throw "missing $setup or its .sig" }

# Code signing (Authenticode, PLAN 0.22.1): once tauri.conf.json has
# bundle.windows.signCommand or certificateThumbprint, the installer must
# carry a Valid signature. Until then releases ship unsigned on purpose.
$win = $conf.bundle.windows
$signing = $win -and ($win.signCommand -or $win.certificateThumbprint)
$auth = Get-AuthenticodeSignature $setup
if ($signing) {
    if ($auth.Status -ne 'Valid') { throw "installer signature is $($auth.Status), not Valid: $($auth.StatusMessage)" }
    Write-Host "signature: Valid ($($auth.SignerCertificate.Subject))"
} elseif ($auth.Status -eq 'NotSigned') {
    Write-Host 'signature: check skipped (no signCommand/certificateThumbprint in tauri.conf.json; the installer is unsigned)'
} elseif ($auth.Status -ne 'Valid') {
    throw "installer has a broken signature ($($auth.Status)) although no signing is configured: $($auth.StatusMessage)"
}

# SHA-256 of the installer: printed, uploaded next to it (sha256sum format)
# and written under the release notes, for the download page.
$setupName = Split-Path $setup -Leaf
$sha256 = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
$shaPath = "$setup.sha256"
[IO.File]::WriteAllText($shaPath, "$sha256  $setupName`n", [Text.UTF8Encoding]::new($false))

# The updater shows the user the sections newer than their version, so the
# notes carry the whole changelog.
$notes = (Get-Content CHANGELOG.md -Raw -Encoding utf8) -replace '(?sm)\A.*?(?=^## )', ''
$latest = [ordered]@{
    version   = $version
    notes     = $notes.Trim()
    pub_date  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    platforms = [ordered]@{
        'windows-x86_64' = [ordered]@{
            signature = (Get-Content $sig -Raw).Trim()
            url       = "https://github.com/$repo/releases/download/$tag/Bakin_${version}_x64-setup.exe"
        }
    }
}
$latestPath = Join-Path $bundle 'latest.json'
[IO.File]::WriteAllText($latestPath, ($latest | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
Write-Host "installer: $setup"
Write-Host "sha256:    $sha256"
Write-Host "manifest:  $latestPath"

if ($Publish) {
    $section = ($notes -split '(?m)^(?=## )' | Where-Object { $_ -match "^## v?$([regex]::Escape($version))\b" } | Select-Object -First 1)
    $notesFile = Join-Path $env:TEMP "bakin-$version-notes.md"
    $body = ($section -replace '^## [^\r\n]*\r?\n', '').Trim() + "`n`n---`n`nSHA-256 de ``$setupName``:`n``$sha256``"
    [IO.File]::WriteAllText($notesFile, $body, [Text.UTF8Encoding]::new($false))
    gh release create $tag $setup $shaPath $latestPath --repo $repo --title "Bakin $version" --notes-file $notesFile
    if ($LASTEXITCODE) { throw 'gh release create failed' }
    Remove-Item $notesFile
    Write-Host "published: https://github.com/$repo/releases/tag/$tag"
}
