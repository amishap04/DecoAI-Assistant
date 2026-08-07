@echo off
REM ===========================================================================
REM  DecoAI Assistant - Windows / Snapdragon X Elite setup
REM
REM  Double-click this file, or run it from a terminal. It re-launches itself
REM  elevated (winget needs administrator rights to install Node and Python)
REM  and then hands over to setup.ps1.
REM
REM  Any arguments are passed straight through, e.g.
REM      setup.bat -DataRoot D:\DecoAI -Clean
REM      setup.bat -SkipPrereqs
REM ===========================================================================

setlocal EnableExtensions
set "SCRIPT_DIR=%~dp0"
set "PS1=%SCRIPT_DIR%setup.ps1"

if not exist "%PS1%" (
    echo.
    echo   [FAIL] setup.ps1 was not found next to this file.
    echo          Expected: %PS1%
    echo.
    pause
    exit /b 1
)

REM --- Re-launch elevated unless we already are ------------------------------
net session >nul 2>&1
if not errorlevel 1 goto :run
if "%DECOAI_ELEVATED%"=="1" goto :run

echo.
echo   Requesting administrator rights (needed to install Node.js and Python)...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$q=[char]34; Start-Process -FilePath 'cmd.exe' -ArgumentList ('/k set DECOAI_ELEVATED=1 & ' + $q + '%~f0' + $q + ' %*') -Verb RunAs"
if errorlevel 1 goto :noelevate
exit /b 0

:noelevate
echo   [WARN] Could not elevate. Continuing without administrator rights.
echo          Installing Node.js or Python may fail. Either re-run this file
echo          via right-click ^> "Run as administrator", or install those
echo          prerequisites yourself and re-run with -SkipPrereqs.
echo.

:run
REM --- Prefer PowerShell 7 when it is installed ------------------------------
set "PSEXE=powershell.exe"
where pwsh.exe >nul 2>&1 && set "PSEXE=pwsh.exe"

echo.
echo   Running setup.ps1 with %PSEXE% ...
echo.

"%PSEXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo   Setup exited with code %RC%.
) else (
    echo   Setup finished. Next: fill in .env, then run start.bat
)
echo.
pause
exit /b %RC%
