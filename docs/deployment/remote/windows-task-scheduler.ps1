# Review and customize this example before running it in PowerShell.
$EvoPi = (Get-Command evopi).Source
$Arguments = @(
    "remote", "serve",
    "default",
    "--proxy",
    "--bind", "127.0.0.1",
    "--port", "8765",
    "--allowed-host", "agent.example.com",
    "--trusted-proxy", "127.0.0.0/8"
) -join " "

$Action = New-ScheduledTaskAction -Execute $EvoPi -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "EvoPi Remote Gateway" -Action $Action -Trigger $Trigger -Settings $Settings -Description "Run EvoPi behind a TLS reverse proxy"
