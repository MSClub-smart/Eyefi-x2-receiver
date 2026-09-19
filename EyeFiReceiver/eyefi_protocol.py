"""
eyefi_protocol.py — Eye-Fi X2 로컬 업로드 프로토콜 핵심 로직 (표준 라이브러리만 사용)

카드(=HTTP 클라이언트)가 서버 TCP 59278 로 접속해 SOAP 로 사진을 올린다.
경로: /api/soap/eyefilm/v1        (SOAP 제어: StartSession/GetPhotoStatus/MarkLastPhotoInRoll)
      /api/soap/eyefilm/v1/upload (UploadPhoto: multipart, tar 안에 실제 JPG)

인증(credential):
  - StartSession 응답 credential = MD5( unhexlify(mac + cnonce + uploadkey) )
    → 카드가 이 값을 검증해 "서버"를 신뢰. (오픈소스 eyefiserver 계열에서 실검증된 공식)
  - 카드→서버 GetPhotoStatus credential 은 **강제검증**(2026-09-15부터):
    후보 조합(아래 credential_variants) 중 하나와 일치해야 offset 을 응답한다.
    일치한 변형 라벨을 photo_status 이벤트(cred_variant)로 로깅해 실카드 공식을 확정한다.
    (참고: 공식앱 dnSpy 해독의 4요소 변형 MD5(mac+snonce+cnonce+uploadkey) 도 후보에 포함)
"""
from __future__ import annotations
import hashlib
import binascii
import os
import re
import struct
import tarfile
import io
from xml.etree import ElementTree as ET

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
EYEFI_NS = "http://localhost/api/soap/eyefilm"


# ---------------------------------------------------------------- credential
def _unhex(*parts: str) -> bytes:
    # MAC 이 '00-18-56-..' 처럼 구분자를 가질 수 있으므로 hex 이외 문자는 제거 후 디코드
    joined = "".join(parts)
    clean = "".join(ch for ch in joined if ch in "0123456789abcdefABCDEF")
    return binascii.unhexlify(clean)


def start_session_credential(mac: str, cnonce: str, uploadkey: str) -> str:
    """StartSession 응답에 넣는 credential (서버→카드 인증). 검증된 공식."""
    return hashlib.md5(_unhex(mac, cnonce, uploadkey)).hexdigest()


def credential_variants(mac: str, cnonce: str, snonce: str, uploadkey: str) -> list[tuple[str, str]]:
    """카드가 보낸 credential 을 대조할 (라벨, digest) 후보들
    (구현체별 순서 차이 흡수 + HANDOFF 4요소)."""
    combos = [
        ("mac+cnonce+uploadkey", (mac, cnonce, uploadkey)),
        ("mac+uploadkey+snonce", (mac, uploadkey, snonce)),   # 오픈소스 eyefiserver 공식
        ("mac+snonce+uploadkey", (mac, snonce, uploadkey)),
        ("mac+snonce+cnonce+uploadkey", (mac, snonce, cnonce, uploadkey)),  # HANDOFF(공식앱) 4요소
        ("mac+cnonce+snonce+uploadkey", (mac, cnonce, snonce, uploadkey)),
    ]
    out = []
    for label, c in combos:
        try:
            out.append((label, hashlib.md5(_unhex(*c)).hexdigest()))
        except binascii.Error:
            pass
    return out


def credential_candidates(mac: str, cnonce: str, snonce: str, uploadkey: str) -> list[str]:
    """라벨 없는 digest 목록 (기존 호환)."""
    return [d for _, d in credential_variants(mac, cnonce, snonce, uploadkey)]


def match_credential(mac: str, cnonce: str, snonce: str, uploadkey: str, got: str) -> str | None:
    """GetPhotoStatus credential 대조: 일치한 변형 라벨 반환, 불일치면 None."""
    if not got:
        return None
    got = got.strip().lower()
    for label, digest in credential_variants(mac, cnonce, snonce, uploadkey):
        if got == digest:
            return label
    return None


