$ErrorActionPreference='Stop'
$taskRoot=Split-Path $PSScriptRoot
$taskPython=Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
$taskResident=Get-ScheduledTask -TaskName 'TongchengShadowResident' -ErrorAction SilentlyContinue
if (-not $taskResident -or $taskResident.State -ne 'Running') { & (Join-Path $PSScriptRoot 'install-tongcheng-resident.ps1') -Restart }
$taskPort=Get-NetTCPConnection -LocalPort 8791 -State Listen -ErrorAction SilentlyContinue
if (-not $taskPort) { & (Join-Path $PSScriptRoot 'restart-tongcheng.ps1') }
else {
    $taskProcess=Get-CimInstance Win32_Process -Filter ('ProcessId='+$taskPort.OwningProcess)
    if ($taskProcess.CommandLine -notlike '*uvicorn tongcheng_app:app*') { throw 'Port 8791 belongs to another service' }
}
$taskReady=$false
for ($taskAttempt=0; $taskAttempt -lt 30; $taskAttempt++) {
    & $taskPython -c "from urllib.request import build_opener,ProxyHandler; build_opener(ProxyHandler({})).open('http://127.0.0.1:8791/',timeout=2).close()" 2>$null
    if ($LASTEXITCODE -eq 0) { $taskReady=$true; break }
    Start-Sleep -Milliseconds 500
}
if (-not $taskReady) { throw 'Tongcheng service did not start. Check artifacts/tongcheng-private/service-error.log' }
Push-Location $taskRoot
try { & $taskPython (Join-Path $PSScriptRoot 'probe-tongcheng.py') --open --start }
finally { Pop-Location }
Start-Process 'http://127.0.0.1:8791/'
