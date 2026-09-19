"""
card_mailbox.py — Eye-Fi X2 카드 설정 프로토콜을 '순수 파이썬'으로 구현.

DLL(EyeFiCard.dll) 없이, 카드 마운트의 EYEFI/ 메일박스 파일(REQM/RSPM/REQC/RSPC)을
직접 읽고 써서 카드와 통신한다. 청사진: docs/eyefi-config.c (오픈소스 eyefi-config, GPLv2).
공식 소프트웨어가 없는 깨끗한 PC 에서도 카드 등록·Wi-Fi 설정이 되게 하기 위함.

프로토콜(eyefi-config.c 기준):
  - 각 메일박스 파일은 16384바이트 고정.
  - init: RSPM 0으로 지움 → RSPC 에서 시퀀스(4B LE) 읽어 +1 (0이면 0x1234).
  - 명령: REQM 에 명령바이트 쓰기 → 시퀀스 +1 해서 REQC 에 쓰기 →
          RSPC 가 그 시퀀스를 돌려줄 때까지 폴링(카드가 처리 완료) → RSPM 이 응답.
  - 'o'+서브 = 읽기(정보), 'O'+서브 = 쓰기(설정), 'a'/'d' = 망추가/삭제, 'g' 스캔, 'l' 목록.

Windows 캐시 일관성: 매 접근마다 파일을 새로 열어 O_BINARY 로 읽고, 쓰기 후 flush+fsync.
(카드 펌웨어가 파일을 갱신하므로 핸들 재사용 시 낡은 값이 읽힐 수 있어 항상 새로 연다.)
"""
from __future__ import annotations
import hashlib
import os
import struct
import time

BUF_SIZE = 16384
EYEFI_DIR = "EYEFI"

# card_info_subcommand (docs/eyefi-config.h)
MAC_ADDRESS = 1
FIRMWARE_INFO = 2
CARD_KEY = 3
API_URL = 4
WLAN_DISABLE = 10
TRANSFER_MODE = 17
DIRECT_MODE_SSID = 0x22
DIRECT_MODE_PASS = 0x23   # 카드 인쇄 활성화코드(다이렉트 모드 비번 슬롯)
UPLOAD_KEY = 0xFD

# 진단 로그에 덤프할 토큰 목록 (이름, 서브커맨드)
DIAG_TOKENS = [
    ("MAC(1)", MAC_ADDRESS),
    ("FIRMWARE(2)", FIRMWARE_INFO),
    ("CARD_KEY(3)", CARD_KEY),
    ("API_URL(4)", API_URL),
    ("WLAN_DISABLE(10)", WLAN_DISABLE),
    ("TRANSFER_MODE(17)", TRANSFER_MODE),
    ("DIRECT_SSID(0x22)", DIRECT_MODE_SSID),
    ("ACT_CODE(0x23)", DIRECT_MODE_PASS),
    ("UPLOAD_KEY(0xFD)", UPLOAD_KEY),
]

ESSID_LEN = 32


class CardMailboxError(Exception):
    pass


def _mbox_path(mount: str, name: str) -> str:
    return os.path.join(mount, EYEFI_DIR, name)


_IS_WIN = os.name == "nt"

