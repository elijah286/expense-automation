; Inno Setup 6 script — build Windows installer after PyInstaller
; Install: https://jrsoftware.org/isinfo.php
; Run from repo root: iscc packaging\ExpenseAutomator.iss

#define VerFile FileOpen(SourcePath + "..\VERSION")
#define MyAppVersion Trim(FileRead(VerFile))
#expr FileClose(VerFile)
#define MyAppName "Expense Automator"
#define MyAppPublisher "Expense Automator"
#define MyAppExeName "ExpenseAutomator.exe"

[Setup]
AppId={{7B2C9E1A-5F4D-4E3C-2B1A-0F9E8D7C6B5A}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=ExpenseAutomator_Setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64
SetupIconFile=..\packaging\icons\ExpenseAutomator.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; Use the Restart Manager to close any process still locking app files during an
; in-place upgrade, so the install does not fail and "revert".
CloseApplications=yes
RestartApplications=no
CloseApplicationsFilter=*.exe,*.dll,*.pyd,*.bin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\ExpenseAutomator\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Skip the install-time Chromium download during silent (auto-update) installs —
; the app downloads it on first launch. This keeps silent upgrades fast and robust.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--install-chromium"; StatusMsg: "Downloading Chromium browser engine (~150 MB)..."; Flags: runhidden skipifsilent
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
; Silent (auto-update) installs get no postinstall checkbox, so relaunch the app
; ourselves. This is what lets a looping client land on the new version and stop.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: IsSilentInstall

[Code]
function IsSilentInstall(): Boolean;
begin
  Result := WizardSilent();
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  // A just-closed previous instance may still hold file locks for a moment.
  // Wait briefly so the in-place upgrade does not fail and roll back
  // ("reverting install"). CloseApplications=yes handles a still-running app.
  Sleep(2500);
  Result := '';
end;
