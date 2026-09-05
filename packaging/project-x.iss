#define MyAppName "Project X"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Project X"
#define MyAppExeName "Project X.exe"

[Setup]
AppId={{DFF41EB8-24F4-4C56-9DCB-6F3BF2CD1A77}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Project X
DefaultGroupName=Project X
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=Project-X-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "..\dist\Project X\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Project X"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\Project X"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch Project X"; Flags: nowait postinstall skipifsilent