if _IS_WIN:
    import ctypes
    from ctypes import wintypes

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
    _OPEN_EXISTING = 3
    _FILE_FLAG_NO_BUFFERING = 0x20000000    # OS 캐시 우회 → 카드의 실제 값 읽기/쓰기
    _FILE_FLAG_WRITE_THROUGH = 0x80000000   # 쓰기를 카드에 즉시 반영
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GetDriveTypeW = _kernel32.GetDriveTypeW
    _GetDriveTypeW.restype = wintypes.UINT
    _GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _GetVolumeInformationW = _kernel32.GetVolumeInformationW
    _GetVolumeInformationW.restype = wintypes.BOOL
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.restype = wintypes.HANDLE
    _CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                             wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _ReadFile = _kernel32.ReadFile
    _ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _WriteFile = _kernel32.WriteFile
    _WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]

    def _aligned_buffer(size: int, align: int = 4096):
        """섹터 정렬 버퍼 (NO_BUFFERING 은 정렬된 버퍼·크기 요구)."""
        raw = ctypes.create_string_buffer(size + align)
        addr = ctypes.addressof(raw)
        offset = (align - (addr % align)) % align
        return raw, offset

    def _read_raw(path: str, n: int = BUF_SIZE) -> bytes:
        h = _CreateFileW(path, _GENERIC_READ, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
                         _FILE_FLAG_NO_BUFFERING, None)
        if h == _INVALID_HANDLE or h is None:
            raise CardMailboxError(f"열기 실패: {path} (err {ctypes.get_last_error()})")
        try:
            raw, off = _aligned_buffer(BUF_SIZE)
            read = wintypes.DWORD(0)
            ok = _ReadFile(h, ctypes.byref(raw, off), BUF_SIZE, ctypes.byref(read), None)
            if not ok:
                raise CardMailboxError(f"읽기 실패: {path} (err {ctypes.get_last_error()})")
            return bytes(raw[off:off + read.value])[:n]
        finally:
            _CloseHandle(h)

    def _write_raw(path: str, data: bytes) -> None:
        h = _CreateFileW(path, _GENERIC_WRITE, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
                         _FILE_FLAG_NO_BUFFERING | _FILE_FLAG_WRITE_THROUGH, None)
        if h == _INVALID_HANDLE or h is None:
            raise CardMailboxError(f"열기 실패(쓰기): {path} (err {ctypes.get_last_error()})")
        try:
            raw, off = _aligned_buffer(BUF_SIZE)
            ctypes.memset(ctypes.byref(raw, off), 0, BUF_SIZE)
            for i, b in enumerate(data[:BUF_SIZE]):
                raw[off + i] = b
            wrote = wintypes.DWORD(0)
            ok = _WriteFile(h, ctypes.byref(raw, off), BUF_SIZE, ctypes.byref(wrote), None)
            if not ok:
                raise CardMailboxError(f"쓰기 실패: {path} (err {ctypes.get_last_error()})")
        finally:
            _CloseHandle(h)

    def _is_removable(root: str) -> bool:
        return _GetDriveTypeW(root) == 2  # DRIVE_REMOVABLE

    def _volume_label(root: str) -> str:
        name = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        ser = wintypes.DWORD(); mcl = wintypes.DWORD(); fl = wintypes.DWORD()
        ok = _GetVolumeInformationW(root, name, 261, ctypes.byref(ser),
                                    ctypes.byref(mcl), ctypes.byref(fl), fs, 261)
        return name.value if ok else ""

else:
    def _is_removable(root: str) -> bool:
        return False

    def _volume_label(root: str) -> str:
        return ""

    def _read_raw(path: str, n: int = BUF_SIZE) -> bytes:
        fd = os.open(path, os.O_RDONLY)
        try:
            data = b""
            while len(data) < n:
                chunk = os.read(fd, n - len(data))
                if not chunk:
                    break
                data += chunk
            return data
        finally:
            os.close(fd)

    def _write_raw(path: str, data: bytes) -> None:
        buf = bytearray(BUF_SIZE)
        buf[: len(data)] = data
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.write(fd, bytes(buf))
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)


