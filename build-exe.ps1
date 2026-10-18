# =============================================================================
#  build-exe.ps1  --  rebuild wkfile.exe
#
#  Usage:   powershell -ExecutionPolicy Bypass -File .\build-exe.ps1
#
#  Notes:
#   * The build python MUST have tkinter (the GUI is tkinter based).
#     The official python.org installer includes it; the embeddable zip does not.
#   * Override the interpreter with:  $env:WKFIL_BUILD_PYTHON = "C:\...\python.exe"
#   * This script is intentionally pure ASCII (PowerShell 5.1 reads .ps1 as GBK
#     on Chinese Windows, which corrupts non-ASCII source).
# =============================================================================

$ErrorActionPreference = "Continue"

# NOTE: keep Continue, not Stop. Native tools (PyInstaller) write deprecation
# warnings to stderr; under Stop every such warning aborts the whole build.
# Every native call below is followed by an explicit $LASTEXITCODE check instead.

$Root  = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Build = Join-Path $Root ".build"
$Venv  = Join-Path $Build "venv"
$Dist  = Join-Path $Root "dist"
$PipIndex = "https://pypi.tuna.tsinghua.edu.cn/simple"

$AppName = "wkfile"
$AppVer  = "1.7.6"

function Find-Python {
    $cands = @()
    if ($env:WKFIL_BUILD_PYTHON) { $cands += $env:WKFIL_BUILD_PYTHON }
    $cands += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe")
    $cands += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    $cands += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe")
    foreach ($c in $cands) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    $cmd = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($cmd) { return "py" }
    throw "No Python found. Install python.org Python 3.8+ (with tkinter) and retry."
}

Write-Host "== build-exe.ps1 : $AppName ==" -ForegroundColor Cyan

# ---- 1. locate interpreter -------------------------------------------------
$Python = Find-Python
Write-Host "[1/4] python : $Python"

& $Python -c "import sys; print(sys.version)" 2>&1 | Write-Host
& $Python -c "import tkinter" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "This Python has no tkinter. Use the python.org installer (not the embeddable zip)."
}
Write-Host "      tkinter  : OK"

# ---- 2. venv ---------------------------------------------------------------
if (-not (Test-Path $Venv)) {
    Write-Host "[2/4] creating build venv ..."
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
} else {
    Write-Host "[2/4] reusing build venv"
}
$Vpy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $Vpy)) { throw "venv python missing: $Vpy" }

# ---- 3. pyinstaller --------------------------------------------------------
& $Vpy -c "import PyInstaller" 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[3/4] installing pyinstaller from mirror ..."
    & $Vpy -m pip install --quiet --disable-pip-version-check -i $PipIndex pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller install failed" }
} else {
    Write-Host "[3/4] pyinstaller already present"
}
$PiVer = (& $Vpy -m PyInstaller --version 2>&1 | Select-Object -Last 1)
Write-Host "      pyinstaller: $PiVer"

# ---- 4. version info resource ---------------------------------------------
if (-not (Test-Path $Build)) { New-Item -ItemType Directory -Path $Build | Out-Null }

$verText = @'
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 6, 0, 0),
    prodvers=(1, 6, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904B0',
          [
            StringStruct('CompanyName', 'wkfile'),
            StringStruct('FileDescription', 'wkfile - remote file agent for Windows (GUI installer)'),
            StringStruct('FileVersion', '1.7.6.0'),
            StringStruct('InternalName', 'wkfile'),
            StringStruct('LegalCopyright', 'MIT License'),
            StringStruct('OriginalFilename', 'wkfile.exe'),
            StringStruct('ProductName', 'wkfile'),
            StringStruct('ProductVersion', '1.7.6.0')
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
'@

$vfServer = Join-Path $Build "version_server.txt"
[IO.File]::WriteAllText($vfServer, $verText, (New-Object Text.UTF8Encoding($false)))

# ---- 5. build --------------------------------------------------------------
# Do NOT remove the whole dist dir. Sandboxed shells route recursive force
# delete through a safe-delete guard and refuse it, which would abort the
# build. PyInstaller's --noconfirm already overwrites existing outputs.
if (-not (Test-Path $Dist)) { New-Item -ItemType Directory -Path $Dist -Force | Out-Null }

Write-Host "[4/4] building $AppName.exe (GUI, windowed) ..."
& $Vpy -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name $AppName `
    --distpath $Dist --workpath (Join-Path $Build "work-server") --specpath (Join-Path $Build "spec") `
    --paths (Join-Path $Root "server") `
    --version-file $vfServer `
    --hidden-import wkfile_selfcert `
    --hidden-import wkfile_mysql `
    --hidden-import wkfile_redis `
    --hidden-import wkfile_sqlsrv `
    --exclude-module numpy --exclude-module PIL --exclude-module unittest `
    (Join-Path $Root "server\wkfile_gui.py")
if ($LASTEXITCODE -ne 0) { throw "$AppName.exe build failed" }

# The legacy stdio MCP bridge was removed in 1.4.0 (IP direct connect is the
# only supported transport). Its source now lives in deprecated\client\ and is
# no longer built.

Write-Host ""
Write-Host "== done ==" -ForegroundColor Green
Get-ChildItem $Dist -Filter *.exe | ForEach-Object {
    "{0,-20} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB)
}
Write-Host ""
Write-Host "Smoke test:" -ForegroundColor Yellow
Write-Host "  $Dist\$AppName.exe --check-gui"
