@echo off
setlocal
chcp 65001 >nul
title AI的她

cd /d "%~dp0"
echo 正在清理旧服务、查找空闲端口并启动 AI的她，请稍候……
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-ui.ps1" -ResetServices -AutoPort %*
set "MIRA_EXIT_CODE=%ERRORLEVEL%"

if not "%MIRA_EXIT_CODE%"=="0" (
  echo.
  echo 启动失败，请查看上方提示和 logs 目录。
  pause
)

exit /b %MIRA_EXIT_CODE%
