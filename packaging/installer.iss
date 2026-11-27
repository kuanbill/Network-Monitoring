#ifndef AppVersion
  #define AppVersion "1.3.0"
#endif
[Setup]
AppId={{D631C0F8-8DDA-4A62-9989-EE327257FB91}
AppName=Netword 主機監控
AppVersion={#AppVersion}
AppPublisher=Netword
DefaultDirName={localappdata}\Programs\NetwordMonitor
DefaultGroupName=Netword 主機監控
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=NetwordMonitor-Setup-{#AppVersion}-x64
SetupIconFile=netword.ico
UninstallDisplayIcon={app}\NetwordMonitor.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
; User settings live outside {app}; uninstall intentionally never deletes them.

[Languages]
Name: "chinesetraditional"; MessagesFile: "compiler:Default.isl,ChineseTraditional.isl"

[Tasks]
Name: "desktopicon"; Description: "建立桌面捷徑"; Flags: unchecked
Name: "startup"; Description: "登入 Windows 後自動啟動（將啟動監控與桌面視窗）"; Flags: unchecked

[Files]
Source: "..\dist\NetwordMonitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Netword 主機監控"; Filename: "{app}\NetwordMonitor.exe"
Name: "{userdesktop}\Netword 主機監控"; Filename: "{app}\NetwordMonitor.exe"; Tasks: desktopicon
Name: "{userstartup}\Netword 主機監控"; Filename: "{app}\NetwordMonitor.exe"; Tasks: startup

[Run]
Filename: "{app}\NetwordMonitor.exe"; Description: "啟動 Netword 主機監控"; Flags: nowait postinstall skipifsilent
