$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$EntryPoint = Join-Path $ProjectRoot "auto_red_clicker_app.py"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Không tìm thấy .venv. Hãy tạo .venv hoặc sửa đường dẫn Python trong script."
}

if (-not (Test-Path -LiteralPath $EntryPoint)) {
    throw "Không tìm thấy auto_red_clicker_app.py."
}

& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name AutoRedClicker `
    --distpath (Join-Path $ProjectRoot "dist") `
    --workpath (Join-Path $ProjectRoot "build") `
    --specpath $ProjectRoot `
    --hidden-import mss `
    --hidden-import mss.windows `
    --hidden-import mss.windows.gdi `
    --hidden-import cv2 `
    --hidden-import numpy `
    --hidden-import pyautogui `
    --hidden-import keyboard `
    --hidden-import keyboard._winkeyboard `
    $EntryPoint

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build thất bại với mã $LASTEXITCODE."
}

Write-Host "Đã build: $(Join-Path $ProjectRoot 'dist\AutoRedClicker.exe')"
