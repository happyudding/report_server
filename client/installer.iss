; Inno Setup script — Honey 클라이언트 설치본 (HoneySetup-x.y.z.exe)
; 빌드: ISCC.exe installer.iss  (먼저 pyinstaller 로 dist\Honey\ onedir 생성 필요)
; 설치 후 일반 앱처럼 시작메뉴/바탕화면 아이콘 + 제어판 제거 항목 등록.
; 관리자 권한 불필요(per-user, %LOCALAPPDATA%\Programs\Honey) → 자동 업데이트 시 쓰기 가능.

#define MyAppName "Honey"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "COINAPI"
#define MyAppExeName "Honey.exe"

[Setup]
AppId={{B7E1B2C0-1A2B-4C3D-8E9F-0A1B2C3D4E5F}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
OutputDir=installer_dist
OutputBaseFilename=HoneySetup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 생성"; GroupDescription: "추가 아이콘:"

[Files]
; dist\Honey\ (onedir) 전체를 설치 폴더로 복사
Source: "dist\Honey\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{#MyAppName} 실행"; Flags: nowait postinstall skipifsilent
