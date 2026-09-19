"""
autostart.py — 부팅 시 자동시작 등록/해제 (Windows, 사용자 범위 HKCU\\...\\Run)

관리자 권한 불필요. 공식 앱의 자동시작(값 이름 'Eye-FiX2')과 별개의 'EyeFiReceiver' 항목을 쓴다.
자동시작 명령은 콘솔 없는 pythonw + run_tray.pyw (트레이로 조용히 기동).
"""
from __future__ import annotations
import os
import sys

try:
    import winreg
except ImportError:  # 비-Windows
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "EyeFiReceiver"


def _pkg_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _pythonw() -> str:
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pyw if os.path.exists(pyw) else sys.executable


def autostart_command() -> str:
    """레지스트리에 넣을 실행 명령 문자열."""
    if getattr(sys, "frozen", False):
        # 배포 .exe: 실행파일 자체가 트레이 모드로 기동(EyeFiReceiver_app.py 진입점)
        return f'"{sys.executable}"'
    launcher = os.path.join(_pkg_dir(), "run_tray.pyw")
    return f'"{_pythonw()}" "{launcher}"'


def is_supported() -> bool:
    return winreg is not None and sys.platform == "win32"


def is_enabled() -> bool:
    if not is_supported():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, APP_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def enable() -> None:
    if not is_supported():
        raise RuntimeError("자동시작은 Windows에서만 지원됩니다.")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())


def disable() -> None:
    if not is_supported():
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def official_autostart_enabled() -> bool:
    """공식 앱(EyeFiX2) 자동시작이 켜져 있는지 — 충돌 안내용."""
    if not is_supported():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, "Eye-FiX2")
            return bool(val)
    except (FileNotFoundError, OSError):
        return False