class CardMailbox:
    """리더에 마운트된 카드 한 장과의 세션. mount 예: 'F:\\\\'."""

    def __init__(self, mount: str, poll_tries: int = 50, poll_interval: float = 0.3):
        self.mount = mount
        self.poll_tries = poll_tries
        self.poll_interval = poll_interval
        self.seq = 0
        self._init_seq()

    def _p(self, name: str) -> str:
        return _mbox_path(self.mount, name)

    def _init_seq(self) -> None:
        # RSPM 지우고, RSPC 의 현재 시퀀스에서 시작
        _write_raw(self._p("RSPM"), b"")
        rspc = _read_raw(self._p("RSPC"), 4)
        seq = struct.unpack("<I", rspc[:4])[0] if len(rspc) >= 4 else 0
        if seq == 0 or seq >= 0xFFFFFF00:
            # 0 = 방금 부트스트랩한 카드, 0xFF.. = 지워진 플래시(공장 초기 카드) → 안전한 시작값
            seq = 0x1234
        self.seq = seq + 1

    def _inc_seq(self) -> None:
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        _write_raw(self._p("REQC"), struct.pack("<I", self.seq))

    def _wait_response(self) -> bytes:
        self._inc_seq()
        for _ in range(self.poll_tries):
            rspc = _read_raw(self._p("RSPC"), 4)
            cur = struct.unpack("<I", rspc[:4])[0] if len(rspc) >= 4 else 0
            if cur == self.seq:
                return _read_raw(self._p("RSPM"))
            time.sleep(self.poll_interval)
        raise CardMailboxError("카드 응답 없음(시퀀스 불일치) — 카드/리더 확인")

    def command(self, data: bytes) -> bytes:
        _write_raw(self._p("REQM"), data)
        return self._wait_response()

    # ---- 정보 읽기 ('o') ----
    def _info(self, subcommand: int) -> bytes:
        return self.command(bytes([ord("o"), subcommand & 0xFF]))

    @staticmethod
    def _pascal(resp: bytes) -> bytes:
        """pascal_string: [len][value...]. value 바이트 반환."""
        if not resp:
            return b""
        n = resp[0]
        return resp[1 : 1 + n]

    def get_mac(self) -> str:
        resp = self._info(MAC_ADDRESS)
        mac = self._pascal(resp)  # mac_address: [len=6][6 bytes]
        if len(mac) < 6:
            raise CardMailboxError("MAC 판독 실패")
        h = mac[:6]
        return "00-18-56-%02x-%02x-%02x" % (h[3], h[4], h[5])

    def get_upload_key(self) -> str:
        resp = self._info(UPLOAD_KEY)
        return self._pascal(resp).decode("ascii", "replace")

    def get_card_key(self) -> str:
        resp = self._info(CARD_KEY)
        return self._pascal(resp).decode("ascii", "replace")

    def get_firmware(self) -> str:
        resp = self._info(FIRMWARE_INFO)
        return self._pascal(resp).decode("latin-1", "replace")

    # ---- 망 목록/스캔 ('l' / 'g') ----
    def list_configured(self) -> list[str]:
        resp = self.command(b"l")
        return self._parse_net_list(resp, has_meta=False)

    def scan(self) -> list[dict]:
        resp = self.command(b"g")
        nets = []
        if not resp:
            return nets
        nr = resp[0]
        # scanned_net: essid[ESSID_LEN] + strength(1) + type(1)
        rec = ESSID_LEN + 2
        for i in range(min(nr, 100)):
            off = 1 + i * rec
            chunk = resp[off : off + rec]
            if len(chunk) < rec:
                break
            essid = chunk[:ESSID_LEN].split(b"\x00", 1)[0].decode("latin-1", "replace")
            strength = struct.unpack("b", chunk[ESSID_LEN : ESSID_LEN + 1])[0]
            ntype = chunk[ESSID_LEN + 1]
            nets.append({"ssid": essid, "strength": strength, "type": ntype})
        return nets

    def _parse_net_list(self, resp: bytes, has_meta: bool) -> list[str]:
        out = []
        if not resp:
            return out
        nr = resp[0]
        rec = ESSID_LEN
        for i in range(min(nr, 100)):
            off = 1 + i * rec
            chunk = resp[off : off + rec]
            if len(chunk) < rec:
                break
            essid = chunk.split(b"\x00", 1)[0].decode("latin-1", "replace")
            if essid:
                out.append(essid)
        return out

    # ---- 망 추가/삭제 ('a' / 'd') ----
    @staticmethod
    def _wpa_key(essid: str, password: str) -> bytes:
        """WPA 키 32바이트. 64 hex 면 raw, 아니면 PBKDF2-SHA1(pass, essid, 4096)."""
        if len(password) == 64 and all(c in "0123456789abcdefABCDEF" for c in password):
            return bytes.fromhex(password)
        return hashlib.pbkdf2_hmac("sha1", password.encode(), essid.encode(), 4096, 32)

    @classmethod
    def build_net_request(cls, cmd: str, essid: str, password: str | None) -> bytes:
        """net_request 구조체(67B): req(1) essid_len(1) essid[32] key{len(1) wpa[32]}."""
        essid_b = essid.encode("latin-1")[:ESSID_LEN]
        buf = bytearray()
        buf += cmd.encode("ascii")
        buf += bytes([len(essid_b)])
        buf += essid_b + b"\x00" * (ESSID_LEN - len(essid_b))
        if password is not None:
            key = cls._wpa_key(essid, password)
            buf += bytes([len(key)]) + key
        else:
            buf += bytes([0]) + b"\x00" * 32
        return bytes(buf)

    def _net_request(self, cmd: str, essid: str, password: str | None) -> bytes:
        return self.command(self.build_net_request(cmd, essid, password))

    def add_network(self, essid: str, password: str) -> list[str]:
        self._net_request("a", essid, password)
        return self.list_configured()

    def remove_network(self, essid: str) -> list[str]:
        self._net_request("d", essid, None)
        return self.list_configured()


