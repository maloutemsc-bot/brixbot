@echo off
title BrixBot - Lanceur
cd /d "%~dp0"

echo ==================================================
echo    BrixBot - Lancement du bot WhatsApp
echo ==================================================
echo.

rem ---------- 1. Python ----------
where python >nul 2>&1
if not errorlevel 1 goto python_ok
echo [ERREUR] Python est introuvable.
echo           Installez-le depuis https://www.python.org
echo           et cochez "Add Python to PATH" a l'installation.
echo.
pause
exit /b 1

:python_ok
if exist ".venv\Scripts\python.exe" goto venv_ok
echo [1/4] Creation de l'environnement Python, premiere fois...
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
goto deps_ok

:venv_ok
echo [1/4] Environnement Python pret.

:deps_ok

rem ---------- 2. Fichiers .env ----------
if not exist "backend\.env" copy /Y "backend\.env.example" "backend\.env" >nul
if not exist "whatsapp-bot\.env" copy /Y "whatsapp-bot\.env.example" "whatsapp-bot\.env" >nul
echo [2/4] Fichiers .env verifies.

rem ---------- 3. Node.js ----------
where node >nul 2>&1
if not errorlevel 1 goto node_ok
echo [ERREUR] Node.js est introuvable.
echo           Installez-le depuis https://nodejs.org version LTS.
echo.
pause
exit /b 1

:node_ok
if exist "whatsapp-bot\node_modules" goto node_done
echo [3/4] Installation des dependances Node, premiere fois...
pushd whatsapp-bot
call npm install
popd
goto port_check

:node_done
echo [3/4] Dependances Node pretes.

rem ---------- 4. Anti double-instance ----------
:port_check
echo [4/4] Verification qu'aucune instance ne tourne deja...

rem Si le port 3000 est occupe, le bot tourne deja : on ne relance pas.
netstat -ano | findstr ":3000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto deja_lance

rem Port 3000 libre mais backend (5000) deja actif : bot seul.
netstat -ano | findstr ":5000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto backend_seul

goto tout_lancer

:tout_lancer
echo [4/4] Demarrage des services...
echo.

rem Ouvre le panneau dans le navigateur apres ~6 secondes
start "" cmd /c "ping -n 7 127.0.0.1 >nul & start http://localhost:5000/admin"

rem Backend Flask dans sa propre fenetre
start "BrixBot - Backend (Flask)" cmd /k ".venv\Scripts\python.exe backend\app.py"

rem Bot WhatsApp dans sa propre fenetre
start "BrixBot - Bot WhatsApp" cmd /k "cd whatsapp-bot && node whatsapp-bot.js"
goto fin

:backend_seul
echo [4/4] Backend deja actif (port 5000), lancement du bot uniquement.
echo.

rem Ouvre le panneau dans le navigateur apres ~6 secondes
start "" cmd /c "ping -n 7 127.0.0.1 >nul & start http://localhost:5000/admin"

start "BrixBot - Bot WhatsApp" cmd /k "cd whatsapp-bot && node whatsapp-bot.js"
goto fin

:deja_lance
echo.
echo  [AVERTISSEMENT] Le bot tourne deja (port 3000 occupe).
echo.
echo  - Ne lancez JAMAIS deux instances en meme temps : ca provoque
echo    les erreurs "Key used already" / "Bad MAC".
echo  - Tout est deja demarre, on ouvre simplement le panneau.
echo  - Pour redemarrer proprement : arreter-bot.bat puis relancez.
start "" cmd /c "start http://localhost:5000/admin"
echo.
pause
exit /b 0

:fin
echo.
echo  Les services demarrent dans des fenetres separees.
echo  - Panneau : http://localhost:5000/admin
echo  - QR Code : onglet WhatsApp du panneau, ou terminal du bot.
echo  Pour tout arreter : double-cliquez sur arreter-bot.bat
echo.
pause
