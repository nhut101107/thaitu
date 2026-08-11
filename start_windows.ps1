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
$miniLog = Join-Path $LogDir "miniapp.log"
$miniErr = Join-Path $LogDir "miniapp.error.log"
$botLog = Join-Path $LogDir "bot.log"
$botErr = Join-Path $LogDir "bot.error.log"

$mini = Start-Process -FilePath $Python -ArgumentList @("miniapp_server.py") -WorkingDirectory $AppDir -RedirectStandardOutput $miniLog -RedirectStandardError $miniErr -PassThru
$bot = Start-Process -FilePath $Python -ArgumentList @("code_goc.py") -WorkingDirectory $AppDir -RedirectStandardOutput $botLog -RedirectStandardError $botErr -PassThru

Write-Host "NFToken Pro đã chạy."
Write-Host ("Mini App PID: {0} | http://{1}:{2}" -f $mini.Id, $env:MINIAPP_HOST, $env:MINIAPP_PORT)
Write-Host ("Bot PID:       {0}" -f $bot.Id)
Write-Host "Log: .\logs\miniapp.log và .\logs\bot.log"
Write-Host "Dừng tiến trình: Stop-Process -Id $($mini.Id),$($bot.Id)"
