@echo off
title BrixBot - Nouveau QR Code
cd /d "%~dp0"

echo ==================================================
echo    BrixBot - Reinitialisation de la session
echo    (nouveau QR Code a scanner)
echo ==================================================
echo.

rem ---------- 1. Arret des services ----------
echo [1/3] Arret du bot et du backend en cours...

rem Ferme le backend Flask, port 5000
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5000 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

rem Ferme le bot WhatsApp, port 3000
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3000 " ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

rem Filet de securite : tue aussi les processus node du bot restes orphelins
rem (ne touche PAS aux autres processus node, ex: Freebuff).
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'node.exe' -and $_.CommandLine -match 'whatsapp-bot' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

timeout /t 2 /nobreak >nul

rem ---------- 2. Suppression de la session ----------
if exist "whatsapp-bot\auth_info" (
    rmdir /S /Q "whatsapp-bot\auth_info"
    echo [2/3] Session supprimee. Un NOUVEAU QR Code sera genere.
) else (
    echo [2/3] Aucune session trouvee. Un QR Code sera genere.
)

rem Supprime aussi l'ancienne image QR (evite d'afficher un QR perime dans le panneau)
if exist "whatsapp-bot\qr_actuel.png" del /Q "whatsapp-bot\qr_actuel.png" >nul 2>&1

rem ---------- 3. Relance des services ----------
echo [3/3] Relance du bot... scannez le nouveau QR Code avec WhatsApp.
echo.

rem Si le backend ne tourne pas, on le relance aussi (pour le panneau)
netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    start "BrixBot - Backend (Flask)" cmd /k ".venv\Scripts\python.exe backend\app.py"
    start "" cmd /c "ping -n 7 127.0.0.1 >nul & start http://localhost:5000/admin"
)

rem Lance le bot : le QR Code s'affiche ici et dans le panneau
start "BrixBot - Bot WhatsApp" cmd /k "cd whatsapp-bot && node whatsapp-bot.js"

echo.
echo  Le QR Code apparait dans la fenetre "BrixBot - Bot WhatsApp"
echo  et dans le panneau : http://localhost:5000/admin (onglet WhatsApp).
echo  IMPORTANT : l'ancien appareil est deconnecte, rescannez vite !
echo.
pause
