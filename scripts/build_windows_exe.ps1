# Optional: build a standalone Windows .exe of the Fathom desktop app.
#
# You do NOT need this to run the desktop app. The simplest path is:
#     py -3 -m pip install --upgrade "deepseek-tulagent[desktop]"
#     deepseekTulDesktop
# Build an exe only if you want a double-clickable bundle for machines without Python.

$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
# pywebview pulls in its Windows backend (pythonnet / WebView2) via environment markers.
python -m pip install --upgrade ".[desktop]" pyinstaller

# --collect-all bundles data files (the desktop assets/ HTML/CSS/JS) AND submodules;
# the explicit hidden-imports cover pywebview's Windows backend, which PyInstaller's
# static analysis otherwise misses (the usual source of "module not found" crashes).
python -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name DeepSeekFathom `
  --icon assets/app-icon.ico `
  --version-file assets/windows-version-info.txt `
  --collect-all deepseek_tulagent `
  --collect-all webview `
  --collect-submodules webview `
  --hidden-import clr `
  --hidden-import proxy_tools `
  --hidden-import bottle `
  --hidden-import webview.platforms.edgechromium `
  --hidden-import webview.platforms.winforms `
  --hidden-import webview.platforms.mshtml `
  scripts/desktop_launcher.py

$iscc = @(
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
  $version = python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
  & $iscc "/DMyAppVersion=$version" scripts/windows_installer.iss
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
} else {
  Write-Warning "Inno Setup 6 was not found; the portable app was built, but the Setup exe was skipped."
}

Write-Host ""
Write-Host "Built dist\DeepSeekFathom\DeepSeekFathom.exe"
if ($iscc) { Write-Host "Built dist\installer\DeepSeekFathom-$version-Setup.exe" }
Write-Host "If the window is blank, install the Microsoft Edge WebView2 Runtime (a free system component)."
