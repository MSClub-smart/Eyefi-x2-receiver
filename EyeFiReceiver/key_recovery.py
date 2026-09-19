r"""
key_recovery.py — 카드에서 업로드키를 못 읽을 때(구펌웨어 ≤5.0), 예전 공식 Eye-Fi
소프트웨어가 PC에 저장해 둔 설정에서 업로드키를 복구한다.

배경: Eye-Fi X2 카드는 펌웨어 5.0 이하에서 업로드키(토큰 0xFD)가 카드에 있어도
빈 값으로 읽힌다. 하지만 카드를 예전에 공식 앱(Eye-Fi Center / X2 Utility)으로
활성화했다면, 그 PC의 설정 파일에 MAC↔업로드키가 저장돼 있다:
  - X2 Utility:  %APPDATA%\Eye-FiX2\Settings.xml  (<Config> 안에 MAC + UploadKey)
  - Eye-Fi Center/Keenai: client.db / *.sqlite (테이블 o_devices: o_mac_address, o_upload_key)

이 모듈은 파일 형식(정확한 스키마)에 의존하지 않도록 방어적으로 파싱한다:
  - XML/텍스트: 대상 MAC 근처의 32자리 hex(업로드키 후보)를 찾는다.
  - sqlite: 모든 테이블을 훑어 MAC 이 든 행에서 32자리 hex 값을 찾는다.
카드에 쓰지 않는 읽기 전용 동작이라 안전하다.
"""
from __future__ import annotations
import glob
import os
import re
import sqlite3

_HEX32 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")


def _mac_digits(mac: str) -> str:
    """MAC 을 구분자 없는 소문자 12자리 hex 로."""
    return "".join(ch for ch in mac if ch in "0123456789abcdefABCDEF").lower()


def _mac_regex(mac: str) -> re.Pattern:
    """'0018561234 60' / '00-18-56-12-34-60' / '00:18:56:10:24:76' 등 어떤 구분자든 매칭."""
    d = _mac_digits(mac)
    if len(d) != 12:
        return re.compile(r"(?!x)x")  # 매칭 안 됨
    # 각 hex 쌍 사이에 구분자(-, :, 공백) 0~2개 허용
    parts = [d[i:i + 2] for i in range(0, 12, 2)]
    return re.compile(r"[\s:\-]{0,2}".join(parts), re.I)


def extract_from_text(text: str, mac: str) -> str | None:
    """텍스트(XML 등)에서 대상 MAC 에 가장 가까운 32-hex(업로드키)를 반환.
    MAC 위치 뒤에서 먼저 찾고, 없으면 앞에서 찾는다."""
    m = _mac_regex(mac).search(text)
    if not m:
        return None
    pos = m.end()
    after = [(k.start(), k.group(0)) for k in _HEX32.finditer(text) if k.start() >= pos]
    if after:
        return after[0][1].lower()
    before = [(k.start(), k.group(0)) for k in _HEX32.finditer(text) if k.start() < m.start()]
    if before:
        return before[-1][1].lower()
    return None


def extract_from_sqlite(path: str, mac: str) -> str | None:
    """sqlite DB의 모든 테이블을 훑어, 대상 MAC 이 든 행에서 32-hex(업로드키)를 찾는다."""
    want = _mac_digits(mac)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            con = sqlite3.connect(path)
        except sqlite3.Error:
            return None
    try:
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            try:
                cur.execute(f'SELECT * FROM "{t}"')
                rows = cur.fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                cells = [str(c) for c in row if c is not None]
                # 이 행에 대상 MAC 이 들어 있나(구분자 무시)
                joined = " ".join(cells)
                if want and want in _mac_digits(joined):
                    # 같은 행에서 32-hex 값(단, MAC 12자리는 제외)
                    for c in cells:
                        mm = _HEX32.search(c)
                        if mm:
                            return mm.group(0).lower()
        return None
    finally:
        con.close()


def default_config_files() -> list[str]:
    """이 PC에서 예전 Eye-Fi 소프트웨어 설정 파일이 있을 만한 위치를 훑는다."""
    roots = [os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", ""),
             os.environ.get("PROGRAMDATA", "")]
    out: list[str] = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for sub in ("Eye-FiX2", "Eye-Fi", "EyeFi", "Keenai"):
            base = os.path.join(root, sub)
            if not os.path.isdir(base):
                continue
            for pat in ("**/Settings.xml", "**/*.xml", "**/*.db", "**/*.sqlite",
                        "**/*.sqlite3"):
                out.extend(glob.glob(os.path.join(base, pat), recursive=True))
    # 중복 제거, 존재하는 파일만
    seen, files = set(), []
    for f in out:
        if f not in seen and os.path.isfile(f):
            seen.add(f)
            files.append(f)
    return files


def recover_from_file(path: str, mac: str) -> str | None:
    """파일 하나에서 업로드키 복구 시도(확장자로 XML/sqlite 판별, 실패 시 양쪽 다 시도)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".db", ".sqlite", ".sqlite3"):
        return extract_from_sqlite(path, mac)
    # XML/텍스트
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        # sqlite 를 .xml 로 저장한 경우 등 폴백
        return extract_from_sqlite(path, mac)
    key = extract_from_text(text, mac)
    if key:
        return key
    # 텍스트로 안 나오면 sqlite 로도 시도
    return extract_from_sqlite(path, mac)


def recover_upload_key(mac: str, extra_files: list[str] | None = None) -> tuple[str, str] | None:
    """대상 MAC 의 업로드키를 (키, 출처파일)로 복구. 기본 위치 + 추가 지정 파일을 훑는다.
    못 찾으면 None. 자기 MAC 과 우연히 같은 32-hex(=MAC 12자리)는 후보에서 자연 제외됨."""
    files = list(default_config_files())
    if extra_files:
        files = list(extra_files) + files
    for f in files:
        try:
            key = recover_from_file(f, mac)
        except Exception:
            key = None
        if key and len(key) == 32 and key != "0" * 32:
            return key, f
    return None
