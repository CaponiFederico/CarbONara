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
Source: "autostart.ps1"; DestDir: "{app}"

[Tasks]
Name: "autostart"; Description: "Avvia EcoMonitor all'accesso"; \
    GroupDescription: "Avvio automatico:"
Name: "autostart\background"; Description: "Parti nell'area di notifica, senza aprire la finestra"; \
    GroupDescription: "Avvio automatico:"; Flags: unchecked

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Comment: "Monitoraggio di energia e CO2eq"
Name: "{autoprograms}\{#AppName} (amministratore)"; Filename: "{app}\{#AppExeName}"; \
    Comment: "Monitoraggio di energia e CO2eq"

[Registry]
Root: HKCU; Subkey: "Software\EcoMonitor"; ValueType: dword; ValueName: "Autostart"; \
    ValueData: 1; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: autostart

[Run]
Filename: "{sys}\schtasks.exe"; Parameters: "/run /tn ""{#AppName}"""; \
    Description: "Avvia {#AppName}"; Flags: postinstall skipifsilent runhidden; \
    Check: AutostartTaskExists
Filename: "{app}\{#AppExeName}"; Description: "Avvia {#AppName}"; \
    Flags: nowait postinstall skipifsilent; Check: not AutostartTaskExists

[Code]
function EcoMonitorIsRunning: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'),
    '/c tasklist /fi "imagename eq {#AppExeName}" | find /i "{#AppExeName}" > nul',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function AutostartTaskExists: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\schtasks.exe'), '/query /tn "EcoMonitor"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if not EcoMonitorIsRunning then
    exit;

  if SuppressibleMsgBox('EcoMonitor e'' in esecuzione e i suoi file non si possono sostituire.'
      + #13#10#13#10 + 'Chiuderlo ora? I dati non ancora scritti, al massimo un minuto,'
      + #13#10 + 'andranno persi. In alternativa: Esci dall''area di notifica, poi riprova.',
      mbConfirmation, MB_YESNO, IDYES) = IDYES then
  begin
    ShellExec('runas', 'taskkill.exe', '/f /im {#AppExeName}', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
    Sleep(1500);
  end;

  if EcoMonitorIsRunning then
    Result := 'EcoMonitor e'' ancora in esecuzione. Chiuderlo con Esci dall''area di notifica'
      + #13#10 + 'e riavviare l''installazione.';
end;

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

// Registrazione e rimozione dell'avvio automatico
function RunAutostartScript(const Arguments: string): Boolean;
var
  ResultCode: Integer;
  ScriptPath: string;
begin
  ScriptPath := ExpandConstant('{app}\autostart.ps1');
  Result := ShellExec('runas', 'powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath + '" ' + Arguments,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function HiddenStartArgument: string;
begin
  if WizardIsTaskSelected('autostart\background') then
    Result := ' -Arguments "--background"'
  else
    Result := '';
end;

function CurrentUserId: string;
begin
  Result := ExpandConstant('{%USERDOMAIN}') + '\' + ExpandConstant('{username}');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    MarkShortcutAsElevated(ExpandConstant('{autoprograms}\{#AppName} (amministratore).lnk'));
    if WizardIsTaskSelected('autostart') then
      if not RunAutostartScript('-Register -TargetPath "' + ExpandConstant('{app}\{#AppExeName}')
          + '" -UserId "' + CurrentUserId + '"' + HiddenStartArgument) then
        SuppressibleMsgBox('Avvio automatico non configurato: elevazione rifiutata o non riuscita.' + #13#10 +
          'L' + #39 + 'applicazione resta installata e avviabile dal menu Start.',
          mbInformation, MB_OK, IDOK);
  end;
end;

// la disinstallazione lasciava vivi i processi in esecuzione
function InitializeUninstall: Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  if not EcoMonitorIsRunning then
    exit;

  if SuppressibleMsgBox('EcoMonitor e'' in esecuzione e i suoi file non si possono rimuovere.'
      + #13#10#13#10 + 'Chiuderlo ora? I dati non ancora scritti, al massimo un minuto,'
      + #13#10 + 'andranno persi. In alternativa: Esci dall''area di notifica, poi riprova.',
      mbConfirmation, MB_YESNO, IDYES) = IDYES then
  begin
    ShellExec('runas', 'taskkill.exe', '/f /im {#AppExeName}', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
    Sleep(1500);
  end;

  if EcoMonitorIsRunning then
  begin
    SuppressibleMsgBox('EcoMonitor e'' ancora in esecuzione: disinstallare adesso lascerebbe'
      + #13#10 + 'i suoi file sul disco. Chiuderlo con Esci dall''area di notifica'
      + #13#10 + 'e riavviare la disinstallazione.', mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    if RegValueExists(HKCU, 'Software\EcoMonitor', 'Autostart') then
      if not RunAutostartScript('-Unregister') then
        SuppressibleMsgBox('Non sono riuscito a togliere l' + #39 + 'avvio automatico.' + #13#10 +
          'Rimuoverlo da Utilita' + #39 + ' di pianificazione, attivita' + #39 + ' EcoMonitor.',
          mbInformation, MB_OK, IDOK);

  if CurUninstallStep = usPostUninstall then
    SuppressibleMsgBox('I dati misurati restano in:' + #13#10 +
           ExpandConstant('{localappdata}\ecomonitor') + #13#10#13#10 +
           'Cancellarli a mano se non servono piu''.', mbInformation, MB_OK, IDOK);
end;
