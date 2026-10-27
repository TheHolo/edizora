#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef PayloadDir
  #define PayloadDir "..\output\desktop\bundle\Publisher"
#endif
#ifndef OutputRoot
  #define OutputRoot "..\output\desktop"
#endif

[Setup]
AppId={{D57B59CB-4552-4F43-B5F7-8F1AA4B31E7E}
AppName=Publisher
AppVersion={#AppVersion}
AppPublisher=Publisher
DefaultDirName={localappdata}\Programs\Publisher
DefaultGroupName=Publisher
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputRoot}
OutputBaseFilename=Publisher-Setup-{#AppVersion}-windows-x64
SetupIconFile=..\work\desktop-icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
AppMutex=Local\Publisher.Application
UninstallDisplayIcon={app}\Publisher.exe
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
Name: "autostart"; Description: "Запускать Publisher при входе в Windows"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Publisher"; Filename: "{app}\Publisher.exe"
Name: "{group}\Остановить Publisher"; Filename: "{app}\Publisher.exe"; Parameters: "--stop"
Name: "{autodesktop}\Publisher"; Filename: "{app}\Publisher.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Publisher"; ValueData: """{app}\Publisher.exe"" --background"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\Publisher.exe"; Description: "Открыть Publisher и настроить приложение"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; User data lives outside {app} and is intentionally preserved.

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\Publisher.exe')) then begin
    if not Exec(ExpandConstant('{app}\Publisher.exe'), '--prepare-update', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
      Result := 'Не удалось создать резервную копию перед обновлением. Запустите Publisher и проверьте диагностику.';
  end;
end;
