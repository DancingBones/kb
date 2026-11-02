# Uploads new or changed Claude Code sessions from this Windows computer to your knowledge base.
# Settings live in %USERPROFILE%\.kb_sync.env (KB_URL=..., KB_TOKEN=...). Runs from Task Scheduler.
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$home_ = $env:USERPROFILE
$cfg = @{}
Get-Content (Join-Path $home_ '.kb_sync.env') | ForEach-Object {
  if ($_ -match '^\s*([A-Z_]+)\s*=\s*(.+?)\s*$') { $cfg[$Matches[1]] = $Matches[2] }
}
$url = $cfg['KB_URL'].TrimEnd('/'); $token = $cfg['KB_TOKEN']
$machine = ($env:COMPUTERNAME -replace '[^A-Za-z0-9._-]', '_')
$statePath = Join-Path $home_ '.kb_sync_state.json'
$state = @{}
if (Test-Path $statePath) {
  (Get-Content $statePath -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $state[$_.Name] = $_.Value }
}
$projects = Join-Path $home_ '.claude\projects'
$sent = 0; $failed = 0; $checked = 0
if (Test-Path $projects) {
  Get-ChildItem -Path $projects -Directory | ForEach-Object {
    $proj = $_.Name
    Get-ChildItem -Path $_.FullName -Filter *.jsonl -File | ForEach-Object {
      $checked++
      $key = $_.FullName
      $sig = "$($_.Length):$([int64]($_.LastWriteTimeUtc - [datetime]'1970-01-01').TotalSeconds)"
      if ($state[$key] -eq $sig) { return }
      $safeProj = ($proj -replace '[^A-Za-z0-9._@+ -]', '_')
      if ($safeProj.Length -gt 200) { $safeProj = $safeProj.Substring(0, 200) }
      $dest = "$url/upload/claude_code/" + [uri]::EscapeDataString($machine) + '/' +
              [uri]::EscapeDataString($safeProj) + '/' + [uri]::EscapeDataString($_.Name)
      try {
        Invoke-WebRequest -Uri $dest -Method Put -InFile $_.FullName -UseBasicParsing `
          -Headers @{ Authorization = "Bearer $token" } -ContentType 'application/octet-stream' | Out-Null
        $state[$key] = $sig; $sent++
      } catch { $failed++; Write-Output "failed $($_.Exception.Message)" }
    }
  }
}
$state | ConvertTo-Json | Set-Content -Path $statePath -Encoding UTF8
"kb_sync $(Get-Date -Format s): $sent uploaded, $failed failed, $checked checked" |
  Tee-Object -FilePath (Join-Path $home_ '.kb_sync.log') -Append
