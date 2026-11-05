@echo off
echo ========================================================
echo  Starting GChart Trading System (Python + .NET 8 WPF)
echo ========================================================

echo [1/2] Launching Python Flask Strategy Engine...
start "GChart Python Engine" cmd /k "cd upstox-nifty-phase1 && ..\.venv\Scripts\python.exe main.py"

echo Waiting for API engine to initialize...
timeout /t 3 /nobreak > nul

echo [2/2] Launching GChart WPF Native Desktop Terminal...
start "GChart WPF Terminal" cmd /c "cd GChartWpf && "%LOCALAPPDATA%\Microsoft\dotnet\dotnet.exe" run"

echo System started successfully.

