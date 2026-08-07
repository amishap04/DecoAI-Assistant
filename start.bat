@echo off
REM ===========================================================================
REM  DecoAI Assistant - start every service
REM
REM  Starts the SD2.1 NPU session server, the Amazon URL builder, the Geniex
REM  vision server and the OpenClaw gateway, then tails the gateway log.
REM  Press Ctrl+C in this window to stop everything.
REM
REM  Run setup.bat first. Arguments are passed straight through, e.g.
REM      start.bat -NoSd21
REM      start.bat -SessionServerOnly
REM ===========================================================================

setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
set "PS1=%SCRIPT_DIR%start.ps1"

if not exist "%PS1%" (
    echo.
    echo   [FAIL] start.ps1 was not found next to this file.
    echo          Expected: %PS1%
    echo.
    pause
    exit /b 1
)

if not exist "%SCRIPT_DIR%decoai-setup.json" (
    echo.
    echo   [FAIL] decoai-setup.json is missing - the project has not been set up
    echo          on this machine yet. Run setup.bat first.
    echo.
    pause
    exit /b 1
)

set "PSEXE=powershell.exe"
where pwsh.exe >nul 2>&1 && set "PSEXE=pwsh.exe"

"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"

echo.
echo   DecoAI stopped (exit code %RC%).
echo.
pause
exit /b %RC%