def make_snonce() -> str:
    """서버 nonce: 128비트 랜덤 hex(32자)."""
    return binascii.hexlify(os.urandom(16)).decode()


# ------------------------------------------------------------------- SOAP I/O
def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_soap_values(body: bytes) -> dict:
    """SOAP 요청 본문에서 자식 요소들을 {태그로컬명: 텍스트} 로 평탄화."""
    values: dict[str, str] = {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return values
    for el in root.iter():
        name = _localname(el.tag)
        if el.text and el.text.strip():
            values[name] = el.text.strip()
    return values


def soap_action_name(headers) -> str:
    """SOAPAction 헤더에서 메서드명 추출. 예: "urn:StartSession" → StartSession."""
    raw = headers.get("SOAPAction", "") or headers.get("soapaction", "")
    raw = raw.strip().strip('"')
    return raw.split(":")[-1] if raw else ""


def _envelope(inner: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="%s">'
        "<SOAP-ENV:Body>%s</SOAP-ENV:Body>"
        "</SOAP-ENV:Envelope>"
    ) % (SOAP_NS, inner)
    return xml.encode("utf-8")


def build_start_session_response(mac, cnonce, uploadkey, snonce, transfermode, ts) -> bytes:
    cred = start_session_credential(mac, cnonce, uploadkey)
    inner = (
        '<StartSessionResponse xmlns="%s">'
        "<credential>%s</credential>"
        "<snonce>%s</snonce>"
        "<transfermode>%s</transfermode>"
        "<transfermodetimestamp>%s</transfermodetimestamp>"
        "<upsyncallowed>false</upsyncallowed>"
        "</StartSessionResponse>"
    ) % (EYEFI_NS, cred, snonce, transfermode, ts)
    return _envelope(inner)


def build_get_photo_status_response(fileid=1, offset=0) -> bytes:
    inner = (
        '<GetPhotoStatusResponse xmlns="%s">'
        "<fileid>%s</fileid><offset>%s</offset>"
        "</GetPhotoStatusResponse>"
    ) % (EYEFI_NS, fileid, offset)
    return _envelope(inner)


def build_upload_photo_response(success=True) -> bytes:
    inner = (
        '<UploadPhotoResponse xmlns="%s"><success>%s</success></UploadPhotoResponse>'
    ) % (EYEFI_NS, "true" if success else "false")
    return _envelope(inner)


def build_mark_last_photo_response() -> bytes:
    inner = '<MarkLastPhotoInRollResponse xmlns="%s"></MarkLastPhotoInRollResponse>' % EYEFI_NS
    return _envelope(inner)


# ---------------------------------------------------------- integrity digest
def tcp_checksum(block: bytes) -> bytes:
    """블록의 TCP 체크섬: 16비트 LE 워드 합 → 캐리 접기 → 1의 보수. 2바이트 LE 반환.
    (홀수 길이는 0 패딩)"""
    if len(block) % 2:
        block += b"\x00"
    total = 0
    for i in range(0, len(block), 2):
        total += block[i] | (block[i + 1] << 8)
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return struct.pack("<H", ~total & 0xFFFF)


def integrity_digest(tar_bytes: bytes, uploadkey: str) -> str:
    """UploadPhoto 의 INTEGRITYDIGEST 계산식 (★확정 2026-09-15): tar 를 512B 블록으로
    잘라 블록별 tcp_checksum(2B) 을 이어붙인 뒤 unhex(uploadkey) 를 덧붙여 MD5 hexdigest.
    병원 PC 실카드 캡처 12개 대조(verify_integrity)에서 완전 캡처 2개 모두 이 공식과
    일치(tcpsum-inv). 부분(중단) 캡처의 불일치는 정상 — 다이제스트는 완전 파일 기준."""
    sums = bytearray()
    for off in range(0, len(tar_bytes), 512):
        sums += tcp_checksum(tar_bytes[off:off + 512])
    sums += _unhex(uploadkey)
    return hashlib.md5(bytes(sums)).hexdigest()


# --------------------------------------------------------------- multipart/tar
def split_multipart(body: bytes, content_type: str) -> dict:
    """
    UploadPhoto 의 multipart 본문을 {파트이름: 바이트} 로 분해.
    파트이름은 Content-Disposition 의 name= 값(SOAPENVELOPE, FILENAME, INTEGRITYDIGEST 등).
    """
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "", re.I)
    if not m:
        return {}
    boundary = (m.group(1) or m.group(2)).strip()
    delim = b"--" + boundary.encode()
    parts: dict[str, bytes] = {}
    for chunk in body.split(delim):
        if not chunk:
            continue
        # 바이트 정확 파싱: 앞뒤 CRLF 를 딱 1개씩만 제거(payload 바이트 보존 — 이어받기 필수).
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        head, sep, payload = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue  # 종료 구분자(--) 등
        head_txt = head.decode("latin-1", "replace")
        nm = re.search(r'name="?([^";\r\n]+)"?', head_txt, re.I)
        if nm:
            parts[nm.group(1).strip()] = payload
    return parts


