@echo off
title BrixBot - Arret
echo ==================================================
echo    BrixBot - Arret des services
echo ==================================================
echo.

rem Ferme le backend Flask, port 5000
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5000 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

rem Ferme le bot WhatsApp, port 3000
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3000 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

rem Filet de securite : tue aussi les processus node du bot restes orphelins
rem (par exemple une instance bloquee avant de prendre le port 3000).
rem Ne touche PAS aux autres processus node (Freebuff, etc.).
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -match 'whatsapp-bot' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

echo  Services arretes.
echo  Vous pouvez fermer les fenetres restantes manuellement.
echo.
pause
