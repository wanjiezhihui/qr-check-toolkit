@echo off
cd /d "%~dp0"

if exist "%~dp0QRCheck.exe" (
    start "" "%~dp0QRCheck.exe"
    exit /b
)

if exist "%~dp0QRCheck-portable\QRCheck.exe" (
    start "" "%~dp0QRCheck-portable\QRCheck.exe"
    exit /b
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0qr_dup_checker.py"
    exit /b
)

if exist "D:\soft\Python313\pythonw.exe" (
    start "" "D:\soft\Python313\pythonw.exe" "%~dp0qr_dup_checker.py"
    exit /b
)

echo Python not found. Please use QRCheck.exe instead.
pause
