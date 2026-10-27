#ifndef AppVersion
  #define AppVersion "0.1"
#endif
#ifndef PayloadDir
  #define PayloadDir "..\output\desktop\bundle\edizora"
#endif
#ifndef OutputRoot
  #define OutputRoot "..\output\desktop"
#endif

[Setup]
AppId={{D57B59CB-4552-4F43-B5F7-8F1AA4B31E7E}
AppName=Edizora
AppVersion={#AppVersion}
AppPublisher=TheHolo
AppPublisherURL=https://github.com/TheHolo/edizora
AppSupportURL=https://github.com/TheHolo/edizora/issues
AppUpdatesURL=https://github.com/TheHolo/edizora/releases
VersionInfoVersion=0.1.0.0
VersionInfoProductName=Edizora
VersionInfoDescription=Edizora 0.1 — application installer
DefaultDirName={localappdata}\Programs\Edizora
DefaultGroupName=Edizora
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputRoot}
OutputBaseFilename=edizora
SetupIconFile=..\work\desktop-icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
AppMutex=Local\Edizora.Application
UninstallDisplayIcon={app}\edizora.exe
DisableWelcomePage=no
SetupLogging=yes
LanguageDetectionMethod=none
#ifdef AcceptanceInstall
; Isolated acceptance installs keep all files in a disposable test directory.
CreateUninstallRegKey=no
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[CustomMessages]
english.DesktopShortcut=Create a desktop shortcut
russian.DesktopShortcut=Создать ярлык на рабочем столе
english.Autostart=Start Edizora when signing in to Windows
russian.Autostart=Запускать Edizora при входе в Windows
english.StopApp=Stop Edizora
russian.StopApp=Остановить Edizora
english.OpenApp=Open Edizora and set up the application
russian.OpenApp=Открыть Edizora и настроить приложение
english.BackupFailed=Could not create a backup before updating. Start Edizora and check diagnostics.
russian.BackupFailed=Не удалось создать резервную копию перед обновлением. Запустите Edizora и проверьте диагностику.
english.LegacyBackupFailed=Could not create a backup before updating. Start the previous version and check diagnostics.
russian.LegacyBackupFailed=Не удалось создать резервную копию перед обновлением. Запустите прежнюю версию и проверьте диагностику.

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopShortcut}"; Flags: unchecked
Name: "autostart"; Description: "{cm:Autostart}"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Edizora"; Filename: "{app}\edizora.exe"
Name: "{group}\{cm:StopApp}"; Filename: "{app}\edizora.exe"; Parameters: "--stop"
Name: "{autodesktop}\Edizora"; Filename: "{app}\edizora.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Edizora"; ValueData: """{app}\edizora.exe"" --background"; Tasks: autostart; Flags: uninsdeletevalue
#ifndef AcceptanceInstall
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Publisher"; Flags: deletevalue
#endif

[InstallDelete]
Type: files; Name: "{app}\Publisher.exe"

[Run]
Filename: "{app}\edizora.exe"; Description: "{cm:OpenApp}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; User data lives outside {app} and is intentionally preserved.

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\edizora.exe')) then begin
    if not Exec(ExpandConstant('{app}\edizora.exe'), '--prepare-update', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      Result := CustomMessage('BackupFailed');
  end else if FileExists(ExpandConstant('{app}\Publisher.exe')) then begin
    if not Exec(ExpandConstant('{app}\Publisher.exe'), '--prepare-update', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      Result := CustomMessage('LegacyBackupFailed');
  end;
end;
