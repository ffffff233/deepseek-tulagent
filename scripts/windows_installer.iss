#ifndef MyAppVersion
  #error MyAppVersion must be provided by scripts/build_windows_exe.ps1
#endif
#define LegacyAppName "DeepSeek" + "TuLAgent"
#define LegacyPackageDir "deepseek_" + "tulagent"

[Setup]
AppId={{D20DC02D-40E2-486A-88B1-AC597D6B45C7}
AppName=DeepSeekFathom
AppVersion={#MyAppVersion}
AppPublisher=DeepSeekFathom
AppPublisherURL=https://github.com/ffffff233/DeepSeekFathom
AppSupportURL=https://github.com/ffffff233/DeepSeekFathom/issues
DefaultDirName={localappdata}\Programs\DeepSeekFathom
DefaultGroupName=DeepSeekFathom
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=DeepSeekFathom-{#MyAppVersion}-Setup
SetupIconFile=..\assets\app-icon.ico
UninstallDisplayIcon={app}\DeepSeekFathom.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\DeepSeekFathom\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\assets\app-icon.ico"; DestDir: "{app}"; DestName: "DeepSeekFathom-{#MyAppVersion}.ico"; Flags: ignoreversion
Source: "..\NOTICE"; DestDir: "{app}"; DestName: "NOTICE.txt"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "Languages\LICENSE"; DestDir: "{app}"; DestName: "LICENSE-Inno-Chinese-Translation.txt"; Flags: ignoreversion

[InstallDelete]
Type: files; Name: "{autodesktop}\{#LegacyAppName}.lnk"
Type: files; Name: "{autoprograms}\{#LegacyAppName}.lnk"
Type: filesandordirs; Name: "{app}\_internal\{#LegacyPackageDir}"
Type: files; Name: "{app}\DeepSeekFathom-*.ico"

[Icons]
Name: "{autodesktop}\DeepSeekFathom"; Filename: "{app}\DeepSeekFathom.exe"; WorkingDir: "{app}"; IconFilename: "{app}\DeepSeekFathom-{#MyAppVersion}.ico"
Name: "{autoprograms}\DeepSeekFathom"; Filename: "{app}\DeepSeekFathom.exe"; WorkingDir: "{app}"; IconFilename: "{app}\DeepSeekFathom-{#MyAppVersion}.ico"

[Run]
Filename: "{app}\DeepSeekFathom.exe"; Description: "{cm:LaunchProgram,DeepSeekFathom}"; Flags: nowait postinstall skipifsilent
