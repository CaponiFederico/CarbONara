; Procedura guidata di installazione per Windows (Inno Setup 6)
;
;
;   ISCC.exe /DAppVersion=0.1.0 packaging\ecomonitor.iss
; la lancia packaging\build_windows.ps1 dopo PyInstaller
;
; L'AppId non va mai cambiato

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "EcoMonitor"
#define AppExeName "EcoMonitor.exe"

[Setup]
AppId={{ef2ed1a8-13dd-4d13-8813-a84e088939a6}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Tesi triennale di green computing
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; nessun amministratore
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
OutputDir=out
OutputBaseFilename={#AppName}-{#AppVersion}-windows-x64-setup
SetupIconFile=ecomonitor.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "italiano"; MessagesFile: "compiler:Languages\Italian.isl"

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Comment: "Monitoraggio di energia e CO2eq"
Name: "{autoprograms}\{#AppName} (amministratore)"; Filename: "{app}\{#AppExeName}"; \
    Comment: "Monitoraggio di energia e CO2eq (sensori hardware completi)"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Avvia {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[Code]
procedure MarkShortcutAsElevated(const LinkPath: string);
var
  Content: AnsiString;
begin
  if not LoadStringFromFile(LinkPath, Content) then
    exit;
  if Length(Content) < 22 then
    exit;
  Content[22] := Chr(Ord(Content[22]) or $20);
  SaveStringToFile(LinkPath, Content, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    MarkShortcutAsElevated(ExpandConstant('{autoprograms}\{#AppName} (amministratore).lnk'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    SuppressibleMsgBox('I dati misurati restano in:' + #13#10 +
           ExpandConstant('{localappdata}\ecomonitor') + #13#10#13#10 +
           'Cancellarli a mano se non servono piu''.', mbInformation, MB_OK, IDOK);
end;
