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
from xml.etree import ElementTree as ET

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


def _localname(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _el_field(el, names: set) -> str | None:
    """el 의 속성 또는 '직속 자식' 에서 이름이 names 에 드는 값을 반환(대소문자 무시)."""
    for k, v in el.attrib.items():
        if _localname(k) in names:
            return v
    for ch in list(el):
        if _localname(ch.tag) in names:
            return ch.text or ""
    return None


def _extract_xml_structured(text: str, mac: str) -> tuple[bool, str | None]:
    """Eye-Fi Settings.xml 을 '카드 블록' 단위로 정확히 파싱.
    <Card MacAddress=".."><UploadKey>..</UploadKey>..</Card> (속성형) 및
    <Card><MacAddress>..</MacAddress><UploadKey>..</UploadKey></Card> (자식형) 모두 지원.
    반환 (matched, key): 대상 MAC 카드 블록을 찾았으면 matched=True 이고 key 는 그 카드의
    UploadKey(비었으면 None). ★ 절대 다른 카드의 키나 DownsyncKey/CardKey 를 집지 않는다."""
    want = _mac_digits(mac)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return (False, None)
    for el in root.iter():
        mv = _el_field(el, {"macaddress", "mac"})
        if mv and _mac_digits(mv) == want:
            uk = _el_field(el, {"uploadkey"})   # 오직 UploadKey (DownsyncKey 등 제외)
            if uk:
                uk = uk.strip().lower()
                if re.fullmatch(r"[0-9a-f]{32}", uk):
                    return (True, uk)
            return (True, None)   # 카드는 찾았으나 UploadKey 가 비어 있음 → '키 없음'
    return (False, None)


def _extract_labeled_key(text: str, mac: str) -> str | None:
    """비-XML/비정형 텍스트 폴백: 'UploadKey' 라벨에 바로 붙은 32-hex 만 인정.
    (근접 추정으로 옆 값 오인하는 사고 방지 — 반드시 upload 라벨이 있어야 함)"""
    if not _mac_regex(mac).search(text):
        return None
    for mm in re.finditer(r"upload[_\- ]?key\s*['\"=:>]*\s*([0-9a-fA-F]{32})", text, re.I):
        return mm.group(1).lower()
    return None


def extract_from_text(text: str, mac: str) -> str | None:
    """텍스트(XML 등)에서 '대상 카드'의 업로드키를 반환. 카드 블록 구조를 우선 파싱하고,
    XML 이 아니면 'UploadKey' 라벨이 붙은 값만 폴백으로 인정한다.
    대상 카드의 UploadKey 가 비어 있으면(그 카드에 키 없음) None 을 돌려준다 —
    옆 카드 키/DownsyncKey 를 잘못 반환하지 않는다."""
    matched, key = _extract_xml_structured(text, mac)
    if matched:
        return key            # 카드 블록을 찾음 → 그 카드의 UploadKey(없으면 None) 확정
    return _extract_labeled_key(text, mac)


def extract_from_sqlite(path: str, mac: str) -> str | None:
    """sqlite DB의 모든 테이블을 훑어, 대상 MAC 이 든 행에서 업로드키를 찾는다.
    ★ 'upload' 키 컬럼을 우선하고 downsync/card 키 컬럼은 제외 — 엉뚱한 키 오인 방지."""
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
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
        except sqlite3.Error:
            return None   # sqlite 가 아닌 파일 등 → 조용히 실패
        for t in tables:
            try:
                cur.execute(f'SELECT * FROM "{t}"')
                rows = cur.fetchall()
                cols = [str(d[0]).lower() for d in (cur.description or [])]
            except sqlite3.Error:
                continue
            for row in rows:
                cells = ["" if c is None else str(c) for c in row]
                joined = " ".join(cells)
                if not (want and want in _mac_digits(joined)):
                    continue
                # 1) 'upload...key' 컬럼 우선
                for i, cn in enumerate(cols):
                    if "upload" in cn and "key" in cn:
                        mm = _HEX32.search(cells[i])
                        if mm:
                            return mm.group(0).lower()
                # 2) downsync/card 키 컬럼은 건너뛰고 나머지 32-hex
                for i, cn in enumerate(cols):
                    if "downsync" in cn or ("card" in cn and "key" in cn):
                        continue
                    mm = _HEX32.search(cells[i])
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
