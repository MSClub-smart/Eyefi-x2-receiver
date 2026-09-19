"""
paths.py — 실행 형태에 따라 '쓰기 가능한 데이터 폴더'와 '읽기 전용 리소스 폴더'를 정한다.

- 소스로 실행(개발/현 클리닉: `python -m EyeFiReceiver`): 데이터=모듈 폴더 그대로 (기존 동작 유지 →
  현재 운영 중인 cards.json/spool/로그를 절대 잃지 않음).
- PyInstaller 로 얼린 .exe(배포 설치본): 데이터=%APPDATA%/EyeFiReceiver (Program Files 는 쓰기 금지라
  반드시 사용자 폴더로), 리소스=PyInstaller 임시 추출 폴더(sys._MEIPASS, 읽기 전용).

이렇게 나눠야 Program Files 설치본이 정상 작동하고, 개발/현 운영 환경은 그대로 유지된다.
"""
from __future__ import annotations
import os
import sys

APP_NAME = "EyeFiReceiver"


def is_frozen() -> bool:
    """PyInstaller 등으로 단일 실행파일화됐는지."""
    return bool(getattr(sys, "frozen", False))


def data_dir() -> str:
    """설정·스풀·로그 등 '쓰기'가 필요한 파일이 들어갈 폴더(없으면 생성)."""
    if is_frozen():
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, APP_NAME)
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def resource_dir() -> str:
    """아이콘·헬퍼 스크립트 등 '읽기 전용' 번들 리소스가 있는 폴더."""
    if is_frozen():
        # PyInstaller: 번들 리소스는 sys._MEIPASS 에 풀린다
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.abspath(__file__))


def data_path(*parts: str) -> str:
    """data_dir() 아래 경로 조립."""
    return os.path.join(data_dir(), *parts)


def resource_path(*parts: str) -> str:
    """resource_dir() 아래 경로 조립."""
    return os.path.join(resource_dir(), *parts)
