Set WshShell = CreateObject("WScript.Shell")
' Run the batch file in the background (0 hides the window)
WshShell.Run Chr(34) & WshShell.CurrentDirectory & "\run_jarvis.bat" & Chr(34), 0
Set WshShell = Nothing
