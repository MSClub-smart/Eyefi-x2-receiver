"""
card_reader.py — 리더에 꽂힌 Eye-Fi X2 카드를 읽어 값(MAC/UploadKey 등)을 돌려준다.

Windows 전용: 32비트 EyeFiCard.dll 을 쓰는 Read-EyeFiCard.ps1 헬퍼를 호출하고 JSON 파싱.
공식 EyeFiX2Receiver 가 켜져 있으면 카드 접근이 막히므로 사전에 종료 필요.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Read-EyeFiCard.ps1")


class CardReadError(Exception):
    pass


def collect_diagnostics(drive: str | None = None, app_version: str = "") -> str:
    """카드 진단 로그(사람이 읽는 텍스트)를 수집. 순수 파이썬 메일박스만 사용.
    등록 실패 원인(구펌웨어/개체/세대)을 원격 분석하도록 사용자가 저장·전송하는 용도."""
    from . import card_mailbox
    return card_mailbox.collect_diagnostics(drive, app_version=app_version)


def read_firmware(drive: str | None = None) -> dict:
    """카드 펌웨어 버전 확인: {mac, firmware, exposes_key}. 순수 파이썬 메일박스."""
    from . import card_mailbox
    return card_mailbox.read_firmware(drive)


def read_card(drive: str | None = None, timeout: int = 40, probe_formatted: bool = False) -> dict:
    """
    카드를 읽어 dict 반환: {mac, uploadkey, actcode, ssid, raw, drive}.
    순수 파이썬 메일박스(공식SW 불필요)를 먼저 시도, 실패 시 DLL(PowerShell) 폴백.
    probe_formatted=True(수동 등록 버튼에서만) 면 카메라 포맷으로 EYEFI 폴더가 지워진
    카드도 메일박스를 재생성해 복구 시도. 실패 시 CardReadError. Windows 아니면 CardReadError.
    """
    # 1순위: 순수 파이썬 메일박스 (DLL·공식앱 불필요) — 배포본의 정식 경로
    mailbox_err = None
    try:
        from . import card_mailbox
        return card_mailbox.read_card_info(drive, probe_formatted=probe_formatted)
    except Exception as e:
        mailbox_err = e  # 진짜 원인 보존(리더에 카드 없음 등) — 숨기지 않음

    # 2순위: DLL(PowerShell) 헬퍼 — 소스/공식SW 있는 PC 에서만. 얼린 배포본엔 미포함.
    frozen = getattr(sys, "frozen", False)
    if frozen or sys.platform != "win32" or not os.path.exists(HELPER):
        # 폴백 불가 → 메일박스의 실제 원인을 그대로 안내('헬퍼 없음' 오해 방지)
        detail = str(mailbox_err) if mailbox_err else "알 수 없는 오류"
        raise CardReadError(f"카드 판독 실패: {detail}\n(리더에 Eye-Fi 카드가 꽂혀 있는지 확인하세요)")

    args = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", HELPER,
    ]
    if drive:
        args += ["-Drive", drive]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise CardReadError("판독 시간 초과. 리더/카드를 확인하세요.")

    out = (proc.stdout or "").strip()
    # 헬퍼는 JSON 한 줄만 출력. 여러 줄이면 마지막 JSON 줄 사용.
    line = ""
    for ln in reversed(out.splitlines()):
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            line = ln
            break
    if not line:
        err = (proc.stderr or "").strip() or out or "출력 없음"
        raise CardReadError(f"판독 실패: {err}")
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        raise CardReadError(f"응답 파싱 실패: {line[:200]}")
    if not data.get("ok"):
        raise CardReadError(data.get("error", "알 수 없는 오류"))
    return data
