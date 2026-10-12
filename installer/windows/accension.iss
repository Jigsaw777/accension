; Per-user offline installer. Runtime and assets are staged by build_windows_installer.py.
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef PayloadDir
  #error PayloadDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif

[Setup]
AppId={{8836BF19-0B56-4D39-98D0-6C1B4FD48970}
AppName=Accension
AppVersion={#AppVersion}
AppPublisher=Accension contributors
AppPublisherURL=https://github.com/Jigsaw777/accension
DefaultDirName={localappdata}\Programs\Accension
DefaultGroupName=Accension
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename=Accension-{#AppVersion}-windows-x64
LicenseFile={#PayloadDir}\LICENSE
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\python\pythonw.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Accension"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\desktop.py"""; WorkingDir: "{app}"
Name: "{group}\Stop Accension"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\desktop.py"" --stop"; WorkingDir: "{app}"
Name: "{autodesktop}\Accension"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\desktop.py"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\desktop.py"""; Description: "Open Accension"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Runtime generates caches which are not individually recorded in the install log.
Type: filesandordirs; Name: "{app}\python"

[Code]
function StopInstalledHub(): Boolean;
var
  ExitCode: Integer;
  Python: String;
begin
  Result := True;
  Python := ExpandConstant('{app}\python\python.exe');
  if FileExists(Python) then
    Result := Exec(Python, '-B "' + ExpandConstant('{app}\desktop.py') + '" --stop',
      ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  if not StopInstalledHub() then
    Result := 'Could not stop Accension. Stop the hub before upgrading, then try again.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := StopInstalledHub();
  if not Result then
    MsgBox('Could not stop Accension. Stop the hub before uninstalling, then try again.', mbError, MB_OK);
end;
