; Inno Setup script for Ithildin.
;
; Build (from repo root, after `uv run pyinstaller packaging/ithildin.spec`):
;
;     iscc /DAppVersion=0.1.0 packaging\windows\installer.iss
;
; CI passes the version from pyproject.toml via /DAppVersion. The
; fallback below keeps local builds working when the define is absent.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

#define AppName       "Ithildin"
#define AppPublisher  "Mint Engineering"
#define AppURL        "https://github.com/tobias-hallberg/ithildin-can-analyzer"
#define AppExeName    "ithildin.exe"

[Setup]
AppId={{51c622f1-754b-4859-8446-a8664166f3e1}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist\installer
OutputBaseFilename=Ithildin-{#AppVersion}-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
Uninstallable=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
#if FileExists("..\..\src\ithildin\resources\app_icon.ico")
SetupIconFile=..\..\src\ithildin\resources\app_icon.ico
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "itpassoc"; Description: "Associate &.itp project files with {#AppName}"; GroupDescription: "File associations:"; Flags: unchecked

[Files]
Source: "..\..\dist\ithildin\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Classes\.itp"; ValueType: string; ValueName: ""; ValueData: "Ithildin.Project"; Flags: uninsdeletevalue; Tasks: itpassoc
Root: HKA; Subkey: "Software\Classes\Ithildin.Project"; ValueType: string; ValueName: ""; ValueData: "Ithildin Project"; Flags: uninsdeletekey; Tasks: itpassoc
Root: HKA; Subkey: "Software\Classes\Ithildin.Project\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"; Tasks: itpassoc
Root: HKA; Subkey: "Software\Classes\Ithildin.Project\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; Tasks: itpassoc

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
