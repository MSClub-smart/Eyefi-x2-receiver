"""
EyeFiReceiver_app.py — PyInstaller 배포 빌드용 진입점.

트레이 상주 모드로 기동한다(창 숨김 + 서버 자동시작). 소스의 run_tray.pyw 와 동일 역할이나,
패키지 밖 최상위 스크립트라 PyInstaller 가 EyeFiReceiver 패키지를 정상 분석한다.
"""
import sys

from EyeFiReceiver.__main__ import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "--tray"]
    main()
