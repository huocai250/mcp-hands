@echo off
cd /d "%~dp0"
set EXE=%~dp0release\aiyu-mcp-bridge\aiyu-mcp-bridge.exe
if not exist "%EXE%" set EXE=%~dp0release\aiyu-mcp-bridge.exe
if not exist "%EXE%" set EXE=%~dp0dist\aiyu-mcp-bridge\aiyu-mcp-bridge.exe
if not exist "%EXE%" set EXE=%~dp0dist\aiyu-mcp-bridge.exe
if not exist "%EXE%" (
  echo aiyu-mcp-bridge.exe not found. Run build-exe.ps1 first.
  pause
  exit /b 1
)
echo starting %EXE%
start "" "%EXE%" --autostart
