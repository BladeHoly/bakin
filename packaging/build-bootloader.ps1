# Rebuild PyInstaller's Windows bootloader from source with the local MSVC and
# put it in the project's .venv.
#
# Why: the prebuilt bootloader that ships with PyInstaller is byte-identical
# in thousands of unrelated programs (malware included), which is what makes
# Microsoft Defender's machine-learning detections (e.g. Bearfoos.A!ml) flag
# installers built with it. A bootloader compiled here is unique to Bakin.
# It reduces false positives; only code signing removes them.
#
# Run again after upgrading PyInstaller. build-engine.ps1 refuses to build
# with the stock bootloader.
# Needs: Visual Studio Build Tools with the C++ workload.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
$version = & $py -c "import PyInstaller; print(PyInstaller.__version__)"
$installed = & $py -c "import PyInstaller, os; print(os.path.join(os.path.dirname(PyInstaller.__file__), 'bootloader', 'Windows-64bit-intel'))"
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vs = & $vswhere -products '*' -latest -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'Visual Studio Build Tools with C++ not found' }
$vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat'

$work = Join-Path $env:TEMP "bakin-bootloader-$version"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $work | Out-Null
Write-Host "PyInstaller ${version}: downloading source"
& $py -m pip download --no-deps --no-binary :all: "pyinstaller==$version" -d $work | Out-Null
tar -xzf (Join-Path $work "pyinstaller-$version.tar.gz") -C $work
$src = Join-Path $work "pyinstaller-$version"

Write-Host 'Compiling the bootloader (MSVC)'
cmd /c "`"$vcvars`" >nul && cd /d `"$src\bootloader`" && `"$py`" ./waf distclean all --target-arch=64bit"
if ($LASTEXITCODE) { throw 'bootloader build failed' }

$built = Join-Path $src 'PyInstaller\bootloader\Windows-64bit-intel'
$marker = [ordered]@{ pyinstaller = $version; built_at = (Get-Date).ToUniversalTime().ToString('s') + 'Z'; files = [ordered]@{} }
foreach ($exe in 'run.exe', 'runw.exe', 'run_d.exe', 'runw_d.exe') {
    Copy-Item (Join-Path $built $exe) (Join-Path $installed $exe) -Force
    $marker.files[$exe] = (Get-FileHash (Join-Path $installed $exe) -Algorithm SHA256).Hash.ToLower()
}
$marker | ConvertTo-Json | Set-Content (Join-Path $installed 'bakin-bootloader.json') -Encoding utf8
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Custom bootloader installed in $installed"