MAILBOX_FILES = ("REQM", "RSPM", "REQC", "RSPC")


def ensure_mailbox(mount: str) -> None:
    """공장 초기 카드 부트스트랩: EYEFI/ 폴더와 16KB 메일박스 파일 4개를 만든다.

    파일 생성 후 반드시 캐시 우회 쓰기(_write_raw)로 0을 실제 카드에 반영해야 한다 —
    버퍼된 쓰기만으로는 플래시의 지워진 값(0xFF)이 남아 시퀀스 판독이 깨진다(실카드 실증).
    이미 올바른 크기로 존재하는 파일은 건드리지 않는다(카드가 관리 중인 시퀀스 보존)."""
    d = os.path.join(mount, EYEFI_DIR)
    os.makedirs(d, exist_ok=True)
    for name in MAILBOX_FILES:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.path.getsize(p) == BUF_SIZE:
            continue
        with open(p, "wb") as f:
            f.write(b"\x00" * BUF_SIZE)
            f.flush()
            os.fsync(f.fileno())
        _write_raw(p, b"")


def probe_bootstrap(root: str, poll_tries: int = 10) -> bool:
    """EYEFI 폴더가 없는 이동식 드라이브가 '카메라로 포맷된 Eye-Fi 카드'인지 탐침.

    카메라 포맷은 EYEFI 폴더와 'Eye-Fi' 볼륨라벨을 모두 지우므로(D40 실증 2026-09-15)
    라벨 판별(is_fresh_card)에 걸리지 않는다. 메일박스를 만들어 카드 펌웨어가 MAC 에
    응답하는지 확인한다 — 응답하면 True(메일박스 유지 = 복구 완료), 아니면 만든
    EYEFI 폴더를 지워 원상복구하고 False.
    일반 USB 에도 잠깐 파일을 만들었다 지우므로 자동 폴링에서는 절대 호출하지 말고
    사용자의 명시적 조작('카드를 앱에 등록' 버튼)에서만 쓸 것."""
    d = os.path.join(root, EYEFI_DIR)
    try:
        if not _is_removable(root):
            return False
        if os.path.isdir(d):
            return True          # 이미 메일박스 있음(설정된 카드) — 탐침 불필요
        ensure_mailbox(root)
        CardMailbox(root, poll_tries=poll_tries).get_mac()
        return True
    except (CardMailboxError, OSError):
        try:
            for name in MAILBOX_FILES:
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    os.remove(p)
            os.rmdir(d)
        except OSError:
            pass
        return False


