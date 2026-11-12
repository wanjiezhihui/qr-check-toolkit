@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if exist "QRCheck.exe" (
    start "" "QRCheck.exe"
    exit /b 0
)

if exist "QRCheck-portable\QRCheck.exe" (
    start "" "QRCheck-portable\QRCheck.exe"
    exit /b 0
)

echo.
echo   [ERROR] 没找到 QRCheck.exe
echo   QRCheck.exe not found.
echo.
echo   请确认整个文件夹被完整拷贝，不要只拷一部分。
echo   Make sure the whole folder was copied.
echo.
pause
