"""
eyefi_server.py — Eye-Fi X2 로컬 수신 HTTP 서버 (표준 라이브러리만)

- ThreadingHTTPServer 로 TCP 59278 대기
- SOAPAction 에 따라 StartSession / GetPhotoStatus / UploadPhoto / MarkLastPhotoInRoll 처리
- 사진은 config 의 카드별(없으면 기본) 폴더에 저장, 옵션에 따라 날짜 하위폴더 생성
- 전송 이벤트 콜백(on_event) 으로 GUI/로그/계측에 연결 가능 (딜레이 계측 내장 지점)

이 모듈은 공식 앱/레지스트리에 의존하지 않는다. 실행 전 공식 EyeFiX2Receiver 는 꺼야
동일 포트 충돌이 없다(테스트 시에는 port 를 다르게 주어 공존 가능).
"""
from __future__ import annotations
import os
import re
import time
import socket
import threading
import datetime
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 업로드 본문 읽기 안전망 타임아웃(초). 카드는 저전력이라 전송 도중 수십 초씩 쉬므로
# 그보다는 길게, 그러나 죽은 연결이 스레드를 오래 붙잡지 않을 만큼 짧게.
# (스트리밍 스풀 도입으로 끊겨도 받은 만큼은 이미 디스크에 있음 → 길게 잡을 이유가 없다)
UPLOAD_STALL_TIMEOUT = 90

# 미전송 감시(워치독): 부분수신 파일이 스풀에 남은 채 진행이 멈추면 주기 점검으로 알림.
# 카드는 카메라 전원이 있어야만 재접속하므로 서버가 카드를 직접 깨울 수는 없다 —
# 대신 '멈춘 전송'을 감지해 사용자에게 카메라를 켜라고 알리고, 켜지면 이어받는다.
WATCHDOG_INTERVAL = 60      # 점검 주기(초)
STALL_ALERT_AFTER = 600     # 마지막 진행 후 이만큼 지나면 '미전송 대기'로 판단(초)
STALL_REALERT = 1800        # 같은 파일 재알림 간격(초)
STALL_GIVEUP_AFTER = 3 * 86400   # 이만큼 진행이 없으면 '카드가 포기한 전송'으로 보고
                                 # 스풀을 보관용(.abandoned)으로 치워 반복 알림을 멈춘다
                                 # (카메라에서 삭제된 사진 등 — 원본은 리더 가져오기로 복구 가능)


def find_stalled(spool_dir: str, now: float, stall_after: float = STALL_ALERT_AFTER) -> list[dict]:
    """스풀에서 진행이 멈춘 부분수신 파일 나열: [{mac, file, got, need, age_sec}].
    need 는 .size 메타가 있을 때만. 순수 함수(테스트 용이) — 상태는 호출측이 관리."""
    out = []
    try:
        macs = os.listdir(spool_dir)
    except OSError:
        return out
    for macdir in macs:
        d = os.path.join(spool_dir, macdir)
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.endswith(".part"):
                continue
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            age = now - st.st_mtime
            if age < stall_after or st.st_size <= 0:
                continue
            need = None
            try:
                with open(p + ".size", "r") as f:
                    need = int(f.read().strip())
            except Exception:
                pass
            out.append({"mac": macdir, "file": name[:-5], "got": st.st_size,
                        "need": need, "age_sec": int(age)})
    out.sort(key=lambda x: x["file"])
    return out

from . import eyefi_protocol as proto
from . import reader_import
from . import paths
from .config import Config, normalize_mac


class _Session:
    __slots__ = ("mac", "cnonce", "snonce", "started")

    def __init__(self, mac, cnonce, snonce):
        self.mac = mac
        self.cnonce = cnonce
        self.snonce = snonce
        self.started = time.time()


