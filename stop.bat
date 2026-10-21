@echo off
setlocal
title Jiedu Knowledge Bridge
set "JIEDU_DIR=%~dp0"
set "JIEDU_PY=%JIEDU_DIR%.venv312\Scripts\python.exe"
if not exist "%JIEDU_PY%" set "JIEDU_PY=%JIEDU_DIR%.venv\Scripts\python.exe"
if not exist "%JIEDU_PY%" (
  echo [Jiedu] Python virtual environment not found. Cannot run the launcher.
  pause
  exit /b 1
)
pushd "%JIEDU_DIR%"
"%JIEDU_PY%" -m knowledge_bridge.bridge_launcher stop
set "JIEDU_EXIT=%ERRORLEVEL%"
popd
if not "%JIEDU_EXIT%"=="0" (
  echo [Jiedu] Stop failed. See data\runtime\bridge-launcher.log
  pause
)
exit /b %JIEDU_EXIT%
