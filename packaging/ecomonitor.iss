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

#define PawnIOUrl "https://github.com/namazso/PawnIO.Setup/releases/download/2.2.0/PawnIO_setup.exe"
#define PawnIOSha256 "1f519a22e47187f70a1379a48ca604981c4fcf694f4e65b734aaa74a9fba3032"

[Setup]
AppId={{ef2ed1a8-13dd-4d13-8813-a84e088939a6}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Tesi triennale di green computing
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
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
Type: files; Name: "{autoprograms}\{#AppName} (amministratore).lnk"

[Files]
Source: "dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "autostart.ps1"; DestDir: "{app}"
Source: "elevated_setup.ps1"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Tasks]
Name: "pawnio"; Description: "Installa il driver PawnIO, che serve a misurare la CPU"; \
    GroupDescription: "Misura:"; Check: not PawnIOInstalled
Name: "autostart"; Description: "Avvia EcoMonitor all'accesso"; \
    GroupDescription: "Avvio automatico:"
Name: "autostart\background"; Description: "Parti nell'area di notifica, senza aprire la finestra"; \
    GroupDescription: "Avvio automatico:"; Flags: unchecked

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Comment: "Monitoraggio di energia e CO2eq"

[Registry]
Root: HKCU; Subkey: "Software\EcoMonitor"; ValueType: dword; ValueName: "Autostart"; \
    ValueData: 1; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: autostart
Root: HKCU; Subkey: "Software\EcoMonitor"; ValueType: dword; ValueName: "Autostart"; \
    ValueData: 0; Flags: uninsdeletevalue uninsdeletekeyifempty; Tasks: not autostart
Root: HKCU; Subkey: "Software\Classes\AppUserModelId\io.github.caponifederico.ecomonitor"; \
    Flags: dontcreatekey uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\ecomonitor"; Flags: dontcreatekey uninsdeletekey

[Run]
Filename: "{sys}\schtasks.exe"; Parameters: "/run /tn ""{code:LauncherTaskName}"""; \
    Description: "Avvia {#AppName}"; Flags: postinstall skipifsilent runhidden; \
    Check: LauncherTaskExists
Filename: "{app}\{#AppExeName}"; Description: "Avvia {#AppName}"; \
    Flags: nowait postinstall skipifsilent runasoriginaluser; Check: not LauncherTaskExists

[Code]
var
  DownloadPage: TDownloadWizardPage;
  PawnIODownloaded: Boolean;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), nil);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID <> wpReady) or not WizardIsTaskSelected('pawnio') then
    exit;

  PawnIODownloaded := False;
  DownloadPage.Clear;
  DownloadPage.Add('{#PawnIOUrl}', 'PawnIO_setup.exe', '{#PawnIOSha256}');
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
      PawnIODownloaded := True;
    except
      Log('PawnIO non scaricato: ' + GetExceptionMessage);
    end;
  finally
    DownloadPage.Hide;
  end;
end;

function EcoMonitorIsRunning: Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'),
    '/c tasklist /fi "imagename eq {#AppExeName}" | find /i "{#AppExeName}" > nul',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function CurrentUserId: string;
begin
  Result := ExpandConstant('{%USERDOMAIN}') + '\' + ExpandConstant('{username}');
end;

function TaskName(Param: string): string;
begin
  Result := 'EcoMonitor (' + ExpandConstant('{%USERDOMAIN}') + '-' + ExpandConstant('{username}') + ')';
end;

function LauncherTaskName(Param: string): string;
begin
  Result := 'EcoMonitor avvio (' + ExpandConstant('{%USERDOMAIN}') + '-' + ExpandConstant('{username}') + ')';
end;

function TaskExists(const Name: string): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\schtasks.exe'), '/query /tn "' + Name + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function AutostartTaskExists: Boolean;
begin
  Result := TaskExists(TaskName(''));
end;

function LauncherTaskExists: Boolean;
begin
  Result := TaskExists(LauncherTaskName(''));
end;

const
  PerUserUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{ef2ed1a8-13dd-4d13-8813-a84e088939a6}_is1';

