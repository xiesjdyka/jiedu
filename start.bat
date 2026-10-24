@echo off
setlocal
title Jiedu Knowledge Bridge
set "JIEDU_DIR=%~dp0"
if not exist "%JIEDU_DIR%config.json" copy /y "%JIEDU_DIR%config.example" "%JIEDU_DIR%config.json" >nul
set "JIEDU_PY=%JIEDU_DIR%.venv312\Scripts\python.exe"
if not exist "%JIEDU_PY%" set "JIEDU_PY=%JIEDU_DIR%.venv\Scripts\python.exe"
if not exist "%JIEDU_PY%" (
  echo [Jiedu] Python virtual environment not found. Follow README.md first.
  pause
  exit /b 1
)
pushd "%JIEDU_DIR%"
"%JIEDU_PY%" -m knowledge_bridge.desktop_launcher start
set "JIEDU_EXIT=%ERRORLEVEL%"
popd
exit /b %JIEDU_EXIT%