class EyeFiServer(ThreadingHTTPServer):
    daemon_threads = True
    # False: 공식앱이 이미 59278 을 쓰고 있으면 우리 bind 가 실패(→ 충돌 로그)해야 함.
    # True(SO_REUSEADDR) 면 Windows 에서 이중 바인딩이 되어 사진이 어느 쪽으로 갈지 불확실해짐.
    allow_reuse_address = False

    def __init__(self, config: Config, on_event=None):
        self.config = config
        self.on_event = on_event or (lambda kind, info: None)
        # MAC당 최근 세션 여러 개 보관(끊김 시 카드가 연결을 연달아 열어 snonce 가
        # 서로 다른 연결에 걸치는 경합 실측 — 단일 세션이면 credential 이 0.6% 헛불일치)
        self.sessions: dict[str, deque] = {}
        self._last_recv_time: float | None = None
        self.spool_dir = paths.data_path("spool")
        os.makedirs(self.spool_dir, exist_ok=True)
        # 스트리밍 스풀 동시성 제어: 파일별 쓰기 잠금 + 최신 쓰기 연결 토큰 + 마지막 안내 offset
        self._spool_locks: dict[tuple, threading.Lock] = {}
        self._spool_locks_guard = threading.Lock()
        self.active_writer: dict[tuple, object] = {}
        self.last_adv: dict[tuple, int] = {}
        super().__init__(("0.0.0.0", config.port), _Handler)
        # 미전송 워치독 — 서버와 수명을 같이하는 데몬 스레드
        self._watchdog_stop = threading.Event()
        self._stall_alerted: dict[str, float] = {}
        threading.Thread(target=self._watchdog_loop, daemon=True).start()

    # -------- 미전송 감시(워치독) --------
    def _watchdog_loop(self):
        while not self._watchdog_stop.wait(WATCHDOG_INTERVAL):
            try:
                self._watchdog_tick(time.time())
            except Exception:
                pass

    def _watchdog_tick(self, now: float):
        stalled = find_stalled(self.spool_dir, now)
        fresh, live = [], set()
        for s in stalled:
            key = s["mac"] + "/" + s["file"]
            if s["age_sec"] >= STALL_GIVEUP_AFTER:
                self._spool_abandon(s)          # 포기 → 보관 이동 + 1회 알림, live 에서 제외
                continue
            live.add(key)
            if now - self._stall_alerted.get(key, 0) >= STALL_REALERT:
                self._stall_alerted[key] = now
                fresh.append(s)
        # 완료(스풀에서 사라짐)·포기된 파일의 알림 기록은 청소 — 재발 시 다시 알리도록
        for k in [k for k in self._stall_alerted if k not in live]:
            del self._stall_alerted[k]
        if fresh:
            self.emit("stalled", files=fresh)

    def _spool_abandon(self, s: dict):
        """며칠째 진행 없는 부분수신을 보관용으로 치움(데이터 보존, 알림 중단)."""
        mac, fname = s["mac"], s["file"]
        with self._spool_lock(mac, fname):
            p = os.path.join(self.spool_dir, mac, fname + ".part")
            try:
                os.replace(p, p + ".abandoned")
            except OSError:
                return
            try:
                os.remove(p + ".size")
            except OSError:
                pass
        self.emit("stall_giveup", mac=mac, file=fname, got=s["got"], need=s["need"],
                  age_days=round(s["age_sec"] / 86400, 1))

    def server_close(self):
        self._watchdog_stop.set()
        super().server_close()

    # -------- resumable spool (이어받기) --------
    def _spool_path(self, mac: str, filename: str) -> str:
        safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in (filename or "unknown"))
        d = os.path.join(self.spool_dir, (mac or "unknown").replace(":", "-"))
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, safe + ".part")

    def spool_offset(self, mac: str, filename: str, filesize: int) -> int:
        """현재까지 받은 바이트수(=이어받을 offset). filesize 불일치/초과면 리셋 후 0."""
        p = self._spool_path(mac, filename)
        try:
            cur = os.path.getsize(p)
        except OSError:
            return 0
        expected = None
        try:
            with open(p + ".size", "r") as f:
                expected = int(f.read().strip())
        except Exception:
            pass
        if filesize and ((expected is not None and expected != filesize) or cur > filesize):
            self.spool_reset(mac, filename)   # 다른 파일/손상 → 처음부터
            return 0
        return cur

    def _spool_lock(self, mac: str, filename: str) -> threading.Lock:
        key = (mac, filename)
        with self._spool_locks_guard:
            lk = self._spool_locks.get(key)
            if lk is None:
                lk = self._spool_locks[key] = threading.Lock()
            return lk

    def claim_writer(self, mac: str, filename: str) -> object:
        """이 (카드,파일) 의 '최신 쓰기 연결' 토큰 발급 — 이전 연결의 쓰기는 무효화 신호."""
        token = object()
        self.active_writer[(mac, filename)] = token
        return token

    def writer_valid(self, mac: str, filename: str, token: object) -> bool:
        return self.active_writer.get((mac, filename)) is token

    def spool_stream_begin(self, mac: str, filename: str, filesize: int, start_offset: int) -> bool:
        """스트리밍 기록 시작 전제조건. start=0 이면 새로 시작(기존 스풀 폐기),
        아니면 스풀 크기와 정확히 이어져야 함. 어긋나면 False(→RAM 폴백)."""
        with self._spool_lock(mac, filename):
            if start_offset == 0:
                self._spool_remove(mac, filename)
                return True
            try:
                cur = os.path.getsize(self._spool_path(mac, filename))
            except OSError:
                cur = 0
            return cur == start_offset

    def spool_append_at(self, mac: str, filename: str, data: bytes, filesize: int, pos: int) -> bool:
        """스풀의 정확히 pos 위치(=현재 크기)에만 append 허용 — 겹침/순서꼬임 쓰기 원천 차단.
        위치 불일치·크기 초과면 False(데이터 버림). 성공 True."""
        if not data:
            return True
        with self._spool_lock(mac, filename):
            p = self._spool_path(mac, filename)
            try:
                cur = os.path.getsize(p)
            except OSError:
                cur = 0
            if cur != pos or (filesize and pos + len(data) > filesize):
                return False
            with open(p, "ab") as f:
                f.write(data)
            if filesize:
                try:
                    with open(p + ".size", "w") as f:
                        f.write(str(filesize))
                except Exception:
                    pass
            return True

    def spool_try_complete(self, mac: str, filename: str, filesize: int):
        """스풀이 완성됐는지 검사. 반환: (완료?, 조립된 tar바이트 or None, 현재크기)."""
        with self._spool_lock(mac, filename):
            p = self._spool_path(mac, filename)
            try:
                cur = os.path.getsize(p)
            except OSError:
                return False, None, 0
            if filesize and cur >= filesize:
                with open(p, "rb") as f:
                    return True, f.read(filesize), cur   # 정확히 filesize 만큼
            # 근접완료 구제: filesize 에 살짝(≤4KB) 못 미쳐도 — 끊김 반복으로 tar 끝
            # 종료블록이 유실된 경우 — 안의 사진(JPEG)이 온전하면 완료로 인정해 저장.
            if filesize and 0 < filesize - cur <= 4096:
                with open(p, "rb") as f:
                    allb = f.read()
                if proto.tar_media_complete(allb):
                    return True, allb, cur
            return False, None, cur

    def _spool_remove(self, mac: str, filename: str):
        # 내부용(잠금 없이) — 반드시 _spool_lock 아래에서 호출
        p = self._spool_path(mac, filename)
        for path in (p, p + ".size"):
            try:
                os.remove(path)
            except OSError:
                pass

    def spool_reset(self, mac: str, filename: str):
        with self._spool_lock(mac, filename):
            self._spool_remove(mac, filename)

    def emit(self, kind: str, **info):
        try:
            self.on_event(kind, info)
        except Exception:
            pass

    def save_media(self, mac: str, filename: str, data: bytes, taken_epoch: int = 0) -> str:
        folder = self.config.folder_for(mac) or "."
        # 같은 이름+크기가 이미 있으면(예: 리더로 먼저 가져옴) 재저장하지 않음
        dup = reader_import.find_duplicate(folder, filename, len(data))
        if dup:
            reader_import.ledger_record(filename, len(data))
            self.emit("duplicate", mac=mac, path=dup, size=len(data))
            return dup
        if self.config.date_subfolders:
            # 저장 날짜 = '촬영일'. tar mtime 은 카드가 FAT 로컬시간을 UTC 취급해 넣으므로
            # gmtime 으로 해석해야 실제 촬영 로컬시각(실카드 캡처로 검증됨).
            # 미래(전송이 촬영보다 앞설 수 없음)나 1년 이상 과거(카메라 시계 고장)는 무시.
            day = None
            if taken_epoch:
                try:
                    t = time.gmtime(taken_epoch)
                    d = datetime.date(t.tm_year, t.tm_mon, t.tm_mday)
                    today = datetime.date.today()
                    if today - datetime.timedelta(days=365) <= d <= today:
                        day = d
                except (ValueError, OverflowError, OSError):
                    day = None
            folder = os.path.join(folder, (day or datetime.date.today()).isoformat())
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, filename)
        # 이름 충돌(크기 다름 = 다른 사진) 시 _1, _2 …
        base, ext = os.path.splitext(dest)
        n = 1
        while os.path.exists(dest):
            dest = f"{base}_{n}{ext}"
            n += 1
        with open(dest, "wb") as f:
            f.write(data)
        reader_import.ledger_record(filename, len(data))   # 리더 가져오기 중복 방지 장부
        # 도착 간격(딜레이) 계측
        now = time.time()
        gap = None if self._last_recv_time is None else round(now - self._last_recv_time, 1)
        self._last_recv_time = now
        self.emit("received", mac=mac, path=dest, size=len(data), gap_sec=gap)
        return dest

    def capture_integrity(self, mac, filename, filesize, data, digest, uploadkey):
        """무결성 검증식 보정용: 카드의 INTEGRITYDIGEST + 받은 데이터 + 키를 저장(최대 12개).
        이 캡처로 '카드가 보낸 다이제스트'와 '우리 계산값'을 맞춰 검증식을 완성한다."""
        try:
            d = paths.data_path("captures", "integrity")
            os.makedirs(d, exist_ok=True)
            existing = [f for f in os.listdir(d) if f.endswith(".txt")]
            if len(existing) >= 12:
                return
            safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in (filename or "x"))
            base = f"{len(existing):02d}_{safe}_{len(data or b'')}"
            with open(os.path.join(d, base + ".bin"), "wb") as f:
                f.write(data or b"")
            dg = digest.decode("latin-1", "replace") if isinstance(digest, (bytes, bytearray)) else str(digest)
            with open(os.path.join(d, base + ".txt"), "w", encoding="utf-8") as f:
                f.write(f"mac={mac}\nfilename={filename}\nfilesize={filesize}\n"
                        f"chunklen={len(data or b'')}\nfull={'yes' if (filesize and len(data or b'')==filesize) else 'no'}\n"
                        f"uploadkey={uploadkey}\nintegritydigest={dg}\n")
        except Exception:
            pass

    def capture_failure(self, headers, body: bytes, reason: str, diag: str):
        """업로드 실패 시 원본 본문 + 헤더를 captures/ 에 저장(분석용, 최대 5개)."""
        try:
            capdir = paths.data_path("captures")
            os.makedirs(capdir, exist_ok=True)
            if len([f for f in os.listdir(capdir) if f.endswith(".bin")]) >= 5:
                return  # 이미 충분히 캡처됨
            stamp = time.strftime("%Y%m%d_%H%M%S_") + str(len(body))
            with open(os.path.join(capdir, stamp + ".bin"), "wb") as f:
                f.write(body)
            with open(os.path.join(capdir, stamp + ".txt"), "w", encoding="utf-8") as f:
                f.write(f"reason: {reason}\ndiag: {diag}\n\n=== headers ===\n{headers}")
        except Exception:
            pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 기본 콘솔 스팸 억제
        pass

    # 공통 응답
    def _send(self, body: bytes, ctype="text/xml; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 부분수신 등으로 연결을 닫아야 하면(close_connection=True) 카드가 새 연결로 이어받게 함.
        self.send_header("Connection", "close" if self.close_connection else "keep-alive")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, progress_cb=None) -> bytes:
        # 견고한 본문 읽기: 부분 read 방지(끝까지 루프) + chunked 대응 + 진행 콜백(실시간 표시).
        te = (self.headers.get("Transfer-Encoding", "") or "").lower()
        if "chunked" in te:
            return self._read_chunked()
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return b""
        buf = bytearray()
        last = 0
        try:
            self.connection.settimeout(UPLOAD_STALL_TIMEOUT)  # 스톨 감지
        except OSError:
            pass
        while len(buf) < length:
            try:
                chunk = self.rfile.read(min(65536, length - len(buf)))   # 64KB 씩 → 진행 표시 가능
            except (socket.timeout, OSError):
                # 무데이터 스톨/링크 끊김 → 여기까지만(부분수신). 나머지는 다음 연결에서 offset 부터 이어받음.
                self.close_connection = True
                break
            if not chunk:
                self.close_connection = True
                break  # 연결이 조기 종료됨 (부분 수신)
            buf.extend(chunk)
            if progress_cb and (len(buf) - last >= 262144 or len(buf) >= length):
                try:
                    progress_cb(len(buf), length)
                except Exception:
                    pass
                last = len(buf)
        return bytes(buf)

    def _read_chunked(self) -> bytes:
        buf = bytearray()
        while True:
            line = self.rfile.readline()
            if not line:
                break
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                break
            if size == 0:
                self.rfile.readline()  # 마지막 CRLF
                break
            got = bytearray()
            while len(got) < size:
                part = self.rfile.read(size - len(got))
                if not part:
                    break
                got.extend(part)
            buf.extend(got)
            self.rfile.readline()  # 청크 뒤 CRLF
        return bytes(buf)

    def do_POST(self):
        srv: EyeFiServer = self.server  # type: ignore
        path = self.path.split("?", 1)[0]
        action = proto.soap_action_name(self.headers)
        ctype = self.headers.get("Content-Type", "")

        # 카드 TCP 스트림 붕괴(헤더에 바이너리 오염) → 이 연결은 못 믿음. 즉시 폐기해
        # 카드가 깨끗한 새 연결로 재시도하게 한다. (실측: 전파불량 시 tar 바이트가 헤더로 유입)
        if any(ord(c) < 32 or ord(c) > 126 for c in action):
            srv.emit("error", msg=f"오염된 요청 폐기 (action 바이너리, path={path[:40]!r})")
            self.close_connection = True
            try:
                self._send(b"", code=400)
            except Exception:
                pass
            return

        srv.emit("request", path=path, action=(action or "-"),
                 ctype=ctype[:48], cl=self.headers.get("Content-Length", "?"),
                 te=self.headers.get("Transfer-Encoding", "-"))

        # 업로드(멀티파트)
        if path.endswith("/upload") or "UploadPhoto" in action or "multipart" in ctype.lower():
            return self._handle_upload(srv, ctype)

        body = self._read_body()
        vals = proto.parse_soap_values(body)

        if action == "StartSession" or "macaddress" in vals and "cnonce" in vals and not action:
            return self._handle_start_session(srv, vals)
        if action == "GetPhotoStatus":
            return self._handle_get_photo_status(srv, vals)
        if action == "MarkLastPhotoInRoll":
            srv.emit("mark_last", mac=normalize_mac(vals.get("macaddress", "")))
            return self._send(proto.build_mark_last_photo_response())

        # 알 수 없는 액션도 세션 유지 위해 빈 200
        return self._send(proto.build_mark_last_photo_response())

    # --- 핸들러들 ---
    def _handle_start_session(self, srv: EyeFiServer, vals: dict):
        mac = normalize_mac(vals.get("macaddress", ""))
        cnonce = vals.get("cnonce", "")
        transfermode = vals.get("transfermode", "546")
        ts = vals.get("transfermodetimestamp", "0")
        card = srv.config.get_card(mac)
        if not card:
            srv.emit("unknown_card", mac=mac)
            # 등록 안 된 카드: 세션은 열어주되(응답 실패 방지) 업로드키 없으면 credential 무의미
            uploadkey = "0" * 32
        else:
            uploadkey = card["uploadkey"]
        snonce = proto.make_snonce()
        srv.sessions.setdefault(mac, deque(maxlen=8)).append(_Session(mac, cnonce, snonce))
        srv.emit("session", mac=mac, known=bool(card))
        resp = proto.build_start_session_response(mac, cnonce, uploadkey, snonce, transfermode, ts)
        self._send(resp)

    def _handle_get_photo_status(self, srv: EyeFiServer, vals: dict):
        mac = normalize_mac(vals.get("macaddress", ""))
        filename = vals.get("filename", "")
        filesize = int(vals.get("filesize", "0") or 0)
        card = srv.config.get_card(mac)

        # ★ 강제검증(2026-09-15): 등록 카드는 credential 이 최근 세션(최대 8개) 중
        # 하나의 (cnonce, snonce) 후보와 일치해야 offset 을 준다. 최근 세션 다중 대조는
        # 카드가 끊김 시 연결을 연달아 열어 snonce 가 연결마다 다른 경합 실측(0.6%) 대응.
        # 불일치 → 403 거부(카드는 StartSession 부터 재시도). 미등록 카드는 검증 불가라 통과.
        variant = None
        if card:
            got = vals.get("credential", "")
            for sess in reversed(srv.sessions.get(mac) or ()):   # 최신 세션부터
                variant = proto.match_credential(mac, sess.cnonce, sess.snonce,
                                                 card["uploadkey"], got)
                if variant:
                    break
            if not variant:
                srv.emit("photo_status", mac=mac, filename=filename, filesize=filesize,
                         offset=-1, cred_ok=False, cred_variant="", cred_got=got)
                return self._send(b"", code=403)

        offset = srv.spool_offset(mac, filename, filesize)   # 이어받기 지점
        srv.last_adv[(mac, filename)] = offset               # 카드가 아는 offset 기록(업로드 위치 판정용)
        if not hasattr(self, "_adv"):
            self._adv = {}
        self._adv[(mac, filename)] = offset
        if card:
            srv.emit("photo_status", mac=mac, filename=filename, filesize=filesize, offset=offset,
                     cred_ok=True, cred_variant=variant, cred_got=vals.get("credential", ""))
        self._send(proto.build_get_photo_status_response(fileid=1, offset=offset))

    def _handle_upload(self, srv: EyeFiServer, ctype: str):
        cl = int(self.headers.get("Content-Length", 0) or 0)
        srv.emit("upload_start", total=cl)   # 실시간 진행 팝업 시작

        m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype or "", re.I)
        boundary = (m.group(1) or m.group(2)).strip() if m else ""
        te = (self.headers.get("Transfer-Encoding", "") or "").lower()

        # 드문 경로(chunked/boundary 없음): 전체 읽고 일괄 처리
        if "chunked" in te or not boundary or cl <= 0:
            body = self._read_body(progress_cb=lambda got, total: srv.emit("upload_progress", got=got, total=total))
            return self._finish_upload(srv, ctype, cl, body, mac="", filename="", filesize=0,
                                       start_offset=0, streamed=0, stream_ok=False, token=None,
                                       payload_start=-1, expected_len=-1)

        # ---- 스트리밍 수신: FILENAME 파트 payload 를 받는 '즉시' spool 에 기록 ----
        # 언제 끊겨도 받은 만큼이 디스크에 있어 GetPhotoStatus 가 곧바로 정확한 offset 을 주고
        # (빠른 재접속에도 0부터 재시작 없음), 위치 전제조건(스풀 크기 == 기록 위치) 덕에
        # 옛 연결과의 어떤 경쟁에서도 스풀 손상이 불가능하다.
        # payload 의 끝은 '경계 문자열 탐색'이 아니라 프로토콜 산술(filesize-offset)로 확정한다:
        # 사진 바이트 안에 경계와 동일한 바이트열이 실제로 나타나 전송이 그 지점에서
        # 영원히 잘리는 사고가 실측됨 (DSC_8923, 2026-08-21).
        buf = bytearray()
        last_prog = 0
        mac = filename = ""
        filesize = 0
        payload_start = -1                 # buf 내 FILENAME payload 시작 인덱스
        payload_end = -1                   # payload 완료 표시(산술로 확정된 끝)
        expected_len = -1                  # 이 연결이 보낼 payload 바이트수(슬라이스 길이)
        streamed = 0                       # spool 에 기록 완료한 payload 바이트수
        start_offset = 0
        token = None
        stream_ok = False

        try:
            self.connection.settimeout(UPLOAD_STALL_TIMEOUT)
        except OSError:
            pass
        while len(buf) < cl:
            try:
                chunk = self.rfile.read(min(65536, cl - len(buf)))
            except (socket.timeout, OSError):
                self.close_connection = True
                break
            if not chunk:
                self.close_connection = True
                break
            buf.extend(chunk)
            if len(buf) - last_prog >= 262144 or len(buf) >= cl:
                srv.emit("upload_progress", got=len(buf), total=cl)
                last_prog = len(buf)

            # 1) SOAP 파트 + FILENAME 파트 헤더가 도착하면 메타 확정(1회)
            if payload_start < 0:
                hidx = buf.find(b'name="FILENAME"')
                if hidx < 0:
                    hidx = buf.find(b"name=FILENAME")
                if hidx >= 0:
                    hend = buf.find(b"\r\n\r\n", hidx)
                    if hend >= 0:
                        soap_pre = proto.split_multipart(bytes(buf[:hidx]), ctype).get("SOAPENVELOPE") or b""
                        vals = proto.parse_soap_values(soap_pre)
                        mac = normalize_mac(vals.get("macaddress", ""))
                        filename = vals.get("filename", "")
                        filesize = int(vals.get("filesize", "0") or 0)
                        payload_start = hend + 4
                        if mac and filename and filesize > 0:
                            adv = getattr(self, "_adv", {})
                            start_offset = adv.get((mac, filename),
                                                   srv.last_adv.get((mac, filename), 0))
                            # 슬라이스 길이 산술 검증: CL = 프리픽스 + payload + 트레일러(≤4KB).
                            # slack<0(본문이 슬라이스보다 짧게 끝남)은 전송 중 끊김과 같아 그대로 진행.
                            expected_len = filesize - start_offset
                            slack = cl - (payload_start + expected_len)
                            if slack > 4096:
                                if 0 <= cl - (payload_start + filesize) <= 4096:
                                    # 카드가 offset 을 무시하고 0부터 전체를 다시 보내는 경우
                                    start_offset = 0
                                    expected_len = filesize
                                else:
                                    expected_len = -1   # 산술 불일치 → 스트리밍 포기(RAM 폴백)
                            if expected_len > 0:
                                token = srv.claim_writer(mac, filename)
                                stream_ok = srv.spool_stream_begin(mac, filename, filesize, start_offset)

            # 2) payload 를 spool 로 흘려보냄 (끝 지점은 산술로 확정 — 경계 오인 불가).
            # 본문이 payload 도중 '완결형으로' 끝나는 경우 끝에 종료 트레일러가 붙어 있으므로,
            # 트레일러 길이만큼은 보류했다가 _finish_upload 에서 정확히 떼어 반영한다.
            if payload_start >= 0 and stream_ok and expected_len > 0 and payload_end < 0:
                trailer_hold = len(boundary) + 16
                appendable_end = min(len(buf) - trailer_hold, payload_start + expected_len)
                if appendable_end > payload_start + streamed:
                    if not srv.writer_valid(mac, filename, token):
                        self.close_connection = True   # 더 새 연결이 인수 → 이 연결 즉시 폐기
                        return
                    data = bytes(buf[payload_start + streamed:appendable_end])
                    if srv.spool_append_at(mac, filename, data, filesize, start_offset + streamed):
                        streamed += len(data)
                    else:
                        stream_ok = False              # 위치 어긋남(경쟁 감지) → RAM 폴백
                if streamed >= expected_len:
                    payload_end = payload_start + expected_len   # payload 완료(나머지는 트레일러)

        return self._finish_upload(srv, ctype, cl, bytes(buf), mac=mac, filename=filename,
                                   filesize=filesize, start_offset=start_offset,
                                   streamed=streamed, stream_ok=stream_ok, token=token,
                                   payload_start=payload_start, expected_len=expected_len)

    def _finish_upload(self, srv: EyeFiServer, ctype: str, cl: int, body: bytes,
                       mac: str, filename: str, filesize: int,
                       start_offset: int, streamed: int, stream_ok: bool, token,
                       payload_start: int, expected_len: int):
        parts = proto.split_multipart(body, ctype)
        soap = parts.get("SOAPENVELOPE") or parts.get("soapenvelope") or b""
        vals = proto.parse_soap_values(soap) if soap else {}
        mac = normalize_mac(vals.get("macaddress", "")) or mac
        filename = vals.get("filename", "") or filename
        filesize = int(vals.get("filesize", "0") or 0) or filesize
        if payload_start >= 0 and expected_len > 0:
            # 스트리밍 메타가 있으면 payload 를 산술로 정확히 잘라낸다.
            # (split_multipart 는 사진 안의 경계 유사 바이트열에 속아 잘라낼 수 있음)
            cut = len(body)
            if cut < payload_start + expected_len:
                # 본문이 payload 도중 끝남: 완결형 짧은 업로드라면 끝의 종료 트레일러 제거
                m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', ctype or "", re.I)
                if m:
                    term = ("\r\n--" + (m.group(1) or m.group(2)).strip() + "--").encode()
                    for t in (term + b"\r\n", term):
                        if body.endswith(t):
                            cut -= len(t)
                            break
            tar_bytes = body[payload_start:min(cut, payload_start + expected_len)] or None
        else:
            tar_bytes = parts.get("FILENAME") or parts.get("filename")
            if tar_bytes is None:
                for k, v in parts.items():
                    if k.upper() not in ("SOAPENVELOPE", "INTEGRITYDIGEST") and len(v) > 512:
                        tar_bytes = v
                        break
        # 무결성 검증식 보정용 캡처 (카드가 보낸 INTEGRITYDIGEST + 받은 데이터 저장)
        _digest = parts.get("INTEGRITYDIGEST") or parts.get("integritydigest")
        if _digest is not None and tar_bytes is not None:
            _card = srv.config.get_card(mac)
            srv.capture_integrity(mac, filename, filesize, tar_bytes, _digest,
                                  _card["uploadkey"] if _card else "")

        diag = (f"CL={cl} body={len(body)} filesize={filesize} "
                f"recv={len(tar_bytes) if tar_bytes else 0} streamed={streamed} file={filename}")

        if not filename or (tar_bytes is None and streamed == 0):
            srv.emit("upload_empty", mac=mac, diag=diag)
            srv.capture_failure(self.headers, body, "filename/tar 파트 없음", diag)
            try:
                self._send(proto.build_upload_photo_response(success=False))
            except Exception:
                pass
            return

        # 더 새 연결이 이 파일 수신을 인수했으면 이 연결은 물러남(스풀은 새 연결이 관리)
        if token is not None and not srv.writer_valid(mac, filename, token):
            self.close_connection = True
            try:
                self._send(proto.build_upload_photo_response(success=False))
            except Exception:
                pass
            return

        # 스풀 반영: 스트리밍이 기록 못 한 잔여분(경계 보류분 등)을 위치 검증하에 추가
        if tar_bytes is not None:
            if stream_ok:
                if streamed < len(tar_bytes):
                    srv.spool_append_at(mac, filename, tar_bytes[streamed:], filesize,
                                        start_offset + streamed)
            else:
                # RAM 폴백: 온전한 전체 파일이면 스풀을 새로 쓰고, 슬라이스면 위치가 맞을 때만
                if filesize and len(tar_bytes) >= filesize:
                    srv.spool_reset(mac, filename)
                    srv.spool_append_at(mac, filename, tar_bytes[:filesize], filesize, 0)
                elif filesize and len(body) >= cl > 0:
                    # 본문을 끝까지 받은 경우에만 슬라이스 시작위치를 역산할 수 있다
                    pos = filesize - len(tar_bytes)
                    if pos >= 0:
                        srv.spool_append_at(mac, filename, tar_bytes, filesize, pos)

        complete, assembled, got = srv.spool_try_complete(mac, filename, filesize)
        if not complete:
            srv.emit("partial", mac=mac, file=filename, got=got, need=filesize)
            # 아직 부분 → 카드가 다음 GetPhotoStatus 로 offset 부터 이어받음. spool 은 유지.
            try:
                self._send(proto.build_upload_photo_response(success=False))
            except Exception:
                pass  # 연결이 이미 끊겼을 수 있음(부분수신). spool 은 남아 다음에 이어감.
            return

        # 완료(spool 이 filesize 도달) → tar 추출·저장
        try:
            media = proto.extract_media_from_tar(assembled)
        except Exception as e:
            srv.emit("error", mac=mac, msg=f"조립 tar 추출 실패: {e} | {diag}")
            srv.capture_failure(self.headers, assembled, f"조립 tar 손상: {e}", diag)
            srv.spool_reset(mac, filename)   # 손상 → 리셋, 카드가 처음부터 재시도
            try:
                self._send(proto.build_upload_photo_response(success=False))
            except Exception:
                pass
            return
        saved = []
        for name, data, taken in media:
            if name.lower().endswith(".log"):
                continue
            saved.append(srv.save_media(mac, name, data, taken_epoch=taken))
        srv.spool_reset(mac, filename)       # 완료 → spool 정리
        try:
            self._send(proto.build_upload_photo_response(success=True))
        except Exception:
            pass
        if not saved:
            srv.emit("upload_empty", mac=mac, diag=diag)


def serve(config: Config, on_event=None) -> EyeFiServer:
    """서버 생성 후 백그라운드 스레드에서 serve_forever. 반환된 서버는 .shutdown() 으로 종료."""
    server = EyeFiServer(config, on_event=on_event)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
