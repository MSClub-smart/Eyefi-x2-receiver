; EyeFiReceiver.iss — Inno Setup 설치 스크립트 (클리닉 Eye-Fi 수신기 배포용)
; 컴파일: ISCC.exe EyeFiReceiver.iss  →  Output\EyeFiReceiver-Setup.exe
; 전제: 먼저 PyInstaller 빌드(dist\EyeFiReceiver\) 가 있어야 함.

#define AppName "Eye-Fi Receiver"
#define AppVer "1.1.8"
; AppVer 는 EyeFiReceiver\__init__.py 의 __version__ 과 맞출 것
#define AppPublisher "Minsik Choi"
#define AppContact "msclub@naver.com (카카오톡 ID: msclub77)"
#define AppExe "EyeFiReceiver.exe"

[Setup]
AppId={{8F3E2A10-EYEF-4C21-9A55-EYEFIRECV0001}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher={#AppPublisher}
AppContact={#AppContact}
AppComments=Made by Minsik Choi — 문의·업데이트 요청: msclub@naver.com / 카카오톡 ID msclub77
AppCopyright=Made by Minsik Choi
VersionInfoCompany={#AppPublisher}
; 설치 첫 화면(웰컴 페이지)에 제작자 표기를 보여주기 위해 명시적으로 켠다(Inno 6 기본은 숨김)
DisableWelcomePage=no
DefaultDirName={autopf}\EyeFiReceiver
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=EyeFiReceiver-Setup-v{#AppVer}
Compression=lzma2
SolidCompression=yes
; 사용자 단위 설치: UAC 불필요, HKCU 자동시작이 실사용자에 정확히 적용됨.
; {autopf}=%LOCALAPPDATA%\Programs, {autodesktop}/{group} 모두 사용자 범위로 매핑.
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\Autorun\EyeFi.ico
UninstallDisplayIcon={app}\{#AppExe}
WizardStyle=modern

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Messages]
; 설치 첫 화면(웰컴)에 제작자·연락처 표기
korean.WelcomeLabel2=이 프로그램은 [name/ver] 을(를) 이 컴퓨터에 설치합니다.%n%nMade by Minsik Choi%n문의·업데이트 요청: msclub@naver.com%n카카오톡 ID: msclub77%n%n계속하려면 [다음]을 클릭하세요.

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 바로가기 만들기"; GroupDescription: "추가 아이콘:"
Name: "autostart"; Description: "부팅 시 자동시작 (권장 — 상시 사진 수신)"; GroupDescription: "시작 옵션:"

[Files]
; PyInstaller 산출물 전체를 통째로 설치
Source: "..\dist\EyeFiReceiver\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; 부팅 자동시작(사용자 범위). 값 이름은 앱 내부 autostart 토글과 동일('EyeFiReceiver')하여 일관.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
    ValueName: "EyeFiReceiver"; ValueData: """{app}\{#AppExe}"""; \
    Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "지금 Eye-Fi Receiver 실행"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 설치 폴더 내 잔여물 정리(사용자 데이터 %APPDATA%\EyeFiReceiver 는 보존 — 삭제 안 함)
Type: filesandordirs; Name: "{app}"
