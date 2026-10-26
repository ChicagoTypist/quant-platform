' 静默启动：不显示黑窗口，后台运行平台
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
sh.Run """" & here & "\启动量化平台.bat""", 0, False
WScript.Sleep 2500
sh.Run "http://127.0.0.1:8899/", 1, False
