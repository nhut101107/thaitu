# NFToken Pro — Windows Server 2022 launcher
$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir
$VenvDir = Join-Path $AppDir ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $Python)) {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        & $py.Source -3 -m venv $VenvDir
    } else {
        & python -m venv $VenvDir
    }
}
if (-not (Test-Path $Python)) {
    throw "Không tạo được .venv. Hãy cài Python 3.11+ và bật tùy chọn Add Python to PATH."
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $AppDir "requirements.txt")

$EnvFile = Join-Path $AppDir ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $AppDir ".env.example") $EnvFile
    throw "Đã tạo .env từ .env.example. Hãy điền token, URL HTTPS và chạy lại."
}

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#") -or $line -notmatch "=") { return }
    $parts = $line -split "=", 2
    $name = $parts[0].Trim()
    $value = $parts[1].Trim().Trim('"').Trim("'")
    if ($name) { Set-Item -Path "Env:$name" -Value $value }
}

if ([string]::IsNullOrWhiteSpace($env:TELEGRAM_BOT_TOKEN)) {
    throw "Thiếu TELEGRAM_BOT_TOKEN trong .env."
}
if ($env:TELEGRAM_MINIAPP_URL -notlike "https://*") {
    Write-Warning "TELEGRAM_MINIAPP_URL chưa là HTTPS; Telegram Mini App production sẽ không mở được."
}

$LogDir = Join-Path $AppDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$env:PYTHONIOENCODING = "utf-8"
$miniLog = Join-Path $LogDir "miniapp.log"
$miniErr = Join-Path $LogDir "miniapp.error.log"
$botLog = Join-Path $LogDir "bot.log"
$botErr = Join-Path $LogDir "bot.error.log"
$watchdogLog = Join-Path $LogDir "watchdog.log"
$watchdogErr = Join-Path $LogDir "watchdog.error.log"

function Get-RecordedAppProcess([string]$PidFile, [string]$ExpectedCommand) {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $value = (Get-Content -Raw -LiteralPath $PidFile).Trim()
    if ($value -notmatch '^\d+$') { return $null }
    $recorded = Get-Process -Id ([int]$value) -ErrorAction SilentlyContinue
    if (-not $recorded) { return $null }
    $expectedName = if ($ExpectedCommand -eq "cloudflared.exe") { "cloudflared" } else { "python" }
    if ($recorded.ProcessName -ne $expectedName) { return $null }
    return $recorded
}

$mini = Get-RecordedAppProcess (Join-Path $LogDir "miniapp.pid") "miniapp_server.py"
if (-not $mini) {
    $mini = Start-Process -FilePath $Python -ArgumentList @("miniapp_server.py") -WorkingDirectory $AppDir -RedirectStandardOutput $miniLog -RedirectStandardError $miniErr -PassThru
}
$bot = Get-RecordedAppProcess (Join-Path $LogDir "bot.pid") "code_goc.py"
if (-not $bot) {
    $bot = Start-Process -FilePath $Python -ArgumentList @("code_goc.py") -WorkingDirectory $AppDir -RedirectStandardOutput $botLog -RedirectStandardError $botErr -PassThru
}
Set-Content -LiteralPath (Join-Path $LogDir "miniapp.pid") -Value $mini.Id -NoNewline
Set-Content -LiteralPath (Join-Path $LogDir "bot.pid") -Value $bot.Id -NoNewline
$watchdog = $null
$watchdogPidFile = Join-Path $LogDir "watchdog.pid"
if (Test-Path -LiteralPath $watchdogPidFile) {
    $existingWatchdogId = (Get-Content -Raw -LiteralPath $watchdogPidFile).Trim()
    if ($existingWatchdogId -match '^\d+$') {
        $existingWatchdog = Get-CimInstance Win32_Process -Filter "ProcessId=$existingWatchdogId" -ErrorAction SilentlyContinue
        if ($existingWatchdog -and $existingWatchdog.CommandLine -match 'watchdog_windows\.ps1') {
            $watchdog = Get-Process -Id ([int]$existingWatchdogId)
        }
    }
}
if (-not $watchdog) {
    $watchdog = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $AppDir "watchdog_windows.ps1")) -WorkingDirectory $AppDir -RedirectStandardOutput $watchdogLog -RedirectStandardError $watchdogErr -WindowStyle Hidden -PassThru
}
Set-Content -LiteralPath (Join-Path $LogDir "watchdog.pid") -Value $watchdog.Id -NoNewline

Write-Host "NFToken Pro đã chạy."
Write-Host ("Mini App PID: {0} | http://{1}:{2}" -f $mini.Id, $env:MINIAPP_HOST, $env:MINIAPP_PORT)
Write-Host ("Bot PID:       {0}" -f $bot.Id)
Write-Host ("Watchdog PID:  {0}" -f $watchdog.Id)
Write-Host "Log: .\logs\miniapp.log và .\logs\bot.log"
Write-Host "Dừng tiến trình: Stop-Process -Id $($mini.Id),$($bot.Id),$($watchdog.Id)"
