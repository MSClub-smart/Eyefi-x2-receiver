# 배포 설치본 빌드 방법

**Made by Minsik Choi** — 문의·업데이트 요청: msclub@naver.com / 카카오톡 ID: msclub77
(제작자 표기는 설치 첫 화면(웰컴)·프로그램 추가/제거 게시자·이 문서·README 에 들어감)

클리닉 Eye-Fi 수신기를 **공식 소프트웨어 없는 깨끗한 PC에도** 설치 가능한 `.exe` 설치본으로 만든다.
카드 등록·Wi-Fi 기능은 순수 파이썬(`card_mailbox.py`)으로 동작하므로 EyeFiCard.dll 이 필요 없다.

## 사전 준비 (한 번만)
- Python 3.12 + `pip install pystray pillow pyinstaller`
- Inno Setup 6 (`winget install --id JRSoftware.InnoSetup`)
  - ISCC.exe 위치: `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`

## 빌드 2단계

### 1) PyInstaller 로 .exe 묶기 (C:\EyeFiReceiver 에서)
```
python -m PyInstaller --noconfirm --clean --windowed --name EyeFiReceiver ^
  --icon "Autorun/EyeFi.ico" --add-data "Autorun/EyeFi.ico;Autorun" ^
  --hidden-import pystray._win32 --paths . EyeFiReceiver_app.py
```
→ `dist\EyeFiReceiver\EyeFiReceiver.exe` (폴더 통째로 배포 단위)

### 2) Inno Setup 으로 설치 마법사 만들기
```
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\EyeFiReceiver.iss
```
→ `installer\Output\EyeFiReceiver-Setup.exe`  ← **이 파일 하나만 배포하면 됨**

## 설치본 특징
- **사용자 단위 설치**: UAC(관리자) 불필요, `%LOCALAPPDATA%\Programs\EyeFiReceiver` 에 설치
- **데이터 위치**: `%APPDATA%\EyeFiReceiver\` (cards.json·스풀·로그·장부) — 소스 실행과 자동 분리
- **자동시작**: 설치 시 체크박스(HKCU Run) 또는 앱 내 "부팅 자동시작" 토글 (둘 다 값 이름 'EyeFiReceiver')
- **제거**: 프로그램 추가/제거에서 제거. 사용자 데이터(%APPDATA%)는 보존됨
- 파이썬 설치 불필요(번들). 공식 Eye-Fi 소프트웨어 불필요

## 설치 직후 첫 사용
1. 트레이 아이콘 → 창 열기
2. **기본 저장폴더** 지정 (사진 받을 위치)
3. 카드 등록: 리더에 카드 꽂고 **🔑 카드를 앱에 등록** → **📶 카드 Wi-Fi** 로 Wi-Fi 써넣기
4. 서버 시작(또는 자동시작 켜기)

## 주의
- **설치본을 이 클리닉 메인 PC(현재 소스 버전 상시 운영 중)에 설치하지 말 것** — 같은 포트(59278)
  충돌. 설치본은 새/추가 PC 용. 메인 PC 를 설치본으로 전환하려면 소스 자동시작 해제 후 전환.
