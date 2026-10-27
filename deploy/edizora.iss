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
VersionInfoDescription=Edizora 0.1 — установка приложения
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
#ifdef AcceptanceInstall
; Isolated acceptance installs keep all files in a disposable test directory.
CreateUninstallRegKey=no
#endif

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; Flags: unchecked
Name: "autostart"; Description: "Запускать Edizora при входе в Windows"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Edizora"; Filename: "{app}\edizora.exe"
Name: "{group}\Остановить Edizora"; Filename: "{app}\edizora.exe"; Parameters: "--stop"
Name: "{autodesktop}\Edizora"; Filename: "{app}\edizora.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Edizora"; ValueData: """{app}\edizora.exe"" --background"; Tasks: autostart; Flags: uninsdeletevalue
#ifndef AcceptanceInstall
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Publisher"; Flags: deletevalue
#endif

[InstallDelete]
Type: files; Name: "{app}\Publisher.exe"

[Run]
Filename: "{app}\edizora.exe"; Description: "Открыть Edizora и настроить приложение"; Flags: nowait postinstall skipifsilent

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
      Result := 'Не удалось создать резервную копию перед обновлением. Запустите Edizora и проверьте диагностику.';
  end else if FileExists(ExpandConstant('{app}\Publisher.exe')) then begin
    if not Exec(ExpandConstant('{app}\Publisher.exe'), '--prepare-update', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      Result := 'Не удалось создать резервную копию перед обновлением. Запустите прежнюю версию и проверьте диагностику.';
  end;
end;
