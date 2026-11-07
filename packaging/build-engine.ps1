# Freeze the Python engine (bakin.sidecar) into desktop/src-tauri/engine-dist/bakin-engine/
# so the Tauri app can ship it as a resource. Run from the project root:
#   .\packaging\build-engine.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

# Only with the bootloader compiled here (see build-bootloader.ps1): the stock
# one is what antivirus machine-learning detections key on.
$boot = & $py -c "import PyInstaller, os; print(os.path.join(os.path.dirname(PyInstaller.__file__), 'bootloader', 'Windows-64bit-intel'))"
$markerPath = Join-Path $boot 'bakin-bootloader.json'
if (-not (Test-Path $markerPath)) { throw 'Stock PyInstaller bootloader: run .\packaging\build-bootloader.ps1 first' }
$marker = Get-Content $markerPath -Raw | ConvertFrom-Json
if ((Get-FileHash (Join-Path $boot 'run.exe') -Algorithm SHA256).Hash.ToLower() -ne $marker.files.'run.exe') {
    throw 'PyInstaller bootloader changed (upgrade?): run .\packaging\build-bootloader.ps1 again'
}

# The engine shows as part of Bakin in the Task Manager: its icon and a
# description that says what it is.
$version = (Get-Content desktop\src-tauri\tauri.conf.json -Raw | ConvertFrom-Json).version
# Windows' numeric version: the x.y.z part only (a test build's «-prueba.1» stays in the strings)
$v = (($version -replace '-.*$', '').Split('.') + @('0')) -join ', '
$versionFile = Join-Path $root 'build\engine-version.txt'
New-Item -ItemType Directory -Force (Split-Path $versionFile) | Out-Null
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($v), prodvers=($v), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'BladeHoly'),
      StringStruct('FileDescription', 'Bakin (motor)'),
      StringStruct('FileVersion', '$version'),
      StringStruct('InternalName', 'bakin-engine'),
      StringStruct('OriginalFilename', 'bakin-engine.exe'),
      StringStruct('ProductName', 'Bakin'),
      StringStruct('ProductVersion', '$version')])]),
    VarFileInfo([VarStruct('Translation', [0x0409, 1200])])
  ]
)
"@ | Set-Content -Encoding utf8 $versionFile

& $py -m PyInstaller `
    --noconfirm --clean `
    --name bakin-engine `
    --icon (Join-Path $root 'desktop\src-tauri\icons\icon.ico') `
    --version-file $versionFile `
    --onedir `
    --console `
    --collect-submodules mutagen `
    --collect-data certifi `
    --collect-data ytmusicapi `
    --exclude-module tkinter --exclude-module sv_ttk `
    --exclude-module webview --exclude-module pythonnet --exclude-module clr_loader `
    --exclude-module playwright --exclude-module pytest `
    --exclude-module cryptography --exclude-module OpenSSL --exclude-module urllib3.contrib.pyopenssl `
    --distpath desktop\src-tauri\engine-dist `
    --workpath build\pyinstaller `
    --specpath build\pyinstaller `
    packaging\engine_entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

# --console is deliberate: the window starts it with CREATE_NO_WINDOW, so no
# console ever shows, and stdin/stdout stay real pipes for the JSON protocol.
Write-Host "engine: desktop\src-tauri\engine-dist\bakin-engine\bakin-engine.exe"