def is_fresh_card(root: str) -> bool:
    """공장 초기 Eye-Fi 카드인가: EYEFI 폴더는 없지만 이동식 + 볼륨라벨 'Eye-Fi'."""
    try:
        if os.path.isdir(os.path.join(root, EYEFI_DIR)):
            return False
    except OSError:
        return False
    return _is_removable(root) and _volume_label(root).strip().lower() == "eye-fi"


def is_eyefi_drive(root: str) -> bool:
    """Eye-Fi 카드 드라이브인가(설정된 카드 또는 공장 초기 카드)."""
    try:
        if os.path.isdir(os.path.join(root, EYEFI_DIR)):
            return True
    except OSError:
        return False
    return is_fresh_card(root)


def find_card_mount(drive: str | None = None, probe_formatted: bool = False) -> str | None:
    """EYEFI 폴더가 있는 이동식 드라이브 루트 반환(Windows). drive 지정 시 그 드라이브.
    공장 초기 카드(EYEFI 없음, 라벨 'Eye-Fi')는 메일박스를 만들어 준 뒤 반환한다.
    probe_formatted=True(수동 등록 조작에서만!) 면 라벨까지 지워진 '카메라 포맷' 카드도
    probe_bootstrap 으로 탐침해 복구한다."""
    if drive:
        root = drive.rstrip("\\/:") + ":\\"
        if os.path.isdir(os.path.join(root, EYEFI_DIR)):
            return root
        if is_fresh_card(root):
            ensure_mailbox(root)
            return root
        if probe_formatted and probe_bootstrap(root):
            return root
        return None
    import string
    fresh = None
    candidates = []          # 탐침 후보: EYEFI 없는 이동식 드라이브
    for letter in string.ascii_uppercase:
        root = letter + ":\\"
        try:
            if os.path.isdir(os.path.join(root, EYEFI_DIR)):
                return root
            if fresh is None and is_fresh_card(root):
                fresh = root
            elif probe_formatted and _is_removable(root):
                candidates.append(root)
        except OSError:
            continue
    if fresh:
        # 설정된 카드가 하나도 없을 때만 공장 초기 카드를 부트스트랩
        ensure_mailbox(fresh)
        return fresh
    for root in candidates:
        if probe_bootstrap(root):
            return root
    return None


# ---- GUI 가 기대하는 dict 형태로 돌려주는 고수준 API (DLL 경로와 동일 형식) ----

def _open(drive: str | None = None) -> "CardMailbox":
    mount = find_card_mount(drive)
    if not mount:
        raise CardMailboxError("Eye-Fi 카드를 찾지 못함(리더 확인, EYEFI 폴더 없음)")
    return CardMailbox(mount)


def firmware_exposes_upload_key(firmware: str) -> bool:
    """카드 펌웨어가 업로드키(토큰 0xFD)를 노출하는가.
    eyefi-config 커뮤니티 확정: FW 5.0 이하는 키가 있어도 빈 값(len 0)으로 반환하고,
    **5.2010(2013-08-27) 이상에서만** 32자리로 읽힌다. 우리 클리닉 카드가 전부 5.2010.
    문자열 예: '5.2010 Aug 27 2013 18:12:44'. 앞의 major.minor 를 뽑아 5.2 이상인지 본다."""
    import re
    m = re.search(r"(\d+)\.(\d+)", firmware or "")
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    # 5.2010 형태: minor 가 4자리(2010)면 실질 5.2 계열 → 노출. 5.0/4.5/3.0 → 미노출.
    return (major, minor) >= (5, 2)


def read_firmware(drive: str | None = None) -> dict:
    """카드 펌웨어 버전만 빠르게 확인: {mac, firmware, exposes_key}.
    카메라 포맷 카드(EYEFI 없음)도 탐침 복구해 읽는다. exposes_key=True 면
    업로드키(토큰 0xFD) 판독 가능한 펌웨어(5.2010+), False 면 구펌웨어(키 숨김)."""
    mount = find_card_mount(drive) or find_card_mount(drive, probe_formatted=True)
    if not mount:
        raise CardMailboxError("Eye-Fi 카드를 찾지 못함(리더 확인)")
    mb = CardMailbox(mount)
    mac = mb.get_mac()
    fw = mb.get_firmware()
    return {"ok": True, "mac": mac, "firmware": fw, "drive": mount.rstrip("\\"),
            "exposes_key": firmware_exposes_upload_key(fw)}


