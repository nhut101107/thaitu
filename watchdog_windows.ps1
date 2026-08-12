$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $AppDir ".venv\Scripts\python.exe"
$LogDir = Join-Path $AppDir "logs"
$Tool = Join-Path $AppDir "tools\cloudflared.exe"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Import-AppEnv {
    Get-Content -LiteralPath (Join-Path $AppDir ".env") | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or $line -notmatch "=") { return }
        $parts = $line -split "=", 2
        $name = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if ($name) { Set-Item -Path "Env:$name" -Value $value }
    }
}

function Test-RecordedProcess([string]$PidFile, [string]$ExpectedCommand) {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $false }
    $value = (Get-Content -Raw -LiteralPath $PidFile).Trim()
    if ($value -notmatch '^\d+$') { return $false }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$value" -ErrorAction SilentlyContinue
    return $null -ne $process -and $process.CommandLine -match [regex]::Escape($ExpectedCommand)
}

function Start-AppProcess([string]$Script, [string]$Name) {
    Import-AppEnv
    $env:PYTHONIOENCODING = "utf-8"
    $process = Start-Process -FilePath $Python -ArgumentList @($Script) -WorkingDirectory $AppDir `
        -RedirectStandardOutput (Join-Path $LogDir "$Name.log") `
        -RedirectStandardError (Join-Path $LogDir "$Name.error.log") `
        -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath (Join-Path $LogDir "$Name.pid") -Value $process.Id -NoNewline
}

function Start-Cloudflare {
    if (-not (Test-Path -LiteralPath $Tool)) { return }
    $process = Start-Process -FilePath $Tool -ArgumentList @(
        "tunnel", "--no-autoupdate", "--protocol", "http2", "--edge-ip-version", "4",
        "--url", "http://127.0.0.1:8080"
    ) -WorkingDirectory $AppDir -RedirectStandardOutput (Join-Path $LogDir "cloudflared.log") `
        -RedirectStandardError (Join-Path $LogDir "cloudflared.error.log") -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath (Join-Path $LogDir "cloudflared.pid") -Value $process.Id -NoNewline
    $publicUrl = ""
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline -and -not $publicUrl) {
        Start-Sleep -Milliseconds 500
        foreach ($file in @((Join-Path $LogDir "cloudflared.log"), (Join-Path $LogDir "cloudflared.error.log"))) {
            if (-not (Test-Path -LiteralPath $file)) { continue }
            $match = Select-String -LiteralPath $file -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches | Select-Object -Last 1
            if ($match) { $publicUrl = $match.Matches[-1].Value }
        }
    }
    if (-not $publicUrl) {
        $recorded = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)" -ErrorAction SilentlyContinue
        if ($recorded -and $recorded.CommandLine -match 'cloudflared\.exe') {
            Stop-Process -Id $process.Id -Force
        }
        return
    }
    $envPath = Join-Path $AppDir ".env"
    $lines = [System.Collections.Generic.List[string]](Get-Content -LiteralPath $envPath)
    $changed = $true
    $found = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match '^\s*TELEGRAM_MINIAPP_URL\s*=') {
            $found = $true
            $changed = $lines[$index] -ne "TELEGRAM_MINIAPP_URL=$publicUrl"
            $lines[$index] = "TELEGRAM_MINIAPP_URL=$publicUrl"
            break
        }
    }
    if (-not $found) { $lines.Add("TELEGRAM_MINIAPP_URL=$publicUrl") }
    if ($changed -or -not $found) {
        [System.IO.File]::WriteAllLines($envPath, $lines, [System.Text.UTF8Encoding]::new($false))
        $botPidFile = Join-Path $LogDir "bot.pid"
        if (Test-Path -LiteralPath $botPidFile) {
            $botValue = (Get-Content -Raw -LiteralPath $botPidFile).Trim()
            if ($botValue -match '^\d+$') {
                $botProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$botValue" -ErrorAction SilentlyContinue
                if ($botProcess -and $botProcess.CommandLine -match 'code_goc\.py') {
                    Stop-Process -Id ([int]$botValue) -Force
                }
            }
        }
        Start-AppProcess "code_goc.py" "bot"
    }
}

while ($true) {
    try {
        if (-not (Test-RecordedProcess (Join-Path $LogDir "miniapp.pid") "miniapp_server.py")) {
            Start-AppProcess "miniapp_server.py" "miniapp"
        }
        if (-not (Test-RecordedProcess (Join-Path $LogDir "bot.pid") "code_goc.py")) {
            Start-AppProcess "code_goc.py" "bot"
        }
        if (-not (Test-RecordedProcess (Join-Path $LogDir "cloudflared.pid") "cloudflared.exe")) {
            Start-Cloudflare
        }
    } catch {
        $safeType = $_.Exception.GetType().Name
        Add-Content -LiteralPath (Join-Path $LogDir "watchdog.error.log") -Value "$(Get-Date -Format s) $safeType"
    }
    Start-Sleep -Seconds 30
}
