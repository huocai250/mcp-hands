@echo off
cd /d "%~dp0"
echo mcp-hands - https://github.com/huocai250/mcp-hands - MIT License
set EXE=%~dp0release\mcp-hands\mcp-hands.exe
if not exist "%EXE%" set EXE=%~dp0release\mcp-hands.exe
if not exist "%EXE%" (
  echo mcp-hands.exe not found. Run build-exe.ps1 first.
  pause
  exit /b 1
)
echo starting %EXE%
start "" "%EXE%" --autostart