def read_card_info(drive: str | None = None, probe_formatted: bool = False) -> dict:
    """card_reader.read_card 와 같은 형식: {ok, mac, uploadkey, ssid, drive}.
    probe_formatted=True(수동 등록 버튼)면 카메라 포맷 카드도 복구 시도 — 복구되면
    recovered=True. 업로드키가 빈 응답이면 1회 재시도. 펌웨어·활성화코드는 항상 읽어
    함께 돌려주고(진단), 빈 키가 구펌웨어(≤5.0) 때문이면 key_hidden_by_fw=True."""
    import binascii
    mount = find_card_mount(drive)
    recovered = False
    if not mount and probe_formatted:
        mount = find_card_mount(drive, probe_formatted=True)
        recovered = bool(mount)
    if not mount:
        raise CardMailboxError("Eye-Fi 카드를 찾지 못함(리더 확인, EYEFI 폴더 없음)")
    mb = CardMailbox(mount)
    mac = mb.get_mac()
    uk_raw = mb._info(UPLOAD_KEY)
    uploadkey = mb._pascal(uk_raw).decode("ascii", "replace")
    if not uploadkey:                       # 빈 응답 → 1회 재시도
        uk_raw = mb._info(UPLOAD_KEY)
        uploadkey = mb._pascal(uk_raw).decode("ascii", "replace")
    try:
        firmware = mb.get_firmware()
    except CardMailboxError:
        firmware = "?"
    try:
        actcode = mb._pascal(mb._info(DIRECT_MODE_PASS)).decode("latin-1", "replace")
    except CardMailboxError:
        actcode = ""
    key_hidden_by_fw = bool(not uploadkey and not firmware_exposes_upload_key(firmware))
    return {
        "ok": True, "mac": mac, "uploadkey": uploadkey,
        "actcode": actcode, "raw": False, "drive": mount.rstrip("\\"),
        "ssid": "Eye-Fi Card " + mac.replace("-", "")[-6:],
        "recovered": recovered, "firmware": firmware,
        "uploadkey_raw": binascii.hexlify(uk_raw[:48]).decode() if not uploadkey else "",
        "key_hidden_by_fw": key_hidden_by_fw,
    }


