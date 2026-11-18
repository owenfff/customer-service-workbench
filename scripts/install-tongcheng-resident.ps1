param([switch]$Restart)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path $PSScriptRoot
$taskPrivate=Join-Path $taskRoot 'artifacts\tongcheng-private'
$taskPython=Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\pythonw.exe'
$taskName='TongchengShadowResident'
$taskUser=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
New-Item -ItemType Directory -Force -Path $taskPrivate | Out-Null
$taskControl=Join-Path $taskPrivate 'resident-control.json'
if (-not (Test-Path -LiteralPath $taskControl)) { '{"monitor_enabled":true}' | Set-Content -LiteralPath $taskControl -Encoding ASCII }
$taskExisting=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($taskExisting -and $taskExisting.Actions.Arguments -notlike '*tongcheng-resident.py*') { throw 'Task name belongs to another program' }
$taskAction=New-ScheduledTaskAction -Execute $taskPython -Argument ('"'+(Join-Path $PSScriptRoot 'tongcheng-resident.py')+'"') -WorkingDirectory $taskRoot
$taskTrigger=New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$taskPrincipal=New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskSettings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description 'Restores locally authorized Tongcheng reception; respects deliberate pause.' -Force | Out-Null
if ($Restart) { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
$taskConnection=Get-NetTCPConnection -LocalPort 8791 -State Listen -ErrorAction SilentlyContinue
if ($taskConnection -and ($Restart -or -not $taskExisting)) {
    $taskService=Get-CimInstance Win32_Process -Filter ('ProcessId='+$taskConnection.OwningProcess)
    $taskOwnedWrapper=$false
    $taskStatusPath=Join-Path $taskPrivate 'resident-status.json'
    if (Test-Path -LiteralPath $taskStatusPath) {
        $taskStatus=Get-Content -LiteralPath $taskStatusPath -Raw | ConvertFrom-Json
        $taskOwnedWrapper=($taskStatus.service_pid -eq $taskService.ProcessId -and
            $taskService.ExecutablePath -eq (Join-Path (Split-Path $taskPython) 'python.exe') -and
            ($taskService.CommandLine.Contains("uvicorn.run('tongcheng_app:app',app_dir='server',host='127.0.0.1',port=8791") -or
             $taskService.CommandLine.Contains("uvicorn.run('tongcheng_app:app',app_dir='server',host='0.0.0.0',port=8791")))
    }
    if ($taskService.CommandLine -notlike '*uvicorn tongcheng_app:app*' -and -not $taskOwnedWrapper) { throw 'Port 8791 belongs to another service' }
    Stop-Process -Id $taskService.ProcessId
}
Start-ScheduledTask -TaskName $taskName
Write-Output 'Tongcheng resident scheduled task started: http://127.0.0.1:8791/'