function InitializeSetup: Boolean;
begin
  Result := True;
  if RegKeyExists(HKCU, PerUserUninstallKey) then
  begin
    SuppressibleMsgBox('C''e'' ancora la versione precedente di EcoMonitor, installata solo'
      + #13#10 + 'per questo utente. Disinstallarla da Impostazioni > App, poi riavviare'
      + #13#10 + 'questa installazione. Le misure gia'' raccolte restano.',
      mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

function PawnIOInstalled: Boolean;
begin
  Result := RegKeyExists(HKLM64, 'SYSTEM\CurrentControlSet\Services\PawnIO');
end;


function PowerShellPath: string;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

// L'app gira elevata e Setup no
procedure CloseEcoMonitor;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/f /im {#AppExeName}', '', SW_HIDE,
    ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
end;

function CloseQuestion(const Action: string): string;
begin
  Result := 'EcoMonitor e'' in esecuzione e i suoi file non si possono ' + Action + '.'
    + #13#10#13#10 + 'Chiuderlo ora? I dati non ancora scritti, al massimo un minuto,'
    + #13#10 + 'andranno persi. In alternativa: Esci dall''area di notifica, poi riprova.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not EcoMonitorIsRunning then
    exit;

  if SuppressibleMsgBox(CloseQuestion('sostituire'), mbConfirmation, MB_YESNO, IDYES) = IDYES then
    CloseEcoMonitor;

  if EcoMonitorIsRunning then
    Result := 'EcoMonitor e'' ancora in esecuzione. Chiuderlo con Esci dall''area di notifica'
      + #13#10 + 'e riavviare l''installazione.';
end;

// Registrazione e rimozione dell'avvio automatico
function RunAutostartScript(const Arguments: string): Boolean;
var
  ResultCode: Integer;
  ScriptPath: string;
begin
  ScriptPath := ExpandConstant('{app}\autostart.ps1');
  Result := Exec(PowerShellPath,
    '-NoProfile -ExecutionPolicy Bypass -File "' + ScriptPath + '" ' + Arguments,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function HiddenStartArgument: string;
begin
  if WizardIsTaskSelected('autostart\background') then
    Result := ' -AutostartArguments "--background"'
  else
    Result := '';
end;

const
  PawnIOFailed = 1;
  AutostartFailed = 2;
  LauncherFailed = 8;
  StaleAutostartFailed = 16;

procedure ReportPawnIOFailure;
begin
  SuppressibleMsgBox('Driver PawnIO non installato.' + #13#10 +
    'EcoMonitor stimera' + #39 + ' il consumo della CPU invece di misurarlo;' + #13#10 +
    'il driver si puo' + #39 + ' installare in seguito da pawnio.eu.',
    mbInformation, MB_OK, IDOK);
end;

procedure ReportAutostartFailure;
begin
  SuppressibleMsgBox('Avvio automatico non configurato.' + #13#10 +
    'L' + #39 + 'applicazione resta installata e avviabile dal menu Start.',
    mbInformation, MB_OK, IDOK);
end;

procedure ReportLauncherFailure;
begin
  SuppressibleMsgBox('Avvio senza conferma non configurato.' + #13#10 +
    'EcoMonitor funziona, ma ogni avvio dal menu Start chiedera' + #39 + ' la conferma' + #13#10 +
    'di Windows. Ripetere l' + #39 + 'installazione per sistemarlo.',
    mbInformation, MB_OK, IDOK);
end;

procedure RunElevatedSetup;
var
  Arguments, Target, User: string;
  ResultCode: Integer;
  WantsPawnIO, WantsAutostart: Boolean;
begin
  WantsPawnIO := WizardIsTaskSelected('pawnio') and not PawnIOInstalled;
  WantsAutostart := WizardIsTaskSelected('autostart');
  Target := ' -AutostartScript "' + ExpandConstant('{app}\autostart.ps1') + '"'
    + ' -TargetPath "' + ExpandConstant('{app}\{#AppExeName}') + '"';
  User := ' -UserId "' + CurrentUserId + '"';

  Arguments := ' -RegisterLauncher -RemoveStaleAutostart' + Target + User;
  if WantsPawnIO then
  begin
    Arguments := Arguments + ' -InstallPawnIO';
    if PawnIODownloaded then
      Arguments := Arguments + ' -PawnIOSetup "' + ExpandConstant('{tmp}\PawnIO_setup.exe') + '"';
  end;
  if WantsAutostart then
    Arguments := Arguments + ' -RegisterAutostart' + HiddenStartArgument;

  if not Exec(PowerShellPath,
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{tmp}\elevated_setup.ps1') + '"' + Arguments,
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    // PowerShell non partito: non e' stato fatto niente
    if WantsPawnIO then
      ReportPawnIOFailure;
    if WantsAutostart then
      ReportAutostartFailure;
    ReportLauncherFailure;
    exit;
  end;

  if WantsPawnIO and (((ResultCode and PawnIOFailed) <> 0) or not PawnIOInstalled) then
    ReportPawnIOFailure;
  if WantsAutostart and (((ResultCode and AutostartFailed) <> 0) or not AutostartTaskExists) then
    ReportAutostartFailure;
  if ((ResultCode and LauncherFailed) <> 0) or not LauncherTaskExists then
    ReportLauncherFailure;
  if ((ResultCode and StaleAutostartFailed) <> 0) and not WantsAutostart then
    SuppressibleMsgBox('E'' rimasto l''avvio automatico di una versione precedente.' + #13#10 +
      'Rimuoverlo da Utilita' + #39 + ' di pianificazione, attivita' + #39 + ' ' + TaskName('') + '.',
      mbInformation, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    RunElevatedSetup;
  end;
end;

// la disinstallazione lasciava vivi i processi in esecuzione
function InitializeUninstall: Boolean;
begin
  Result := True;
  if not EcoMonitorIsRunning then
    exit;

  if SuppressibleMsgBox(CloseQuestion('rimuovere'), mbConfirmation, MB_YESNO, IDYES) = IDYES then
    CloseEcoMonitor;

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
  begin
    if AutostartTaskExists then
      if not RunAutostartScript('-Unregister -UserId "' + CurrentUserId + '"') then
        SuppressibleMsgBox('Non sono riuscito a togliere l' + #39 + 'avvio automatico.' + #13#10 +
          'Rimuoverlo da Utilita' + #39 + ' di pianificazione, attivita' + #39 + ' ' + TaskName('') + '.',
          mbInformation, MB_OK, IDOK);
    if LauncherTaskExists then
      if not RunAutostartScript('-Unregister -Launcher -UserId "' + CurrentUserId + '"') then
        SuppressibleMsgBox('Non sono riuscito a togliere l' + #39 + 'attivita' + #39 + ' di lancio.' + #13#10 +
          'Rimuoverla da Utilita' + #39 + ' di pianificazione, attivita' + #39 + ' ' + LauncherTaskName('') + '.',
          mbInformation, MB_OK, IDOK);
  end;

  if CurUninstallStep = usPostUninstall then
    SuppressibleMsgBox('I dati misurati restano in:' + #13#10 +
           ExpandConstant('{localappdata}\ecomonitor') + #13#10#13#10 +
           'Cancellarli a mano se non servono piu''.', mbInformation, MB_OK, IDOK);
end;
