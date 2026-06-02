$ErrorActionPreference = "Stop"

py -3 -m pip install --upgrade pip
py -3 -m pip install --upgrade ".[desktop]" pyinstaller

py -3 -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name DeepSeekTuLAgent `
  --collect-data deepseek_tulagent `
  --hidden-import webview `
  src/deepseek_tulagent/desktop/app.py

Write-Host "Built dist\DeepSeekTuLAgent\DeepSeekTuLAgent.exe"
