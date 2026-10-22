@echo off
cd /d "%~dp0"
if not exist "desktop\node_modules\electron\dist\electron.exe" (
  call npm.cmd ci --prefix desktop
  if errorlevel 1 goto fail
)
if not exist "frontend\dist\index.html" (
  call npm.cmd ci --prefix frontend
  if errorlevel 1 goto fail
  call npm.cmd run build --prefix frontend
  if errorlevel 1 goto fail
)
start "" "%~dp0desktop\node_modules\electron\dist\electron.exe" "%~dp0desktop"
exit /b 0
:fail
echo Nao foi possivel instalar o aplicativo desktop. Confira Node.js e internet.
pause
