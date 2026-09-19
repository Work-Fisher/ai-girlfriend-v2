@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "CODE=%ERRORLEVEL%"

echo.
if "%CODE%"=="0" (
  echo 安装完成，接下来双击「一键启动.cmd」。
) else if "%CODE%"=="2" (
  echo WSL 已启用。请重启电脑，然后再双击一次「首次安装.cmd」。
) else (
  echo 安装没有完成，看上面的提示。
)
pause
exit /b %CODE%
