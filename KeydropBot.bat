@echo off
rem ==================================================================
rem  Keydrop Bot - double-click this file to start the bot.
rem  First run: downloads everything it needs by itself (uv, Python,
rem  packages, Chromium) and adds a "Keydrop Bot" desktop shortcut.
rem  Later runs: starts the bot right away.
rem ==================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Keydrop Bot
set "SELF=%~f0"
set "TOOLS=%~dp0.tools"
set "UV=%TOOLS%\uv.exe"
set "UVW=%TOOLS%\uvw.exe"
set "RUN=--no-project --python 3.12 --with-requirements src\requirements.txt"
set "CHECK=import playwright, customtkinter, PIL"

if not exist "src\keydrop_ui.py" goto notextracted
if exist "%TOOLS%\ready" if exist "%UV%" goto run

echo.
echo  ===========================================================
echo    Keydrop Bot - first-time setup (only once)
echo    About 250 MB will be downloaded. This takes a few minutes.
echo.
echo    Keydrop Bot - ilk kurulum (sadece bir kez)
echo    Yaklasik 250 MB indirilecek. Birkac dakika surer.
echo  ===========================================================
echo.
if not exist "%SystemRoot%\System32\curl.exe" goto oldwindows
if not exist "%TOOLS%" mkdir "%TOOLS%"
if exist "%UV%" goto haveuv
echo  [1/3] Downloading uv...
"%SystemRoot%\System32\curl.exe" -L --fail --progress-bar -o "%TOOLS%\uv.zip" "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" || goto fail
"%SystemRoot%\System32\tar.exe" -xf "%TOOLS%\uv.zip" -C "%TOOLS%" || goto fail
del "%TOOLS%\uv.zip"
:haveuv
echo  [2/3] Downloading Python and packages...
"%UV%" run %RUN% python -c "%CHECK%" || goto fail
echo  [3/3] Downloading the browser (Chromium)...
"%UV%" run %RUN% python -m playwright install --no-shell chromium || goto fail
echo ok>"%TOOLS%\ready"
call :shortcut
echo.
echo  Setup complete! A "Keydrop Bot" shortcut was added to your desktop.
echo  Kurulum tamam! Masaustune "Keydrop Bot" kisayolu eklendi.
echo.

:run
rem Quick check (about a second). Without internet, use what was already downloaded.
set "MODE="
"%UV%" run %RUN% python -c "%CHECK%" >nul 2>&1 && goto launch
set "MODE=--offline"
"%UV%" run %MODE% %RUN% python -c "%CHECK%" >nul 2>&1 || goto fail
:launch
start "" "%UVW%" run %MODE% %RUN% --gui-script src\keydrop_ui.py
exit /b 0

:shortcut
rem Desktop shortcut: starts this file minimized, with the bot's icon.
rem (KEYDROP_NO_SHORTCUT=1 skips it, used for testing.)
if "%KEYDROP_NO_SHORTCUT%"=="1" exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Keydrop Bot.lnk')); $s.TargetPath=$env:SELF; $s.WorkingDirectory=(Split-Path $env:SELF); $s.WindowStyle=7; $s.IconLocation=(Join-Path (Split-Path $env:SELF) 'src\icon.ico'); $s.Save()" >nul 2>&1
exit /b 0

:notextracted
echo.
echo  Please EXTRACT the ZIP first (right click - Extract All),
echo  then run KeydropBot.bat from the extracted folder.
echo.
echo  Lutfen once ZIP'i cikart (sag tik - Tumunu ayikla),
echo  sonra cikan klasordeki KeydropBot.bat'i calistir.
echo.
pause
exit /b 1

:oldwindows
echo  This needs Windows 10 (version 1803) or newer.
echo  Windows 10 (1803) veya daha yeni bir surum gerekir.
pause
exit /b 1

:fail
echo.
echo  Something went wrong. Check your internet connection and run KeydropBot.bat again.
echo  Bir sorun oldu. Internet baglantini kontrol edip KeydropBot.bat'i tekrar calistir.
echo.
pause
exit /b 1
