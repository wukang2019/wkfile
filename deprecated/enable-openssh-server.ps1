# =====================================================================
#  Enable OpenSSH Server on Windows Server
#  Run as Administrator on the SERVER.
#  Pure ASCII on purpose: PowerShell 5.1 decodes UTF-8 .ps1 as GBK and
#  Chinese comments would break the parser.
# =====================================================================

$ErrorActionPreference = "Continue"

function Say($msg) { Write-Host $msg }

Say "=== Step 0: current state ==="
$cap = Get-WindowsCapability -Online -Name OpenSSH.Server* -ErrorAction SilentlyContinue
if (-not $cap) {
    Say "[X] OpenSSH.Server capability not found in the image. This OS build may not support it."
    Say "    Fallback: install Win32-OpenSSH from https://github.com/PowerShell/Win32-OpenSSH/releases"
    exit 1
}
$cap | ForEach-Object { Say ("    {0}  ->  {1}" -f $_.Name, $_.State) }
Say ("    OpenSSH-Client state: " + (Get-WindowsCapability -Online -Name OpenSSH.Client* -ErrorAction SilentlyContinue).State)

if ($cap.State -ne "Installed") {
    Say ""
    Say "=== Step 1: installing OpenSSH.Server (needs internet / WU access, 1-3 min) ==="
    $r = Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Continue
    Say ("    result: Online=" + $r.Online + " RestartNeeded=" + $r.RestartNeeded)
    $cap = Get-WindowsCapability -Online -Name OpenSSH.Server*
    Say ("    state now: " + $cap.State)
    if ($cap.State -ne "Installed") {
        Say ""
        Say "[X] Install failed. Common causes:"
        Say "    - no internet / WSUS blocks the Feature-on-Demand source"
        Say "    - try with a local ISO source:"
        Say '        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -Source "D:\sources\sxs" -LimitAccess'
        Say "    - or install Win32-OpenSSH manually (offline zip from GitHub):"
        Say "        https://github.com/PowerShell/Win32-OpenSSH/releases"
        exit 1
    }
} else {
    Say ""
    Say "=== Step 1: already installed, skip ==="
}

Say ""
Say "=== Step 2: service ==="
$svc = Get-Service -Name sshd -ErrorAction SilentlyContinue
if (-not $svc) {
    Say "[X] service 'sshd' still not found. Open an new admin PowerShell window and retry."
    exit 1
}
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Start-Sleep -Seconds 2
$svc = Get-Service -Name sshd
Say ("    sshd status   : " + $svc.Status)
Say ("    sshd starttype: " + $svc.StartType)

Say ""
Say "=== Step 3: firewall rule on port 22 ==="
$fw = Get-NetFirewallRule -Name sshd-22 -ErrorAction SilentlyContinue
if (-not $fw) {
    New-NetFirewallRule -Name sshd-22 -DisplayName "OpenSSH Server (22)" -Enabled True `
        -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    Say "    rule created"
} else {
    Say "    rule already exists"
}

Say ""
Say "=== Step 4: verify port 22 is listening ==="
$listen = $null
for ($i = 0; $i -lt 10; $i++) {
    $listen = Get-NetTCPConnection -State Listen -LocalPort 22 -ErrorAction SilentlyContinue
    if ($listen) { break }
    Start-Sleep -Milliseconds 500
}
if ($listen) {
    Say "    OK - port 22 is LISTENING"
} else {
    Say "    [!] port 22 not listening yet. Check: Get-Service sshd ; journal: $env:ProgramData\ssh\logs"
}

Say ""
Say "=== Step 5: local reachability test ==="
$t = Test-NetConnection -ComputerName 127.0.0.1 -Port 22 -InformationLevel Quiet -WarningAction SilentlyContinue
Say ("    127.0.0.1:22 -> " + $(if ($t) { "reachable" } else { "not reachable" }))

Say ""
Say "=== done. Now on the DEV machine run: ==="
Say "    ssh -N -L <port>:127.0.0.1:<port> <account>@<server-address>"
Say "    <port> MUST equal the WinFileServer port on this server"
Say "    (see port field in server\agent.config.json, default 8788)."
Say "    <server-address> is whatever address the DEV machine can reach."
Say "    Do NOT use this tunnel if DEV can already reach <port> directly."
Say "    (or double click the tunnel launcher .bat on the dev machine)"
Say ""
Read-Host "Press Enter to close"
