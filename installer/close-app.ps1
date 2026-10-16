# Used by the setup wizard before it updates an installed copy: closes FX Workspace in that folder.
# First politely (the desktop window closes; its launcher then stops the server and llama-server by itself),
# then - after 20 s - whatever of that copy is still running (the server under run.bat, a hung process).
param([Parameter(Mandatory = $true)][string]$App)
$App = $App.TrimEnd('\')
function Mine { Get-CimInstance Win32_Process -Filter "name='pythonw.exe' or name='python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$App*" } }

Get-Process 'FX Workspace' -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$App*" } |
    ForEach-Object { [void]$_.CloseMainWindow() }
for ($i = 0; $i -lt 40 -and (Mine | Where-Object { $_.CommandLine -like '*launcher.pyw*' }); $i++) { Start-Sleep -Milliseconds 500 }
Mine | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F | Out-Null }
Get-Process 'FX Workspace' -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$App*" } | Stop-Process -Force
exit 0
