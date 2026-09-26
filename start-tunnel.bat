@echo off
setlocal
set NGROK=C:\Users\camer\AppData\Local\ngrok\ngrok.exe
set DIR=C:\Users\camer\SouthparkDS-server

if not exist "%NGROK%" (
    echo ngrok.exe not found at:
    echo   %NGROK%
    pause
    exit /b 1
)
if not exist "%DIR%\ngrok.yml" (
    echo ngrok.yml missing in:
    echo   %DIR%
    pause
    exit /b 1
)

findstr /C:"ADD_YOUR_NGROK" "%DIR%\ngrok.yml" >nul
if not errorlevel 1 goto notoken

echo Starting ngrok tunnel (PLAIN HTTP) for SouthparkDS...
start "ngrok SouthparkDS" "%NGROK%" start --config "%DIR%\ngrok.yml" southparkds
echo Waiting for the tunnel address, then the URL is printed...
python "%DIR%\get-ngrok-url.py"
pause
exit /b 0

:notoken
echo.
echo ngrok needs an authtoken. Do this once:
echo   1. open  https://dashboard.ngrok.com/get-started/your-authtoken
echo   2. edit  %DIR%\ngrok.yml
echo      and replace  ADD_YOUR_NGROK_AUTHTOKEN_HERE  with your token.
pause
exit /b 1