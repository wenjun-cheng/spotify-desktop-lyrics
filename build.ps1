# Builds dist\SpotifyLyrics\SpotifyLyrics.exe and zips it as dist\SpotifyLyrics-<version>-win64.zip
# Usage: create .venv, install requirements.txt and pyinstaller into it, then run .\build.ps1
# No $ErrorActionPreference = "Stop": PyInstaller logs to stderr, which Windows PowerShell 5.1
# would treat as an error. Exit codes are checked after each step instead.
Set-Location $PSScriptRoot
$py = ".venv\Scripts\python.exe"

# uiautomation generates its comtypes bindings on first import; make sure they exist before bundling
& $py -c "import uiautomation"
if ($LASTEXITCODE -ne 0) { throw "Generating comtypes bindings failed" }
& $py -c "import sys; from PySide6.QtWidgets import QApplication; a = QApplication(sys.argv); import app; app._make_icon().pixmap(256, 256).save('assets/icon.ico')"
if ($LASTEXITCODE -ne 0) { throw "Generating the icon failed" }

& $py -m PyInstaller --noconfirm --clean --windowed --name SpotifyLyrics --icon assets\icon.ico `
    --collect-submodules winrt --collect-submodules comtypes.gen --collect-data zhconv `
    --exclude-module tkinter app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$ver = (Select-String -Path app.py -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$zip = "dist\SpotifyLyrics-$ver-win64.zip"
Compress-Archive -Path dist\SpotifyLyrics -DestinationPath $zip -Force
"Built $zip ({0:N1} MB)" -f ((Get-Item $zip).Length / 1MB)
