; Inno Setup script for Whisper 2.
; Build with: build\build.bat (calls iscc).

#define MyAppName       "Whisper 2"
#define MyAppVersion    "2.0.0"
#define MyAppPublisher  "Robert AIIMN"
#define MyAppExeName    "Whisper2.exe"
#define MyAppId         "{{C2A4E1C1-A1A2-4F4F-9F6E-67A0D0F2B3C1}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Whisper2
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=Whisper2-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startupicon"; Description: "Start Whisper 2 when Windows starts"; Flags: checkedonce

[Files]
; The frozen app, copied wholesale.
Source: "..\dist\Whisper2\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion
; Default config seeded into the user's data dir, only if absent;
; never removed on uninstall so settings survive reinstall.
Source: "..\config.yaml"; DestDir: "{userappdata}\Whisper2"; \
    Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{autoprograms}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Make sure the tray process is dead before files are deleted.
Filename: "taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; Flags: runhidden
