# Creates a desktop shortcut for streamtools
$WshShell = New-Object -ComObject WScript.Shell
$Desktop = $WshShell.SpecialFolders("Desktop")
$Shortcut = $WshShell.CreateShortcut("$Desktop\streamtools.lnk")
$Shortcut.TargetPath = "C:\GitHub Repositories\streamtools\launch.bat"
$Shortcut.WorkingDirectory = "C:\GitHub Repositories\streamtools"
$Shortcut.WindowStyle = 1
$Shortcut.Description = "Launch streamtools video pipeline"
$Shortcut.Save()
Write-Host "Shortcut created on Desktop." -ForegroundColor Green
