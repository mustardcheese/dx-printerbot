# Registers PrintBot to start when Windows starts, before anyone logs in.
# Run once in an Administrator PowerShell, from this folder:
#   powershell -ExecutionPolicy Bypass -File .\install_task.ps1
#
# Needs Python 3.10 or later from python.org (not the Microsoft Store version), and:
#   pip install -r requirements.txt
#
# Before you run it, set the bot's settings as user environment variables
# for the Windows account that will run the task (log in as that account):
#   setx SLACK_BOT_TOKEN "xoxb-..."
#   setx SLACK_APP_TOKEN "xapp-..."
#   setx ALERT_USER_IDS  "U0123ABCD,U0456EFGH"
#   setx PRINTER_NAME    "<name from Get-Printer>"
#   setx PRINTER_IP      "<IP from the printer network report>"
#   (WIFI_DIRECT_PROFILE defaults to DIRECT-92HL-L2460DW_BRddaf)

$python = (Get-Command pythonw.exe -ErrorAction Stop).Source
$script = Join-Path $PSScriptRoot "printbot.py"

$action   = New-ScheduledTaskAction -Execute $python -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
$trigger  = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT30S"   # let the network connect first
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

$cred = Get-Credential -Message "Windows account that runs PrintBot (the one with the env vars)"
Register-ScheduledTask -TaskName "PrintBot" -Action $action -Trigger $trigger -Settings $settings `
    -User $cred.UserName -Password $cred.GetNetworkCredential().Password -RunLevel Limited -Force

Write-Host "PrintBot task registered. Test it now with: Start-ScheduledTask -TaskName PrintBot"
