<#
  wkfile - Windows Server deployment script (pure-Python route,
  the alternative to the single-file wkfile.exe).

  NOTE: This file is intentionally ASCII-only. Windows PowerShell 5.1 decodes
  .ps1 files as ANSI/GBK when no BOM is present, which corrupts non-ASCII text
  and breaks parsing. Keep all messages in English.

  Run in an ELEVATED PowerShell:

    powershell -ExecutionPolicy Bypass -File .\install-server.ps1 -Roots "D:\share","E:\data"

  Useful switches:
    -Port 8788                 listen port
    -InstallDir C:\wkfile
    -PythonExe C:\Python312\python.exe
    -ReadOnly                  expose files as read-only
    -TryDownloadPython         fetch Python embeddable zip if Python is missing
    -AllowAllIps               firewall rule for any remote address (default: any)
    -NoFirewall / -NoTask      skip firewall rule / skip auto-start task
#>
[CmdletBinding()]
param(
  [string[]]$Roots = @("D:\share"),
  [int]$Port = 8788,
  [string]$InstallDir = "C:\wkfile",
  [string]$PythonExe = "",
  [string]$TaskName = "wkfile",
  [switch]$ReadOnly,
  [switch]$TryDownloadPython,
  [switch]$NoFirewall,
  [switch]$NoTask,
  [string[]]$AllowIps = @()
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Say($m) { Write-Host $m }
function Ok($m)  { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m){ Write-Host "[!]  $m" -ForegroundColor Yellow }
function Bad($m) { Write-Host "[X]  $m" -ForegroundColor Red }

Say "=============================================="
Say " wkfile - server deployment"
Say "=============================================="

# ---------------------------------------------------------------- 0. elevation
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Bad "This script must run in an ELEVATED PowerShell (Run as Administrator)."
  exit 1
}
Ok "Running elevated."

# ---------------------------------------------------------------- 1. python
function Resolve-Python {
  param([string]$Preferred)
  $cands = New-Object System.Collections.ArrayList
  if ($Preferred) { [void]$cands.Add($Preferred) }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { [void]$cands.Add($cmd.Source) }
  foreach ($p in @(
      "C:\Python313\python.exe", "C:\Python312\python.exe", "C:\Python311\python.exe",
      "C:\Python310\python.exe", "C:\Python39\python.exe",
      "$env:ProgramFiles\Python313\python.exe", "$env:ProgramFiles\Python312\python.exe",
      "$env:ProgramFiles\Python311\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
      "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe")) {
    [void]$cands.Add($p)
  }
  Get-ChildItem "$InstallDir\runtime" -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
    ForEach-Object { [void]$cands.Insert(0, $_.FullName) }
  foreach ($c in $cands) {
    if (-not $c) { continue }
    if (-not (Test-Path $c)) { continue }
    try {
      $v = & $c -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
      if ($LASTEXITCODE -eq 0 -and $v) { return @{ exe = $c; ver = "$v" } }
    } catch { }
  }
  return $null
}

function Get-EmbeddablePython {
  param([string]$Dir)
  $urls = @(
    "https://registry.npmmirror.com/-/binary/python/3.12.10/python-3.12.10-embed-amd64.zip",
    "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip",
    "https://registry.npmmirror.com/-/binary/python/3.11.9/python-3.11.9-embed-amd64.zip",
    "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
  )
  $rt = Join-Path $Dir "runtime"
  New-Item -ItemType Directory -Force -Path $rt | Out-Null
  foreach ($u in $urls) {
    $zip = Join-Path $rt "_python.zip"
    Warn "Trying $u"
    try {
      Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing -TimeoutSec 120
    } catch {
      Warn "  download failed"
      continue
    }
    try {
      Expand-Archive -Path $zip -DestinationPath $rt -Force
      Remove-Item $zip -Force -ErrorAction SilentlyContinue
      $exe = Join-Path $rt "python.exe"
      if (Test-Path $exe) {
        $v = & $exe -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0) { Ok "Python $v ready at $exe"; return $exe }
      }
      Warn "  extracted but python.exe did not run"
    } catch {
      Warn "  extract failed: $($_.Exception.Message)"
    }
  }
  return $null
}

