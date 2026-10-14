' Agentic Workspace - double-click to start. Opens only the app window: no console windows, not even a
' flash. Closing the app window stops it. The first time (no .venv yet) it runs run.bat in a window instead,
' so the setup can be seen. launcher.pyw has the details.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here
pythonw = here & "\.venv\Scripts\pythonw.exe"
If fso.FileExists(pythonw) Then
    shell.Run """" & pythonw & """ """ & here & "\launcher.pyw""", 0, False
Else
    shell.Run """" & here & "\run.bat"" console", 1, False
End If
