@echo off
echo Stopping ngrok...
taskkill /F /IM ngrok.exe >nul 2>&1
echo Done.
pause