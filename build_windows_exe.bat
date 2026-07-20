@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows_exe.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo Build failed with exit code %EXIT_CODE%.
    pause
    exit /b %EXIT_CODE%
)
echo.
echo Build completed successfully.
pause
