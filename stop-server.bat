@echo off
title SouthparkDS Server - Stop
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*SouthparkDS-server*server.py*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Stopped PID ' + $_.ProcessId) } } else { Write-Host 'Server was not running.' }"
if exist "C:\Users\camer\SouthparkDS-server\server.pid" del "C:\Users\camer\SouthparkDS-server\server.pid"
pause