def extract_media_from_tar(tar_bytes: bytes) -> list[tuple[str, bytes, int]]:
    """카드가 보낸 tar 에서 (파일명, 내용, mtime) 목록 추출. 로그(.log) 등은 그대로 포함.
    mtime = 카드 원본 파일의 촬영 시각. 단, 카드가 FAT 로컬시간을 UTC 로 취급해 넣으므로
    실제 로컬 촬영시각은 time.gmtime(mtime) 으로 해석해야 한다(실카드 캡처로 확인).
    부분 tar(끊김 반복으로 마지막 종료 0블록이 유실된 경우)라도, 데이터가 온전한
    멤버까지는 추출한다. getmembers() 대신 next() 루프로 종료블록 누락에 견고."""
    out: list[tuple[str, bytes, int]] = []
    tf = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*")
    try:
        while True:
            try:
                member = tf.next()
            except tarfile.ReadError:
                break  # 종료블록 누락 등 — 여기까지가 유효
            if member is None:
                break
            if not member.isfile():
                continue
            f = tf.extractfile(member)
            if f is None:
                continue
            out.append((os.path.basename(member.name), f.read(), int(member.mtime or 0)))
    finally:
        tf.close()
    return out


def tar_media_complete(tar_bytes: bytes) -> bool:
    """tar 안 모든 파일 멤버의 데이터 영역이 tar_bytes 안에 온전히 들어있으면 True.
    (tar 끝 종료 0블록의 유무는 무시 — 파일 데이터만 완전하면 '완료'로 인정)
    → 끊김 반복으로 정확한 filesize 에 못 미쳐도 사진이 온전하면 저장 가능하게 함.
    tarfile 내부동작에 의존하지 않도록 헤더(512B 블록)를 직접 파싱해 길이만 검증한다."""
    n = len(tar_bytes)
    off = 0
    saw_file = False
    while off + 512 <= n:
        header = tar_bytes[off:off + 512]
        if header == b"\x00" * 512:      # 종료(EOF) 블록
            break
        name = header[0:100].split(b"\x00", 1)[0]
        if not name:                      # 이름 없음 → 유효 헤더 아님
            break
        try:
            size = int(bytes(header[124:136]).rstrip(b"\x00 ") or b"0", 8)
        except ValueError:
            return False                  # 헤더 손상
        typeflag = header[156:157]
        data_off = off + 512
        if typeflag in (b"0", b"\x00"):   # 일반 파일
            if data_off + size > n:
                return False              # 데이터 영역이 잘림 → 아직 미완
            saw_file = True
        off = data_off + ((size + 511) // 512) * 512   # 다음 블록(512 배수)
    return saw_file