$py = Resolve-Python -Preferred $PythonExe
if (-not $py -and $TryDownloadPython) {
  Warn "No Python found - trying the official embeddable package (no admin install needed)."
  $exe = Get-EmbeddablePython -Dir $InstallDir
  if ($exe) { $py = @{ exe = $exe; ver = "embeddable" } }
}
if (-not $py) {
  Bad "Python 3.8+ was not found on this machine."
  Say ""
  Say "Pick one of these and re-run this script:"
  Say "  (A) no-install route, recommended for locked-down servers:"
  Say "      1. download https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
  Say "      2. unzip it to $InstallDir\runtime"
  Say "      3. re-run:  .\install-server.ps1 -Roots ... -PythonExe `"$InstallDir\runtime\python.exe`""
  Say "  (B) install Python normally, then re-run this script."
  Say "  (C) re-run with -TryDownloadPython to let this script fetch it."
  exit 1
}
Ok "Python: $($py.exe)  (v$($py.ver))"

# ---------------------------------------------------------------- 2. stage files
# the agent imports two siblings at runtime:
#   wkfile_mcp_endpoint.py  -> the MCP layer (/mcp, /sse); optional but needed for MCP
#   wkfile_selfcert.py      -> the self-signed certificate generator (--gen-selfcert)
#   wkfile_mysql.py         -> pure-stdlib MySQL driver (win_db_* tools); optional
#   wkfile_redis.py         -> pure-stdlib Redis driver (win_redis_* tools); optional
$stage = @("wkfile_agent.py", "wkfile_mcp_endpoint.py", "wkfile_selfcert.py",
           "wkfile_mysql.py", "wkfile_redis.py")
$src = Join-Path $PSScriptRoot "wkfile_agent.py"
if (-not (Test-Path $src)) { Bad "wkfile_agent.py not found next to this script ($src)."; exit 1 }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
foreach ($f in $stage) {
  $s = Join-Path $PSScriptRoot $f
  if (-not (Test-Path $s)) { Warn "missing $f (MCP or self-signed cert support will be off)"; continue }
  Copy-Item $s (Join-Path $InstallDir $f) -Force
  Ok "staged $f"
}
$dst = Join-Path $InstallDir "wkfile_agent.py"

foreach ($r in $Roots) {
  if (-not (Test-Path $r)) {
    Warn "Root does not exist yet: $r"
    New-Item -ItemType Directory -Force -Path $r | Out-Null
    Ok "Created $r"
  }
}

# ---------------------------------------------------------------- 3. init config
$cfgPath = Join-Path $InstallDir "wkfile.config.json"
$initArgs = @($dst, "--init", "--port", "$Port", "--roots", ($Roots -join ";"))
& $py.exe @initArgs | ForEach-Object { Say $_ }
if ($LASTEXITCODE -ne 0) { Bad "Agent init failed."; exit 1 }
if (-not (Test-Path $cfgPath)) { Bad "Config not created: $cfgPath"; exit 1 }

$cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
$token = $cfg.token
if ($ReadOnly) { $cfg.read_only = $true; ($cfg | ConvertTo-Json -Depth 6) | Set-Content $cfgPath -Encoding UTF8; Ok "Read-only mode ON" }
if ($AllowIps.Count -gt 0) { $cfg.ip_allowlist = $AllowIps; ($cfg | ConvertTo-Json -Depth 6) | Set-Content $cfgPath -Encoding UTF8; Ok "IP allowlist: $($AllowIps -join ', ')" }

# ---------------------------------------------------------------- 4. firewall
if (-not $NoFirewall) {
  # legacy rule names from the pre-1.5 era; drop them so connect-check (which looks
# for a rule literally named "wkfile") does not report a false negative
  foreach ($rn in @("wkfile", "WinFile Agent")) {
    $existing = Get-NetFirewallRule -DisplayName $rn -ErrorAction SilentlyContinue
    if ($existing) { Remove-NetFirewallRule -DisplayName $rn -ErrorAction SilentlyContinue | Out-Null }
  }
  try {
    New-NetFirewallRule -DisplayName "wkfile" -Direction Inbound -Action Allow `
      -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
    Ok "Firewall rule created for TCP $Port"
  } catch {
    Warn "New-NetFirewallRule failed, falling back to netsh"
    netsh advfirewall firewall add rule name="wkfile" dir=in action=allow protocol=TCP localport=$Port | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "Firewall rule created via netsh" } else { Warn "Firewall rule not created" }
  }
} else {
  Warn "Skipped firewall rule (-NoFirewall)"
}

# ---------------------------------------------------------------- 5. auto start
if (-not $NoTask) {
  try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    $action = New-ScheduledTaskAction -Execute $py.exe -Argument "`"$dst`"" -WorkingDirectory $InstallDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
      -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
      -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
      -Principal $principal -Settings $settings -Force | Out-Null
    Ok "Auto-start task registered: $TaskName (runs as SYSTEM at boot)"
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    Ok "Task started"
  } catch {
    Warn "Could not register the auto-start task: $($_.Exception.Message)"
    Warn "Start the agent manually:  `"$($py.exe)`" `"$dst`""
  }
} else {
  Warn "Skipped auto-start task (-NoTask)"
}

# ---------------------------------------------------------------- 6. report
$ips = @()
try { $ips = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
              Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" }
             ).IPAddress } catch { }
if (-not $ips) { $ips = @("<server-ip>") }

Say ""
Say "=============================================="
Ok "Deployment finished"
Say "  config : $cfgPath"
Say "  audit  : $(Join-Path $InstallDir $cfg.audit_log)"
Say "  roots  : $($Roots -join '  |  ')"
Say "  port   : $Port"
Say "  token  : $token"
Say ""
Say "  Local test on this server:"
Say "    curl.exe http://127.0.0.1:$Port/api/health"
Say ""
Say "  Put this into client\mcp.config.json on your dev machine:"
Say "  {"
Say "    `"base_url`": `"http://$($ips[0]):$Port`","
Say "    `"token`": `"$token`","
Say "    `"timeout`": 60,"
Say "    `"verify_tls`": true"
Say "  }"
Say ""
Warn "Security checklist:"
Say "  1. Restrict the cloud security group for TCP $Port to YOUR office/home public IP only."
Say "  2. Never expose this port to 0.0.0.0/0 without TLS."
Say "  3. Prefer an SSH tunnel or Cloudflare Tunnel instead of a raw public port."
Say "     ssh -N -L 8788:127.0.0.1:$Port user@$($ips[0])   then use base_url http://127.0.0.1:8788"
Say "=============================================="
