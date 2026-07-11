#ifndef MyAppVersion
  #define MyAppVersion "0.1.10"
#endif

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

[Files]
Source: "..\dist\DeepSeekFathom\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\assets\app-icon.ico"; DestDir: "{app}"; DestName: "DeepSeekFathom-{#MyAppVersion}.ico"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\DeepSeekFathom"; Filename: "{app}\DeepSeekFathom.exe"; WorkingDir: "{app}"; IconFilename: "{app}\DeepSeekFathom-{#MyAppVersion}.ico"
Name: "{autoprograms}\DeepSeekFathom"; Filename: "{app}\DeepSeekFathom.exe"; WorkingDir: "{app}"; IconFilename: "{app}\DeepSeekFathom-{#MyAppVersion}.ico"

[Run]
Filename: "{app}\DeepSeekFathom.exe"; Description: "启动 DeepSeekFathom"; Flags: nowait postinstall skipifsilent
