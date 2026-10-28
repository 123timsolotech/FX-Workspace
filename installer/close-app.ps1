# Used by the setup wizard before it updates an installed copy: closes FX Workspace in that folder.
# First politely - the app is asked to quit (POST /api/shell/quit, since closing its window only hides it in the
# tray); its launcher then stops the server and llama-server by itself - then, after 20 s, whatever of that copy is
# still running (the server under run.bat, a hung process).
param([Parameter(Mandatory = $true)][string]$App)
$App = $App.TrimEnd('\')
function Mine { Get-CimInstance Win32_Process -Filter "name='pythonw.exe' or name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$App*" } }

try { Invoke-WebRequest -UseBasicParsing -Method Post -TimeoutSec 3 -Uri 'http://127.0.0.1:8000/api/shell/quit' | Out-Null } catch {}

Get-Process 'FX Workspace' -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$App*" } |
    ForEach-Object { [void]$_.CloseMainWindow() }
for ($i = 0; $i -lt 40 -and (Mine | Where-Object { $_.CommandLine -like '*launcher.pyw*' }); $i++) { Start-Sleep -Milliseconds 500 }
Mine | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F | Out-Null }
Get-Process 'FX Workspace' -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$App*" } | Stop-Process -Force
exit 0
