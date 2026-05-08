@echo off
setlocal
title Stop Turnstile API Server

cd /d "%~dp0"

echo Stopping Turnstile Server on http://127.0.0.1:5000/ ...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$servers = Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*.exe' -or $_.Name -eq 'py.exe') -and $_.CommandLine -like '*api_solver.py*' }; if (-not $servers) { Write-Host 'No running Turnstile Python server found.'; exit 0 }; foreach ($server in $servers) { Stop-Process -Id $server.ProcessId -Force; Write-Host ('Stopped server process PID ' + $server.ProcessId) }"

set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo Done.
) else (
    echo Failed to stop server. Error code %EXIT_CODE%.
)

pause
exit /b %EXIT_CODE%
