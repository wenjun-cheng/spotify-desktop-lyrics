# 打包成 dist\SpotifyLyrics\SpotifyLyrics.exe，再压缩成 dist\SpotifyLyrics-<版本>-win64.zip
# 用法：先建好 .venv 并装好 requirements.txt 和 pyinstaller，然后 .\build.ps1
# 不用 $ErrorActionPreference = "Stop"：PyInstaller 把普通日志写到 stderr，PowerShell 5.1 会当成错误中止。
# 改为逐步检查退出码。
Set-Location $PSScriptRoot
$py = ".venv\Scripts\python.exe"

# uiautomation 第一次导入时才会生成 comtypes 的 UIAutomation 绑定，打包前先生成好
& $py -c "import uiautomation"
if ($LASTEXITCODE -ne 0) { throw "生成 comtypes 绑定失败" }
& $py -c "import sys; from PySide6.QtWidgets import QApplication; a = QApplication(sys.argv); import app; app._make_icon().pixmap(256, 256).save('assets/icon.ico')"
if ($LASTEXITCODE -ne 0) { throw "生成图标失败" }

& $py -m PyInstaller --noconfirm --clean --windowed --name SpotifyLyrics --icon assets\icon.ico `
    --collect-submodules winrt --collect-submodules comtypes.gen --collect-data zhconv `
    --exclude-module tkinter app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$ver = (Select-String -Path app.py -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$zip = "dist\SpotifyLyrics-$ver-win64.zip"
Compress-Archive -Path dist\SpotifyLyrics -DestinationPath $zip -Force
"打包完成：$zip（{0:N1} MB）" -f ((Get-Item $zip).Length / 1MB)
