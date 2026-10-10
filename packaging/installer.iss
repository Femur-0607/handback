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
Filename: "{app}\handback.exe"; Parameters: "setup --yes"; Flags: runhidden waituntilterminated; StatusMsg: "에이전트 스킬을 설치하는 중..."
Filename: "{app}\handback.exe"; Parameters: "dashboard --autostart on"; Flags: runhidden waituntilterminated; Tasks: autostart
Filename: "{app}\handback-dashboard.exe"; Description: "handback 실행"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\handback.exe"; Parameters: "dashboard --autostart off"; Flags: runhidden waituntilterminated; RunOnceId: "AutostartOff"
Filename: "{app}\handback.exe"; Parameters: "uninstall-hooks"; Flags: runhidden waituntilterminated; RunOnceId: "UninstallHooks"
