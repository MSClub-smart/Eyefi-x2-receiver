#!/usr/bin/env pythonw
"""
run_tray.pyw — 자동시작/트레이 기동용 진입점 (콘솔 없음).
패키지 부모 경로를 sys.path 에 넣고 트레이 모드로 실행한다.
레지스트리 자동시작 항목이 이 파일을 pythonw 로 실행한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from EyeFiReceiver.__main__ import main

sys.argv = [sys.argv[0], "--tray"]
main()
