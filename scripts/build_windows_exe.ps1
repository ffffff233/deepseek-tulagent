# Optional: build a standalone Windows .exe of the Fathom desktop app.
#
# You do NOT need this to run the desktop app. The simplest path is:
#     py -3 -m pip install --upgrade "deepseekfathom[desktop]"
#     deepseekfathom-desktop
# Build an exe only if you want a double-clickable bundle for machines without Python.

$ErrorActionPreference = "Stop"

python -m pip install --upgrade pip
# pywebview pulls in its Windows backend (pythonnet / WebView2) via environment markers.
python -m pip install --upgrade ".[desktop]" pyinstaller
$version = python -c "import sys; sys.path.insert(0, 'src'); from deepseekfathom import DESKTOP_VERSION; print(DESKTOP_VERSION)"
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid desktop version: $version" }
python scripts/prepare_desktop_build.py --version $version --output build/desktop-release
python scripts/collect_third_party_licenses.py --output build/third-party-licenses

# The checked-in spec pins package discovery and UI assets to this checkout. Generating
# a spec with --collect-all here can silently pull an older site-packages frontend.
python -m PyInstaller --noconfirm --clean DeepSeekFathom.spec

$appDir = Resolve-Path "dist\DeepSeekFathom"
$internalDir = Join-Path $appDir "_internal"
$exePath = Join-Path $appDir "DeepSeekFathom.exe"
$actualVersion = (Get-Item $exePath).VersionInfo.ProductVersion
if ($actualVersion -notmatch "^$([regex]::Escape($version))(\.0)?$") {
  throw "Built EXE version $actualVersion does not match $version"
}
$packagedHtml = Join-Path $internalDir "deepseekfathom\_core\desktop\assets\index.html"
$html = Get-Content -LiteralPath $packagedHtml -Raw -Encoding UTF8
if ($html -notmatch [regex]::Escape("style.css?v=$version") -or $html -notmatch [regex]::Escape("app.js?v=$version") -or $html -match '__DESKTOP_VERSION__') {
  throw "Packaged frontend version does not match $version"
}
if (Test-Path (Join-Path $internalDir ("deepseek_" + "tulagent"))) {
  throw "Legacy Python package was bundled into the new desktop app"
}
foreach ($required in @(
  (Join-Path $internalDir "deepseekfathom"),
  (Join-Path $internalDir "LICENSE"),
  (Join-Path $internalDir "NOTICE"),
  (Join-Path $internalDir "licenses\THIRD_PARTY_COMPONENTS.txt")
)) {
  if (-not (Test-Path -LiteralPath $required)) { throw "Required packaged file is missing: $required" }
}

$iscc = @(
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
  "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
  "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
  & $iscc "/DMyAppVersion=$version" scripts/windows_installer.iss
  if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
  $installer = "dist\installer\DeepSeekFathom-$version-Setup.exe"
  if (-not (Test-Path -LiteralPath $installer)) { throw "Expected installer was not created: $installer" }
} else {
  Write-Warning "Inno Setup 6 was not found; the portable app was built, but the Setup exe was skipped."
}

Write-Host ""
Write-Host "Built dist\DeepSeekFathom\DeepSeekFathom.exe"
if ($iscc) { Write-Host "Built dist\installer\DeepSeekFathom-$version-Setup.exe" }
Write-Host "If the window is blank, install the Microsoft Edge WebView2 Runtime (a free system component)."