def collect_diagnostics(drive: str | None = None, app_version: str = "",
                        probe_formatted: bool = True) -> str:
    """카드의 모든 진단 정보를 사람이 읽을 수 있는 텍스트로 수집한다.
    등록 실패 시 사용자가 이 파일을 보내면 원격에서 원인(구펌웨어/개체불량/세대 등)을
    분석할 수 있다. 카드가 없거나 통신 실패해도 그 사실 자체를 기록한다(예외 안 냄)."""
    import binascii
    import datetime
    lines = []
    def w(s=""):
        lines.append(s)
    w("=== Eye-Fi Receiver 카드 진단 로그 ===")
    w("시각: " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    w("앱 버전: " + (app_version or "?"))
    w("")

    mount = None
    recovered = False
    try:
        mount = find_card_mount(drive)
        if not mount and probe_formatted:
            mount = find_card_mount(drive, probe_formatted=True)
            recovered = bool(mount)
    except Exception as e:
        w("[마운트 탐색 오류] " + repr(e))

    if not mount:
        w("결과: Eye-Fi 카드(EYEFI 메일박스)를 찾지 못함.")
        w(" - 리더에 카드가 꽂혀 있는지, 탐색기에 드라이브가 보이는지 확인 필요.")
        w(" - 카메라 포맷 카드 탐침(probe)도 실패 → Eye-Fi 카드가 아니거나 응답 없음.")
        return "\n".join(lines)

    w("마운트: " + mount + ("  (카메라 포맷 → 메일박스 복구됨)" if recovered else ""))
    try:
        w("볼륨 라벨: " + _volume_label(mount))
    except Exception:
        pass
    w("")
    w("--- 카드 토큰 덤프 (raw hex + 해석) ---")
    try:
        mb = CardMailbox(mount)
    except Exception as e:
        w("[메일박스 세션 생성 실패] " + repr(e))
        return "\n".join(lines)

    fw = ""
    for name, tok in DIAG_TOKENS:
        try:
            raw = mb._info(tok)
            val = mb._pascal(raw)
            rawhex = binascii.hexlify(raw[:48]).decode()
            if tok == FIRMWARE_INFO:
                fw = val.decode("latin-1", "replace")
                text = fw
            elif tok == MAC_ADDRESS and len(val) >= 6:
                text = "00-18-56-%02x-%02x-%02x" % (val[3], val[4], val[5])
            else:
                text = val.decode("latin-1", "replace")
            w(f"{name:20} len={len(val):<3} raw={rawhex}")
            w(f"{'':20} → {text!r}")
        except Exception as e:
            w(f"{name:20} [읽기 실패] {e!r}")
    w("")
    try:
        w("설정된 망: " + repr(mb.list_configured()))
    except Exception as e:
        w("설정된 망: [실패] " + repr(e))

    w("")
    w("--- 판정 ---")
    uk = ""
    try:
        uk = mb._pascal(mb._info(UPLOAD_KEY)).decode("ascii", "replace")
    except Exception:
        pass
    if uk:
        w("업로드키 판독 OK (32자리). 등록 가능.")
    elif not firmware_exposes_upload_key(fw):
        w(f"업로드키 빈 값 + 펌웨어 '{fw}' (5.2 미만).")
        w("→ 원인: 구펌웨어는 키가 카드에 있어도 노출하지 않음(eyefi-config 커뮤니티 확정).")
        w("→ 해결(권장): 이 카드를 예전에 공식 Eye-Fi 앱으로 쓰던 PC의 설정에서 업로드키")
        w("   복구('카드를 앱에 등록' 시 자동 시도 + 설정 파일 직접 선택 가능).")
        w("→ 참고: 펌웨어 5.2010 업데이트로도 읽히지만, Eye-Fi 업데이트 서버가 종료돼")
        w("   지금은 사실상 불가. 복구가 안 되면 이 로그를 개발자에게 전달.")
    else:
        w(f"업로드키 빈 값인데 펌웨어 '{fw}' 는 5.2 이상.")
        w("→ 예상 밖 사례. 이 로그 전체를 개발자에게 전달해 분석 필요.")
    return "\n".join(lines)


def list_networks(drive: str | None = None) -> dict:
    """card_wifi.list_networks 와 같은 형식."""
    mb = _open(drive)
    return {
        "ok": True, "mac": mb.get_mac(), "drive": mb.mount.rstrip("\\"),
        "configured": [{"ssid": s} for s in mb.list_configured()],
        "scanned": [{"ssid": n["ssid"], "rssi": n["strength"], "flags": n["type"]}
                    for n in mb.scan()],
    }


def add_network(essid: str, password: str, drive: str | None = None) -> dict:
    mb = _open(drive)
    configured = mb.add_network(essid, password)
    return {"ok": True, "action": "add", "ssid": essid, "rc": 0,
            "registered": essid in configured,
            "configured": [{"ssid": s} for s in configured],
            "drive": mb.mount.rstrip("\\"), "mac": mb.get_mac()}


def delete_network(essid: str, drive: str | None = None) -> dict:
    mb = _open(drive)
    configured = mb.remove_network(essid)
    return {"ok": True, "action": "delete", "ssid": essid, "rc": 0,
            "removed": essid not in configured,
            "configured": [{"ssid": s} for s in configured],
            "drive": mb.mount.rstrip("\\"), "mac": mb.get_mac()}
