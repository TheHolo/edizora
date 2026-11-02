; Isolated stage-1 acceptance build. This is not the Edizora release installer.
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef OutputRoot
  #error OutputRoot is required
#endif

[Setup]
AppId={{1573508B-46BC-46B6-AC7D-955C1FC4C775}
AppName=Edizora desktop prototype
AppVersion=0.1
DefaultDirName={tmp}\EdizoraPrototype
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CreateUninstallRegKey=no
DisableProgramGroupPage=yes
OutputDir={#OutputRoot}
OutputBaseFilename=edizora-prototype-setup
Compression=lzma2
SolidCompression=yes
CloseApplications=no
RestartApplications=no
SetupIconFile=..\frontend\src-tauri\icons\icon.ico

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
