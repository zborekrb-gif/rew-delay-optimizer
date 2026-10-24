Option Explicit

Dim shell, fso, folder, exePath
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

folder = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = folder & "\REWDO_GUI.exe"

shell.CurrentDirectory = folder
shell.Run """" & exePath & """", 1, False
