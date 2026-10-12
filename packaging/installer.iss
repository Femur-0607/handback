; handback installer (Inno Setup 6).
; Build: ISCC /DAppVersion=<ver> /DSourceDir=<dist\handback> /DOutDir=<dir> packaging\installer.iss
#ifndef AppVersion
  #error Pass /DAppVersion=<version>
#endif
#ifndef SourceDir
  #error Pass /DSourceDir=<PyInstaller onedir folder>
#endif
#ifndef WizardImages
  #error Pass /DWizardImages=<folder from make-wizard-images.ps1>
#endif
#ifndef OutDir
  #define OutDir "."
#endif

[Setup]
AppId={{6F1C2A58-3B7E-4D0A-9C41-7A5E8B2D1F63}
AppName=handback
AppVersion={#AppVersion}
AppVerName=handback {#AppVersion}
AppPublisher=Femur0607
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\handback
DefaultGroupName=handback
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableWelcomePage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile={#SourcePath}\..\handback\assets\icon.ico
UninstallDisplayIcon={app}\handback-dashboard.exe
UninstallDisplayName=handback
OutputDir={#OutDir}
OutputBaseFilename=handback-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
; The [Code] section adds {app} to the per-user PATH; broadcast the change to running shells.
ChangesEnvironment=yes
RestartApplications=no
WizardStyle=modern dark
ShowLanguageDialog=no
WizardImageFile={#WizardImages}\wizard-banner-1x.bmp,{#WizardImages}\wizard-banner-2x.bmp
WizardSmallImageFile={#WizardImages}\wizard-small-1x.bmp,{#WizardImages}\wizard-small-2x.bmp

[Languages]
#ifexist "compiler:Languages\Korean.isl"
Name: "ko"; MessagesFile: "compiler:Languages\Korean.isl"
#else
Name: "en"; MessagesFile: "compiler:Default.isl"
#endif

[Messages]
WizardSelectTasks=설치 옵션
SelectTasksDesc=원하는 항목을 고르세요.
SelectTasksLabel2=
FinishedHeadingLabel=설치 완료
FinishedLabelNoIcons=handback이 설치되었습니다.
FinishedLabel=handback이 설치되었습니다.
ClickFinish=마침을 눌러 닫으세요.

[Tasks]
Name: "desktopicon"; Description: "바탕화면 아이콘"; Flags: unchecked
Name: "autostart"; Description: "Windows 시작 시 실행"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\handback"; Filename: "{app}\handback-dashboard.exe"; AppUserModelID: "Femur0607.handback.dashboard"
Name: "{autodesktop}\handback"; Filename: "{app}\handback-dashboard.exe"; AppUserModelID: "Femur0607.handback.dashboard"; Tasks: desktopicon

[Run]
Filename: "{app}\handback.exe"; Parameters: "setup --yes"; Flags: runhidden waituntilterminated; StatusMsg: "스킬과 복구 훅을 설치하는 중..."
Filename: "{app}\handback.exe"; Parameters: "dashboard --autostart on"; Flags: runhidden waituntilterminated; Tasks: autostart
Filename: "{app}\handback-dashboard.exe"; Description: "handback 실행"; Flags: nowait postinstall skipifsilent
; In-app update runs silently with /RELAUNCH=1, so bring the dashboard back.
Filename: "{app}\handback-dashboard.exe"; Flags: nowait; Check: RelaunchRequested

[UninstallRun]
Filename: "{app}\handback.exe"; Parameters: "dashboard --autostart off"; Flags: runhidden waituntilterminated; RunOnceId: "AutostartOff"
Filename: "{app}\handback.exe"; Parameters: "uninstall-hooks --agents codex,claude,antigravity"; Flags: runhidden waituntilterminated; RunOnceId: "UninstallHooks"
Filename: "{app}\handback.exe"; Parameters: "uninstall-skills"; Flags: runhidden waituntilterminated; RunOnceId: "UninstallSkills"

[Code]
function RelaunchRequested: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:RELAUNCH|0}') = '1');
end;

// Per-user PATH (HKCU\Environment\Path): append {app} once on install, remove exactly that entry on uninstall.
// Works with PrivilegesRequired=lowest because only HKCU is touched.
const
  EnvironmentKey = 'Environment';

function NeedsAddPath(Dir: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths) then
  begin
    Result := True;
    exit;
  end;
  // Compare whole ';'-separated entries case-insensitively, with or without a trailing backslash.
  Result := (Pos(';' + Uppercase(Dir) + ';', ';' + Uppercase(Paths) + ';') = 0)
    and (Pos(';' + Uppercase(Dir) + '\;', ';' + Uppercase(Paths) + ';') = 0);
end;

procedure AddAppToPath;
var
  Paths, Dir: string;
begin
  Dir := ExpandConstant('{app}');
  if not NeedsAddPath(Dir) then
    exit;
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths) then
    Paths := '';
  if (Paths <> '') and (Copy(Paths, Length(Paths), 1) <> ';') then
    Paths := Paths + ';';
  Paths := Paths + Dir;
  if not RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths) then
    Log('Could not add ' + Dir + ' to the user PATH');
end;

procedure RemoveAppFromPath;
var
  Paths, Dir: string;
  P: Integer;
begin
  Dir := ExpandConstant('{app}');
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths) then
    exit;
  // Sentinel semicolons make the first and last entries match like the middle ones.
  Paths := ';' + Paths + ';';
  P := Pos(';' + Uppercase(Dir) + ';', Uppercase(Paths));
  if P = 0 then
    exit;
  Delete(Paths, P, Length(Dir) + 1);  // drop ';' + Dir, keep the following ';'
  Paths := Copy(Paths, 2, Length(Paths) - 2);
  if Paths = '' then
    RegDeleteValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path')
  else if not RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths) then
    Log('Could not remove ' + Dir + ' from the user PATH');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    AddAppToPath;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveAppFromPath;
end;
