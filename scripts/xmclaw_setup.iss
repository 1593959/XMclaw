; XMclaw Installer Script for Inno Setup
#define MyAppName "XMclaw"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "XMclaw Team"
#define MyAppURL "https://github.com/1593959/XMclaw"
#define MyAppExeName "XMclaw.exe"

[Setup]
AppId={{XMCLAW-APP-0.1.0}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\XMclaw
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=XMclaw_Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\XMclaw.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\XMclaw\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
