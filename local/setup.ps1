# One-time setup on each Windows computer:
#   irm https://srv1122155.hstgr.cloud/kb/setup.ps1 | iex
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$base = 'https://srv1122155.hstgr.cloud/kb'
$h = $env:USERPROFILE
Write-Host "Knowledge base: Claude Code session uploader for $env:COMPUTERNAME" -ForegroundColor Cyan
$sec = Read-Host -AsSecureString 'Paste your KB_TOKEN (hidden), then Enter'
$token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)).Trim()
if ($token.Length -eq 128 -and $token.Substring(0,64) -eq $token.Substring(64)) { $token = $token.Substring(0,64) }
try {
  Invoke-WebRequest -Uri "$base/stats" -Headers @{ Authorization = "Bearer $token" } -UseBasicParsing | Out-Null
} catch { Write-Host 'That token was not accepted. Copy it again with: grep KB_TOKEN /opt/kb/.env' -ForegroundColor Red; return }
"KB_URL=$base`nKB_TOKEN=$token" | Set-Content -Path (Join-Path $h '.kb_sync.env') -Encoding ASCII
Invoke-WebRequest -Uri "$base/kb_sync.ps1" -OutFile (Join-Path $h 'kb_sync.ps1') -UseBasicParsing

# Keep Claude Code sessions for a year instead of 30 days
$settings = Join-Path $h '.claude\settings.json'
New-Item -ItemType Directory -Force -Path (Split-Path $settings) | Out-Null
$obj = if (Test-Path $settings) { Get-Content $settings -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
if (-not $obj.PSObject.Properties['cleanupPeriodDays'] -or $obj.cleanupPeriodDays -lt 365) {
  $obj | Add-Member -NotePropertyName cleanupPeriodDays -NotePropertyValue 365 -Force
  $obj | ConvertTo-Json -Depth 20 | Set-Content -Path $settings -Encoding UTF8
  Write-Host 'Claude Code will now keep sessions for 365 days.'
}

# Run every 3 hours, hidden
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$h\kb_sync.ps1`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 3)
$opts = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName 'KB Sync' -Action $action -Trigger $trigger -Settings $opts -Force | Out-Null
Write-Host 'Scheduled: uploads new sessions every 3 hours (Task Scheduler > KB Sync).'
Write-Host 'Uploading your sessions now...'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $h 'kb_sync.ps1')
Write-Host 'Done.' -ForegroundColor Green
