"""
card_wifi.py — 리더에 꽂힌 Eye-Fi X2 카드의 Wi-Fi 네트워크 조회/테스트/등록/삭제.

클라우드·공식앱 불필요: EyeFiCardWifi.ps1 헬퍼(32bit, 공식 EyeFiCard.dll 사용)를 호출.
공식 수신앱의 P/Invoke 선언과 IL 을 해독해 확정한 시그니처/동작을 그대로 따른다:
  - 스캔망 레코드 = SSID[33]+RSSI+flags(35B), 설정망 = SSID[33](33B)
  - Add/Test 의 authType = 카드 스캔 결과의 flags 바이트 (공식앱과 동일)
사전 조건: 공식 EyeFiX2Receiver 종료, 카드가 리더에 꽂혀 있을 것.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "EyeFiCardWifi.ps1")


class CardWifiError(Exception):
    pass


def _run(action: str, ssid: str | None = None, key: str | None = None,
         auth: int | None = None, drive: str | None = None, timeout: int = 150) -> dict:
    if sys.platform != "win32":
        raise CardWifiError("카드 Wi-Fi 기능은 Windows 전용입니다(32bit EyeFiCard.dll).")
    if not os.path.exists(HELPER):
        raise CardWifiError(f"헬퍼 없음: {HELPER}")

    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", HELPER, "-Action", action]
    if ssid:
        args += ["-Ssid", ssid]
    if key:
        args += ["-Key", key]
    if auth is not None:
        args += ["-Auth", str(auth)]
    if drive:
        args += ["-Drive", drive]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise CardWifiError("카드 응답 시간 초과. 리더/카드를 확인하세요.")

    out = (proc.stdout or "").strip()
    line = ""
    for ln in reversed(out.splitlines()):
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            line = ln
            break
    if not line:
        err = (proc.stderr or "").strip() or out or "출력 없음"
        raise CardWifiError(f"실행 실패: {err}")
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        raise CardWifiError(f"응답 파싱 실패: {line[:200]}")
    if not data.get("ok"):
        e = CardWifiError(data.get("error", "알 수 없는 오류"))
        e.data = data  # 스캔 목록 등 부가정보 (GUI 표시용)
        raise e
    return data


# 순수 파이썬 메일박스 백엔드를 '먼저' 시도(공식SW·DLL 불필요). 실패 시:
#  - 얼린 배포본(frozen): PS/DLL 미포함이라 폴백 불가 → 메일박스의 실제 오류를 그대로 알림
#  - 소스/공식SW 있는 PC: 기존 DLL(PowerShell) 경로로 폴백
def _mailbox_or_ps(fn, ps_action: str, **ps_kwargs):
    mailbox_err = None
    try:
        from . import card_mailbox
        return fn(card_mailbox)
    except Exception as e:
        mailbox_err = e  # 진짜 원인 보존(카드 없음/응답없음 등)
    frozen = getattr(sys, "frozen", False)
    if not frozen and os.path.exists(HELPER):
        return _run(ps_action, **ps_kwargs)   # 소스: DLL 폴백
    detail = str(mailbox_err) if mailbox_err else "알 수 없는 오류"
    e = CardWifiError(f"{detail}\n(리더에 Eye-Fi 카드가 꽂혀 있는지 확인하세요)")
    if mailbox_err is not None and hasattr(mailbox_err, "data"):
        e.data = mailbox_err.data
    raise e


def list_networks(drive: str | None = None) -> dict:
    """카드의 설정된 망 + 스캔된 망. {mac, configured:[{ssid}], scanned:[{ssid,rssi,flags}]}"""
    return _mailbox_or_ps(lambda m: m.list_networks(drive), "list", drive=drive)


def card_info(drive: str | None = None) -> dict:
    """{mac, uploadkey, ...}"""
    return _mailbox_or_ps(lambda m: m.read_card_info(drive), "info", drive=drive)


def test_network(ssid: str, key: str, auth: int | None = None, drive: str | None = None) -> dict:
    """카드 접속 테스트. 메일박스엔 test 명령이 없어, 등록으로 바로 시도하도록 안내(배포본).
    소스/공식SW 있는 PC 에선 DLL 의 TestNetworkSettings 사용."""
    if not getattr(sys, "frozen", False) and os.path.exists(HELPER):
        return _run("test", ssid=ssid, key=key, auth=auth, drive=drive)
    raise CardWifiError("연결 테스트는 배포본에서 미지원입니다.\n'카드에 등록'을 바로 눌러 등록하세요.")


def add_network(ssid: str, key: str, auth: int | None = None, drive: str | None = None) -> dict:
    """카드에 망 등록. 반환 registered=True 면 등록 직후 재조회에서 확인됨."""
    return _mailbox_or_ps(lambda m: m.add_network(ssid, key, drive), "add",
                          ssid=ssid, key=key, auth=auth, drive=drive)


def delete_network(ssid: str, drive: str | None = None) -> dict:
    """카드에서 망 삭제. 반환 removed=True 면 삭제 확인됨."""
    return _mailbox_or_ps(lambda m: m.delete_network(ssid, drive), "delete",
                          ssid=ssid, drive=drive)
