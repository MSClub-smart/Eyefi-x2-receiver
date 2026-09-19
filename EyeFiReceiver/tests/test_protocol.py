"""
test_protocol.py — 프로토콜/서버 통합 검증 (실제 카드·공식앱 없이 오프라인 실행)

실행:
  cd "C:/EyeFiReceiver"
  python -m unittest EyeFiReceiver.tests.test_protocol -v

핵심: 가짜 카드가 서버에 multipart+tar 로 사진을 올리는 흐름을 그대로 모사해,
파일이 지정 폴더에 저장되는지 end-to-end 로 확인한다. (임의 포트 사용 → 공식앱 무간섭)
"""
import io
import os
import re
import socket
import sqlite3
import tarfile
import tempfile
import time
import unittest
import urllib.request

from EyeFiReceiver import eyefi_protocol as proto
from EyeFiReceiver.config import Config, normalize_mac
from EyeFiReceiver.eyefi_server import EyeFiServer
import threading

MAC = "00-18-56-12-34-56"
UPLOADKEY = "00000000000000000000000000000000"


def make_tar(filename: str, content: bytes, mtime: int = 0) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name=filename)
        info.size = len(content)
        info.mtime = mtime
        tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def make_multipart(soap_bytes: bytes, tar_bytes: bytes, boundary="EYEFIBOUNDARY"):
    b = ("--" + boundary).encode()
    parts = []
    parts.append(b + b'\r\nContent-Disposition: form-data; name="SOAPENVELOPE"\r\n\r\n' + soap_bytes + b"\r\n")
    parts.append(b + b'\r\nContent-Disposition: form-data; name="FILENAME"\r\n\r\n' + tar_bytes + b"\r\n")
    parts.append(b + b"--\r\n")
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class TestUnits(unittest.TestCase):
    def test_mac_normalize(self):
        self.assertEqual(normalize_mac("001856123456"), MAC)
        self.assertEqual(normalize_mac("00:18:56:12:34:56"), MAC)
        self.assertEqual(normalize_mac(MAC), MAC)

    def test_start_session_credential_deterministic(self):
        c1 = proto.start_session_credential(MAC.replace("-", ""), "aa" * 16, UPLOADKEY)
        c2 = proto.start_session_credential(MAC.replace("-", ""), "aa" * 16, UPLOADKEY)
        self.assertEqual(c1, c2)
        self.assertEqual(len(c1), 32)

    def test_credential_candidates_include_handoff(self):
        cands = proto.credential_candidates(MAC.replace("-", ""), "bb" * 16, "cc" * 16, UPLOADKEY)
        self.assertEqual(len(cands), 5)
        for c in cands:
            self.assertEqual(len(c), 32)

    def test_match_credential_returns_variant_label(self):
        import hashlib, binascii
        mac_hex, cn, sn = MAC.replace("-", ""), "bb" * 16, "cc" * 16
        # 오픈소스 eyefiserver 공식(mac+uploadkey+snonce)으로 만든 값 → 그 라벨로 일치
        good = hashlib.md5(binascii.unhexlify(mac_hex + UPLOADKEY + sn)).hexdigest()
        self.assertEqual(proto.match_credential(mac_hex, cn, sn, UPLOADKEY, good),
                         "mac+uploadkey+snonce")
        # 대소문자/공백 무해
        self.assertTrue(proto.match_credential(mac_hex, cn, sn, UPLOADKEY, " " + good.upper()))
        # 불일치/빈 값 → None
        self.assertIsNone(proto.match_credential(mac_hex, cn, sn, UPLOADKEY, "0" * 32))
        self.assertIsNone(proto.match_credential(mac_hex, cn, sn, UPLOADKEY, ""))

    def test_tcp_checksum_known_vectors(self):
        # 0x0201 → 1의 보수 0xFDFE → LE 바이트 FE FD
        self.assertEqual(proto.tcp_checksum(b"\x01\x02"), b"\xfe\xfd")
        # 홀수 길이는 0 패딩: b"\x01" → 워드 0x0001 → ~ = 0xFFFE → LE FE FF
        self.assertEqual(proto.tcp_checksum(b"\x01"), b"\xfe\xff")
        # 캐리 접기: 0xFFFF+0x0001 = 0x10000 → 접으면 0x0001 → ~ = 0xFFFE
        self.assertEqual(proto.tcp_checksum(b"\xff\xff\x01\x00"), b"\xfe\xff")

    def test_integrity_digest_deterministic(self):
        # 공식(후보) 자체의 회귀 고정값: 512B 'A' + 위 카드 키.
        # 수기 검산: 'AA' 워드(0x4141)×256 합=0x414100 → 접기 0x4100+0x41=0x4141
        # → ~ = 0xBEBE → 스트림 BE BE + unhex(key) → MD5
        d = proto.integrity_digest(b"A" * 512, UPLOADKEY)
        self.assertEqual(len(d), 32)
        self.assertEqual(d, proto.integrity_digest(b"A" * 512, UPLOADKEY))
        import hashlib, binascii
        expect = hashlib.md5(b"\xbe\xbe" + binascii.unhexlify(UPLOADKEY)).hexdigest()
        self.assertEqual(d, expect)

    def test_multipart_and_tar_roundtrip(self):
        tar = make_tar("DSC_0001.JPG", b"\xff\xd8fakejpeg\xff\xd9")
        body, ctype = make_multipart(b"<x/>", tar)
        parts = proto.split_multipart(body, ctype)
        self.assertIn("SOAPENVELOPE", parts)
        self.assertIn("FILENAME", parts)
        media = proto.extract_media_from_tar(parts["FILENAME"])
        self.assertEqual(media[0][0], "DSC_0001.JPG")
        self.assertTrue(media[0][1].startswith(b"\xff\xd8"))

    def test_truncated_tar_completion(self):
        # 끊김 반복으로 tar 끝 종료블록이 유실돼도, JPEG 데이터가 온전하면 완료로 인정
        jpeg = b"\xff\xd8" + b"P" * 3000 + b"\xff\xd9"
        full = make_tar("DSC_0002.JPG", jpeg)
        truncated = full[:-800]   # 끝 800B(종료 0블록 영역)만 잘라낸 부분 tar
        self.assertTrue(proto.tar_media_complete(truncated), "온전한 JPEG 인데 미완 판정")
        media = proto.extract_media_from_tar(truncated)
        self.assertEqual(media[0][0], "DSC_0002.JPG")
        self.assertEqual(media[0][1], jpeg)   # 사진 바이트 완전 일치
        # 반대로 JPEG 데이터 자체가 잘린 경우는 미완이어야 함
        # (헤더512 + 데이터 3004 중 앞 1500만 → 데이터 영역이 실제로 잘림)
        self.assertFalse(proto.tar_media_complete(full[:512 + 1500]), "데이터 잘렸는데 완료 판정")


class TestConfig(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cards.json")
            c = Config(p)
            c.default_folder = os.path.join(d, "down")
            c.add_or_update_card(MAC, UPLOADKEY, name="Test Card")
            c.save()
            c2 = Config.load(p)
            self.assertEqual(c2.default_folder, c.default_folder)
            self.assertEqual(c2.get_card(MAC)["uploadkey"], UPLOADKEY)
            self.assertEqual(c2.get_card(MAC)["name"], "Test Card")


class TestEndToEndUpload(unittest.TestCase):
    """가짜 카드 업로드 → 파일 저장까지 실제 HTTP 로 검증."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 수신 장부 격리 (save_media 가 실장부에 기록하지 않도록)
        from EyeFiReceiver import reader_import as ri
        self._ledger_orig = ri.LEDGER_PATH
        ri.LEDGER_PATH = os.path.join(self.tmp.name, "ledger.json")
        self.cfg = Config(os.path.join(self.tmp.name, "cards.json"))
        self.cfg.port = 0  # 임의 빈 포트
        self.cfg.default_folder = os.path.join(self.tmp.name, "down")
        self.cfg.date_subfolders = False
        self.cfg.add_or_update_card(MAC, UPLOADKEY, name="E2E")
        self.events = []
        self.server = EyeFiServer(self.cfg, on_event=lambda k, i: self.events.append((k, i)))
        self.server.spool_dir = os.path.join(self.tmp.name, "spool")   # 테스트 격리
        os.makedirs(self.server.spool_dir, exist_ok=True)
        self.port = self.server.server_address[1]
        self.t = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        from EyeFiReceiver import reader_import as ri
        ri.LEDGER_PATH = self._ledger_orig
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _post(self, body, ctype, action=None, path="/api/soap/eyefilm/v1"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body)
        req.add_header("Content-Type", ctype)
        if action:
            req.add_header("SOAPAction", f'"urn:{action}"')
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read()

    def _start_session(self, cnonce="dd" * 16):
        """StartSession 을 보내고 (cnonce, 서버 snonce) 반환."""
        ss = (
            '<?xml version="1.0"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="%s"><SOAP-ENV:Body>'
            '<StartSession xmlns="%s"><macaddress>%s</macaddress><cnonce>%s</cnonce>'
            "<transfermode>546</transfermode><transfermodetimestamp>0</transfermodetimestamp>"
            "</StartSession></SOAP-ENV:Body></SOAP-ENV:Envelope>"
        ) % (proto.SOAP_NS, proto.EYEFI_NS, MAC.replace("-", ""), cnonce)
        resp = self._post(ss.encode(), "text/xml", action="StartSession")
        snonce = re.search(rb"<snonce>([0-9a-fA-F]+)</snonce>", resp).group(1).decode()
        return cnonce, snonce

    @staticmethod
    def _card_credential(snonce):
        """카드가 GetPhotoStatus 에 넣는 credential (eyefiserver 공식: mac+uploadkey+snonce)."""
        import hashlib, binascii
        return hashlib.md5(binascii.unhexlify(MAC.replace("-", "") + UPLOADKEY + snonce)).hexdigest()

    def _gps(self, filename, fsize, credential):
        """GetPhotoStatus 요청 본문."""
        return (
            '<GetPhotoStatus xmlns="%s"><macaddress>%s</macaddress>'
            "<credential>%s</credential>"
            "<filename>%s</filename><filesize>%d</filesize></GetPhotoStatus>"
        ) % (proto.EYEFI_NS, MAC.replace("-", ""), credential, filename, fsize)

    def test_full_upload_flow(self):
        # 1) StartSession
        ss = (
            '<?xml version="1.0"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="%s"><SOAP-ENV:Body>'
            '<StartSession xmlns="%s"><macaddress>%s</macaddress><cnonce>%s</cnonce>'
            "<transfermode>546</transfermode><transfermodetimestamp>0</transfermodetimestamp>"
            "</StartSession></SOAP-ENV:Body></SOAP-ENV:Envelope>"
        ) % (proto.SOAP_NS, proto.EYEFI_NS, MAC.replace("-", ""), "dd" * 16)
        resp = self._post(ss.encode(), "text/xml", action="StartSession")
        self.assertIn(b"StartSessionResponse", resp)
        self.assertIn(b"snonce", resp)

        # 2) UploadPhoto (multipart + tar). filesize = tar 실제 크기(프로토콜대로)
        tar = make_tar("DSC_9999.JPG", b"\xff\xd8" + b"X" * 2000 + b"\xff\xd9")
        soap = (
            '<UploadPhoto xmlns="%s"><macaddress>%s</macaddress>'
            "<filename>DSC_9999.JPG.tar</filename><filesize>%d</filesize>"
            "</UploadPhoto>"
        ) % (proto.EYEFI_NS, MAC.replace("-", ""), len(tar))
        body, ctype = make_multipart(soap.encode(), tar)
        resp = self._post(body, ctype, action="UploadPhoto", path="/api/soap/eyefilm/v1/upload")
        self.assertIn(b"<success>true</success>", resp)

        # 3) 파일이 실제로 저장되었는가
        saved = os.path.join(self.cfg.default_folder, "DSC_9999.JPG")
        self.assertTrue(os.path.exists(saved), f"저장 안 됨: {saved}")
        with open(saved, "rb") as f:
            self.assertTrue(f.read().startswith(b"\xff\xd8"))

        kinds = [k for k, _ in self.events]
        self.assertIn("session", kinds)
        self.assertIn("received", kinds)

    def test_payload_containing_boundary_bytes(self):
        """사진 데이터 안에 multipart 경계와 동일한 바이트열이 들어 있어도
        잘리지 않고 원본과 바이트 단위로 일치 저장되어야 한다.
        (실측 사고: DSC_8923 이 경계 오인으로 매번 같은 지점에서 잘려 무한 재시도, 2026-08-21)"""
        mac_hex = MAC.replace("-", "")
        boundary = "------------------"          # 실카드와 같은 전부-하이픈 경계
        poison = b"\r\n" + b"-" * 24 + b"\r\n"   # 경계로 오인될 바이트열을 데이터 한복판에 심음
        payload = b"\xff\xd8" + b"A" * 5000 + poison + b"B" * 5000 + b"\xff\xd9"
        tar = make_tar("DSC_6666.JPG", payload)
        soap = (
            '<UploadPhoto xmlns="%s"><macaddress>%s</macaddress>'
            "<filename>DSC_6666.JPG.tar</filename><filesize>%d</filesize></UploadPhoto>"
        ) % (proto.EYEFI_NS, mac_hex, len(tar))
        body, ctype = make_multipart(soap.encode(), tar, boundary=boundary)
        resp = self._post(body, ctype, action="UploadPhoto", path="/api/soap/eyefilm/v1/upload")
        self.assertIn(b"<success>true</success>", resp)
        saved = os.path.join(self.cfg.default_folder, "DSC_6666.JPG")
        self.assertTrue(os.path.exists(saved), "경계 오인으로 저장 실패(회귀)")
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), payload, "저장 파일이 원본과 불일치(경계 오인 잘림)")

    def test_streaming_partial_live_offset(self):
        """전송 도중 소켓이 '뚝' 끊겨도(감지 지연 없이) 받은 바이트가 즉시 spool 에
        반영되어, 곧바로 온 GetPhotoStatus 가 0이 아닌 offset 을 줘야 한다(스트리밍 스풀).
        이어받기 후 저장 파일이 원본과 바이트 단위로 완전 일치해야 한다(무손상 불변식)."""
        mac_hex = MAC.replace("-", "")
        payload = b"\xff\xd8" + bytes(range(256)) * 4000 + b"\xff\xd9"   # 약 1MB JPEG
        tar = make_tar("DSC_5555.JPG", payload)
        fsize = len(tar)
        up_soap = (
            '<UploadPhoto xmlns="%s"><macaddress>%s</macaddress>'
            "<filename>DSC_5555.JPG.tar</filename><filesize>%d</filesize></UploadPhoto>"
        ) % (proto.EYEFI_NS, mac_hex, fsize)
        body, ctype = make_multipart(up_soap.encode(), tar)

        # 1) raw socket 으로 업로드 본문을 절반만 보내고 즉시 끊는다(끊김 흉내)
        half = len(body) // 2
        req = (
            f"POST /api/soap/eyefilm/v1/upload HTTP/1.1\r\nHost: t\r\n"
            f"Content-Type: {ctype}\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode() + body[:half]
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(req)
        s.close()

        # 2) 곧바로 GetPhotoStatus → offset 이 이미 받은 만큼(≈half) 나와야 함
        #    (강제검증: 실카드처럼 StartSession 후 credential 포함)
        _, snonce = self._start_session()
        gs = self._gps("DSC_5555.JPG.tar", fsize, self._card_credential(snonce))
        offset, prev = 0, -1
        for _ in range(40):   # offset 이 '멈출 때'까지 폴링(스트리밍 중엔 계속 자람, 최대 10초)
            resp = self._post(gs.encode(), "text/xml", action="GetPhotoStatus")
            offset = int(re.search(rb"<offset>(\d+)</offset>", resp).group(1))
            if offset > 0 and offset == prev:
                break
            prev = offset
            time.sleep(0.25)
        self.assertGreater(offset, half // 2, "스트리밍 스풀 미동작: 끊김 직후 offset 이 너무 작음")
        self.assertLessEqual(offset, fsize, "offset 이 filesize 초과(스풀 오염)")

        # 3) 이어받기: offset 부터 나머지 전송 → 완료·저장
        body2, ctype2 = make_multipart(up_soap.encode(), tar[offset:])
        resp = self._post(body2, ctype2, action="UploadPhoto", path="/api/soap/eyefilm/v1/upload")
        self.assertIn(b"<success>true</success>", resp)
        saved = os.path.join(self.cfg.default_folder, "DSC_5555.JPG")
        self.assertTrue(os.path.exists(saved), "이어받기 후 저장 안 됨")
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), payload, "저장 파일이 원본과 불일치(손상)")

    def test_resumable_upload(self):
        """부분 업로드 → GetPhotoStatus offset → 이어받기 → 완료·저장 (핵심 시나리오)."""
        mac_hex = MAC.replace("-", "")
        tar = make_tar("DSC_7777.JPG", b"\xff\xd8" + bytes(range(256)) * 40 + b"\xff\xd9")
        fsize = len(tar)
        half = fsize // 2
        up_soap = (
            '<UploadPhoto xmlns="%s"><macaddress>%s</macaddress>'
            "<filename>DSC_7777.JPG.tar</filename><filesize>%d</filesize></UploadPhoto>"
        ) % (proto.EYEFI_NS, mac_hex, fsize)
        saved = os.path.join(self.cfg.default_folder, "DSC_7777.JPG")

        # 1) 앞 half 바이트만 업로드(연결 끊김 흉내) → 미완료, 저장 안 됨
        body, ctype = make_multipart(up_soap.encode(), tar[:half])
        resp = self._post(body, ctype, action="UploadPhoto", path="/api/soap/eyefilm/v1/upload")
        self.assertIn(b"<success>false</success>", resp)
        self.assertFalse(os.path.exists(saved), "부분 업로드인데 저장됨(버그)")

        # 2) GetPhotoStatus → offset == half (이어받을 지점) — credential 포함(강제검증)
        _, snonce = self._start_session()
        gs = self._gps("DSC_7777.JPG.tar", fsize, self._card_credential(snonce))
        resp = self._post(gs.encode(), "text/xml", action="GetPhotoStatus")
        self.assertIn(("<offset>%d</offset>" % half).encode(), resp)

        # 3) 이어받기: 나머지 바이트 전송 → 완료·저장, 내용 일치
        body, ctype = make_multipart(up_soap.encode(), tar[half:])
        resp = self._post(body, ctype, action="UploadPhoto", path="/api/soap/eyefilm/v1/upload")
        self.assertIn(b"<success>true</success>", resp)
        self.assertTrue(os.path.exists(saved), "이어받기 후에도 저장 안 됨")
        with open(saved, "rb") as f:
            data = f.read()
        self.assertTrue(data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"))

    def test_get_photo_status_rejects_bad_credential(self):
        """강제검증: 등록 카드의 GetPhotoStatus 는 credential 이 틀리면/없으면 403."""
        self._start_session()
        # 틀린 credential → 403
        gs = self._gps("DSC_1111.JPG.tar", 1024, "0" * 32)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post(gs.encode(), "text/xml", action="GetPhotoStatus")
        self.assertEqual(cm.exception.code, 403)
        # credential 자체가 없어도 403
        gs = (
            '<GetPhotoStatus xmlns="%s"><macaddress>%s</macaddress>'
            "<filename>DSC_1111.JPG.tar</filename><filesize>1024</filesize></GetPhotoStatus>"
        ) % (proto.EYEFI_NS, MAC.replace("-", ""))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post(gs.encode(), "text/xml", action="GetPhotoStatus")
        self.assertEqual(cm.exception.code, 403)
        # 거부 이벤트가 남는다
        rejected = [i for k, i in self.events if k == "photo_status" and not i.get("cred_ok")]
        self.assertEqual(len(rejected), 2)

    def test_get_photo_status_accepts_older_recent_session(self):
        """세션 경합 내성: 카드가 연결을 연달아 열어 snonce 가 갈려도(실측 0.6%)
        '이전' 세션의 snonce 로 만든 credential 이 여전히 통과해야 한다."""
        _, snonce1 = self._start_session(cnonce="ee" * 16)
        self._start_session(cnonce="ff" * 16)   # 새 세션이 덮어도
        gs = self._gps("DSC_2222.JPG.tar", 1024, self._card_credential(snonce1))
        resp = self._post(gs.encode(), "text/xml", action="GetPhotoStatus")
        self.assertIn(b"<offset>", resp)
        ok = [i for k, i in self.events if k == "photo_status" and i.get("cred_ok")]
        self.assertEqual(ok[-1].get("cred_variant"), "mac+uploadkey+snonce")


class TestWatchdog(unittest.TestCase):
    """미전송 감시: 스풀에 멈춘 부분수신 파일 감지 + 재알림 쿨다운."""

    def _make_part(self, spool, mac, filename, size, need=None, age_sec=0):
        d = os.path.join(spool, mac)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, filename + ".part")
        with open(p, "wb") as f:
            f.write(b"x" * size)
        if need is not None:
            with open(p + ".size", "w") as f:
                f.write(str(need))
        old = time.time() - age_sec
        os.utime(p, (old, old))
        return p

    def test_find_stalled(self):
        from EyeFiReceiver.eyefi_server import find_stalled
        with tempfile.TemporaryDirectory() as spool:
            # 오래 멈춘 파일 → 감지 (need 포함)
            self._make_part(spool, MAC, "DSC_9001.JPG.tar", 1000, need=4000, age_sec=700)
            # 방금 갱신된 파일(전송 중) → 미감지
            self._make_part(spool, MAC, "DSC_9002.JPG.tar", 500, age_sec=10)
            # 빈 파일 → 미감지
            self._make_part(spool, MAC, "DSC_9003.JPG.tar", 0, age_sec=700)
            # .part 아닌 파일 → 무시
            with open(os.path.join(spool, MAC, "noise.txt"), "w") as f:
                f.write("x")
            got = find_stalled(spool, time.time(), stall_after=600)
            self.assertEqual(len(got), 1)
            s = got[0]
            self.assertEqual(s["mac"], MAC)
            self.assertEqual(s["file"], "DSC_9001.JPG.tar")
            self.assertEqual(s["got"], 1000)
            self.assertEqual(s["need"], 4000)
            self.assertGreaterEqual(s["age_sec"], 600)

    def test_watchdog_tick_cooldown_and_cleanup(self):
        from EyeFiReceiver import eyefi_server as es
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "cards.json"))
            cfg.port = 0
            cfg.default_folder = os.path.join(tmp, "down")
            events = []
            srv = EyeFiServer(cfg, on_event=lambda k, i: events.append((k, i)))
            try:
                srv.spool_dir = os.path.join(tmp, "spool")
                os.makedirs(srv.spool_dir, exist_ok=True)
                p = self._make_part(srv.spool_dir, MAC, "DSC_9010.JPG.tar", 2048,
                                    need=8192, age_sec=es.STALL_ALERT_AFTER + 100)
                now = time.time()
                srv._watchdog_tick(now)              # 첫 감지 → 알림
                srv._watchdog_tick(now + 60)         # 쿨다운 내 → 알림 없음
                stall_events = [i for k, i in events if k == "stalled"]
                self.assertEqual(len(stall_events), 1)
                self.assertEqual(stall_events[0]["files"][0]["file"], "DSC_9010.JPG.tar")
                srv._watchdog_tick(now + es.STALL_REALERT + 61)   # 쿨다운 경과 → 재알림
                stall_events = [i for k, i in events if k == "stalled"]
                self.assertEqual(len(stall_events), 2)
                # 파일 완료(스풀 제거) → 알림 기록 청소 → 재등장 시 즉시 알림
                os.remove(p)
                os.remove(p + ".size")
                srv._watchdog_tick(now + es.STALL_REALERT + 120)
                self.assertEqual(srv._stall_alerted, {})
            finally:
                srv.server_close()

    def test_watchdog_giveup_archives_old_partial(self):
        """며칠째 진행 없는 부분수신 → .abandoned 보관 + 1회 알림 + 이후 무음."""
        from EyeFiReceiver import eyefi_server as es
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "cards.json"))
            cfg.port = 0
            cfg.default_folder = os.path.join(tmp, "down")
            events = []
            srv = EyeFiServer(cfg, on_event=lambda k, i: events.append((k, i)))
            try:
                srv.spool_dir = os.path.join(tmp, "spool")
                os.makedirs(srv.spool_dir, exist_ok=True)
                old = self._make_part(srv.spool_dir, MAC, "DSC_9110.JPG.tar", 1024,
                                      need=4096, age_sec=es.STALL_GIVEUP_AFTER + 60)
                young = self._make_part(srv.spool_dir, MAC, "DSC_9200.JPG.tar", 512,
                                        need=4096, age_sec=es.STALL_ALERT_AFTER + 60)
                srv._watchdog_tick(time.time())
                giveups = [i for k, i in events if k == "stall_giveup"]
                self.assertEqual(len(giveups), 1)
                self.assertEqual(giveups[0]["file"], "DSC_9110.JPG.tar")
                self.assertFalse(os.path.exists(old))                    # .part 는 사라지고
                self.assertTrue(os.path.exists(old + ".abandoned"))      # 보관본이 남음
                self.assertFalse(os.path.exists(old + ".size"))
                self.assertTrue(os.path.exists(young))                   # 젊은 파일은 그대로
                stalls = [i for k, i in events if k == "stalled"]
                self.assertEqual(len(stalls), 1)                         # 젊은 파일만 경고
                self.assertEqual(stalls[0]["files"][0]["file"], "DSC_9200.JPG.tar")
                events.clear()
                srv._watchdog_tick(time.time())                          # 다음 틱: 포기건 무음
                self.assertEqual([k for k, _ in events if k == "stall_giveup"], [])
            finally:
                srv.server_close()

    def test_config_wifi_fields_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cards.json")
            c = Config(p)
            c.wifi_ssid = "Eyefi"
            c.wifi_key = "secret!"
            c.save()
            c2 = Config.load(p)
            self.assertEqual(c2.wifi_ssid, "Eyefi")
            self.assertEqual(c2.wifi_key, "secret!")


class TestReaderImport(unittest.TestCase):
    """리더 직접 가져오기: 복사/중복 스킵/날짜 라우팅/장부 + 서버 저장의 중복 스킵."""

    def setUp(self):
        # 수신 장부를 임시 파일로 격리 (실장부 오염 방지)
        from EyeFiReceiver import reader_import as ri
        self._ledger_tmp = tempfile.TemporaryDirectory()
        self._ledger_orig = ri.LEDGER_PATH
        ri.LEDGER_PATH = os.path.join(self._ledger_tmp.name, "ledger.json")

    def tearDown(self):
        from EyeFiReceiver import reader_import as ri
        ri.LEDGER_PATH = self._ledger_orig
        self._ledger_tmp.cleanup()

    def _make_card(self, root, names, day: str):
        d = os.path.join(root, "DCIM", "272D7200")
        os.makedirs(d, exist_ok=True)
        os.makedirs(os.path.join(root, "EYEFI"), exist_ok=True)
        ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + 12 * 3600
        out = []
        for n, content in names:
            p = os.path.join(d, n)
            with open(p, "wb") as f:
                f.write(content)
            os.utime(p, (ts, ts))
            out.append(p)
        return out

    def test_import_copy_skip_and_route(self):
        from EyeFiReceiver import reader_import as ri
        import datetime as dt
        today = dt.date.today().isoformat()
        with tempfile.TemporaryDirectory() as card, tempfile.TemporaryDirectory() as dest:
            self._make_card(card, [
                ("DSC_0001.JPG", b"A" * 100),   # 새 파일 → 복사
                ("DSC_0002.JPG", b"B" * 200),   # 대상에 같은 이름+크기 존재 → 스킵
                ("DSC_0003.JPG", b"C" * 300),   # 같은 이름·다른 크기 존재 → _r1 별도 저장
                ("NOTE.TXT", b"x"),             # 미디어 아님 → 무시
            ], today)
            os.makedirs(os.path.join(dest, today), exist_ok=True)
            with open(os.path.join(dest, today, "DSC_0002.JPG"), "wb") as f:
                f.write(b"B" * 200)
            with open(os.path.join(dest, today, "DSC_0003.JPG"), "wb") as f:
                f.write(b"Z" * 10)
            r = ri.import_photos(card, dest, date_subfolders=True)
            self.assertEqual(r["errors"], [])
            self.assertEqual(r["skipped"], 1)
            copied = sorted(os.path.basename(p) for p in r["copied"])
            self.assertEqual(copied, ["DSC_0001.JPG", "DSC_0003_r1.JPG"])
            self.assertTrue(os.path.exists(os.path.join(dest, today, "DSC_0001.JPG")))
            with open(os.path.join(dest, today, "DSC_0003_r1.JPG"), "rb") as f:
                self.assertEqual(f.read(), b"C" * 300)

    def test_ledger_blocks_reimport_after_files_moved(self):
        """받은 사진을 직원이 옮겨가(폴더 비움) 재삽입해도 장부로 재복사 차단."""
        from EyeFiReceiver import reader_import as ri
        import datetime as dt
        today = dt.date.today().isoformat()
        with tempfile.TemporaryDirectory() as card, tempfile.TemporaryDirectory() as dest:
            self._make_card(card, [("DSC_0010.JPG", b"D" * 150)], today)
            r1 = ri.import_photos(card, dest)
            self.assertEqual(len(r1["copied"]), 1)
            os.remove(r1["copied"][0])          # 직원이 옮겨감(폴더에서 사라짐)
            r2 = ri.import_photos(card, dest)   # 재삽입
            self.assertEqual(len(r2["copied"]), 0)
            self.assertEqual(r2["skipped"], 1)

    def test_wifi_receive_records_ledger(self):
        """Wi-Fi 수신도 장부에 기록 → 이후 리더 가져오기가 스킵."""
        from EyeFiReceiver import reader_import as ri
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "cards.json"))
            cfg.port = 0
            cfg.default_folder = os.path.join(tmp, "down")
            cfg.add_or_update_card(MAC, UPLOADKEY)
            srv = EyeFiServer(cfg, on_event=lambda k, i: None)
            try:
                srv.save_media(MAC, "DSC_0020.JPG", b"E" * 777)
            finally:
                srv.server_close()
            self.assertTrue(ri.ledger_has("DSC_0020.JPG", 777))

    def test_save_media_routes_by_capture_date(self):
        """저장 날짜폴더 = 촬영일(tar mtime 의 gmtime 해석). 비정상 mtime 은 오늘로 폴백."""
        import calendar
        import datetime as dt
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "cards.json"))
            cfg.port = 0
            cfg.default_folder = os.path.join(tmp, "down")
            cfg.date_subfolders = True
            cfg.add_or_update_card(MAC, UPLOADKEY)
            srv = EyeFiServer(cfg, on_event=lambda k, i: None)
            try:
                today = dt.date.today()
                yday = today - dt.timedelta(days=1)
                # 어제 정오 촬영(카드 방식: 로컬시각을 UTC 로 취급한 epoch)
                ep = calendar.timegm((yday.year, yday.month, yday.day, 12, 0, 0))
                p = srv.save_media(MAC, "DSC_0200.JPG", b"Y" * 100, taken_epoch=ep)
                self.assertIn(yday.isoformat(), p)
                # mtime=0(정보 없음) → 오늘
                p = srv.save_media(MAC, "DSC_0201.JPG", b"Z" * 100, taken_epoch=0)
                self.assertIn(today.isoformat(), p)
                # 미래 mtime(시계 고장) → 오늘
                fut = calendar.timegm((today.year + 1, 1, 1, 0, 0, 0))
                p = srv.save_media(MAC, "DSC_0202.JPG", b"W" * 100, taken_epoch=fut)
                self.assertIn(today.isoformat(), p)
            finally:
                srv.server_close()

    def test_upload_saves_to_capture_date_folder(self):
        """끝-끝: 어제 촬영분이 오늘 전송돼도 어제 날짜 폴더에 저장."""
        import calendar
        import datetime as dt
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "cards.json"))
            cfg.port = 0
            cfg.default_folder = os.path.join(tmp, "down")
            cfg.date_subfolders = True
            cfg.add_or_update_card(MAC, UPLOADKEY)
            srv = EyeFiServer(cfg, on_event=lambda k, i: None)
            srv.spool_dir = os.path.join(tmp, "spool")
            os.makedirs(srv.spool_dir, exist_ok=True)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                yday = dt.date.today() - dt.timedelta(days=1)
                ep = calendar.timegm((yday.year, yday.month, yday.day, 15, 30, 0))
                tar = make_tar("DSC_0300.JPG", b"\xff\xd8" + b"Q" * 2000 + b"\xff\xd9", mtime=ep)
                mac_hex = MAC.replace("-", "")
                soap = (
                    '<UploadPhoto xmlns="%s"><macaddress>%s</macaddress>'
                    "<filename>DSC_0300.JPG.tar</filename><filesize>%d</filesize></UploadPhoto>"
                ) % (proto.EYEFI_NS, mac_hex, len(tar))
                body, ctype = make_multipart(soap.encode(), tar)
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/soap/eyefilm/v1/upload", data=body)
                req.add_header("Content-Type", ctype)
                req.add_header("SOAPAction", '"urn:UploadPhoto"')
                with urllib.request.urlopen(req, timeout=10) as r:
                    self.assertIn(b"<success>true</success>", r.read())
                expect = os.path.join(cfg.default_folder, yday.isoformat(), "DSC_0300.JPG")
                self.assertTrue(os.path.exists(expect), f"촬영일 폴더에 없음: {expect}")
            finally:
                srv.shutdown()
                srv.server_close()

    def test_save_media_skips_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "cards.json"))
            cfg.port = 0
            cfg.default_folder = os.path.join(tmp, "down")
            cfg.date_subfolders = True
            cfg.add_or_update_card(MAC, UPLOADKEY)
            events = []
            srv = EyeFiServer(cfg, on_event=lambda k, i: events.append((k, i)))
            try:
                p1 = srv.save_media(MAC, "DSC_0100.JPG", b"J" * 500)
                self.assertTrue(os.path.exists(p1))
                p2 = srv.save_media(MAC, "DSC_0100.JPG", b"J" * 500)   # 같은 이름+크기 → 스킵
                self.assertEqual(p1, p2)
                self.assertFalse(os.path.exists(p1.replace(".JPG", "_1.JPG")))
                kinds = [k for k, _ in events]
                self.assertEqual(kinds.count("received"), 1)
                self.assertEqual(kinds.count("duplicate"), 1)
                p3 = srv.save_media(MAC, "DSC_0100.JPG", b"K" * 999)   # 다른 크기 → _1 저장
                self.assertNotEqual(p3, p1)
                self.assertTrue(os.path.exists(p3))
            finally:
                srv.server_close()


class TestCardMailbox(unittest.TestCase):
    """순수 파이썬 카드 프로토콜의 파싱·빌드 로직 (하드웨어 없이 오프라인 검증).
    라이브 카드 대조(read/write=DLL 일치)는 별도로 완료됨."""

    def test_pascal_parse(self):
        from EyeFiReceiver.card_mailbox import CardMailbox
        # pascal_string: [len][value...]
        self.assertEqual(CardMailbox._pascal(b"\x03abcXXXX"), b"abc")
        self.assertEqual(CardMailbox._pascal(b"\x00"), b"")
        self.assertEqual(CardMailbox._pascal(b""), b"")
        # 32바이트 업로드키 형태
        key = b"22222222222222222222222222222222"
        self.assertEqual(CardMailbox._pascal(bytes([len(key)]) + key + b"\x00" * 5), key)

    def test_wpa_key_pbkdf2(self):
        from EyeFiReceiver.card_mailbox import CardMailbox
        import hashlib
        # ASCII 비번 → PBKDF2-SHA1(pass, essid, 4096, 32B)
        got = CardMailbox._wpa_key("example-ssid", "EXAMPLE-wifi-pass")
        want = hashlib.pbkdf2_hmac("sha1", b"EXAMPLE-wifi-pass", b"example-ssid", 4096, 32)
        self.assertEqual(got, want)
        self.assertEqual(len(got), 32)
        # 64 hex 비번 → raw 32바이트 그대로
        hexpw = "ab" * 32
        self.assertEqual(CardMailbox._wpa_key("x", hexpw), bytes.fromhex(hexpw))

    def test_build_net_request_layout(self):
        from EyeFiReceiver.card_mailbox import CardMailbox, ESSID_LEN
        req = CardMailbox.build_net_request("a", "Eyefi", "password123")
        # req(1) + essid_len(1) + essid[32] + keylen(1) + wpa[32] = 67
        self.assertEqual(len(req), 1 + 1 + ESSID_LEN + 1 + 32)
        self.assertEqual(req[0:1], b"a")
        self.assertEqual(req[1], len("Eyefi"))
        self.assertEqual(req[2:2 + 5], b"Eyefi")
        self.assertEqual(req[2 + ESSID_LEN], 32)   # 키 길이
        # 삭제 요청은 키 없음(0)
        d = CardMailbox.build_net_request("d", "Eyefi", None)
        self.assertEqual(d[0:1], b"d")
        self.assertEqual(d[2 + ESSID_LEN], 0)

    def test_parse_net_list(self):
        from EyeFiReceiver.card_mailbox import CardMailbox, ESSID_LEN
        mb = CardMailbox.__new__(CardMailbox)   # 하드웨어 init 없이 파서만
        # nr=2, 각 essid[32] null 종료
        def essid(s):
            b = s.encode()
            return b + b"\x00" * (ESSID_LEN - len(b))
        raw = bytes([2]) + essid("Eyefi") + essid("example-ssid")
        self.assertEqual(mb._parse_net_list(raw, has_meta=False), ["Eyefi", "example-ssid"])
        self.assertEqual(mb._parse_net_list(b"\x00", has_meta=False), [])

    def test_mac_format_from_pascal(self):
        # get_mac 의 포맷 로직: [len=6][6 raw bytes] → 00-18-56-xx-xx-xx
        raw = bytes([6]) + bytes([0x00, 0x18, 0x56, 0x12, 0x34, 0x58])
        from EyeFiReceiver.card_mailbox import CardMailbox
        h = CardMailbox._pascal(raw)
        mac = "00-18-56-%02x-%02x-%02x" % (h[3], h[4], h[5])
        self.assertEqual(mac, "00-18-56-12-34-58")


class TestReaderOnlyCard(unittest.TestCase):
    """키 없는 '리더 전용' 카드: 자동 가져오기용으로 등록되고, 무선은 안전히 거부."""

    def test_config_stores_empty_key(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cards.json")
            c = Config(p)
            c.add_or_update_card(MAC, "", name="리더 전용")   # 키 없이 등록
            c.save()
            c2 = Config.load(p)
            card = c2.get_card(MAC)
            self.assertIsNotNone(card)                         # 등록됨 → 자동 가져오기 게이트 통과
            self.assertEqual(card["uploadkey"], "")
            self.assertEqual(card["name"], "리더 전용")

    def test_empty_key_card_wifi_safely_rejected(self):
        """빈 키 카드가 무선으로 붙어도 크래시 없이 credential 불일치로 거부(403)."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(os.path.join(tmp, "cards.json"))
            cfg.port = 0
            cfg.default_folder = os.path.join(tmp, "down")
            cfg.add_or_update_card(MAC, "", name="리더 전용")   # 키 없음
            srv = EyeFiServer(cfg, on_event=lambda k, i: None)
            srv.spool_dir = os.path.join(tmp, "spool")
            os.makedirs(srv.spool_dir, exist_ok=True)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                mac_hex = MAC.replace("-", "")
                # StartSession (크래시 없이 응답)
                ss = ('<?xml version="1.0"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="%s"><SOAP-ENV:Body>'
                      '<StartSession xmlns="%s"><macaddress>%s</macaddress><cnonce>%s</cnonce>'
                      "<transfermode>546</transfermode><transfermodetimestamp>0</transfermodetimestamp>"
                      "</StartSession></SOAP-ENV:Body></SOAP-ENV:Envelope>"
                      ) % (proto.SOAP_NS, proto.EYEFI_NS, mac_hex, "dd" * 16)
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/soap/eyefilm/v1",
                                             data=ss.encode())
                req.add_header("Content-Type", "text/xml")
                req.add_header("SOAPAction", '"urn:StartSession"')
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertIn(b"StartSessionResponse", r.read())
                # GetPhotoStatus with any credential → 403 (빈 키라 절대 일치 안 함)
                gs = ('<GetPhotoStatus xmlns="%s"><macaddress>%s</macaddress>'
                      "<credential>%s</credential><filename>X.JPG.tar</filename>"
                      "<filesize>10</filesize></GetPhotoStatus>"
                      ) % (proto.EYEFI_NS, mac_hex, "0" * 32)
                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/soap/eyefilm/v1",
                                             data=gs.encode())
                req.add_header("Content-Type", "text/xml")
                req.add_header("SOAPAction", '"urn:GetPhotoStatus"')
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(cm.exception.code, 403)
            finally:
                srv.shutdown()
                srv.server_close()


class TestKeyRecovery(unittest.TestCase):
    """구펌웨어로 업로드키를 못 읽는 카드: 예전 Eye-Fi 설정에서 복구."""

    def test_extract_from_xml_settings(self):
        from EyeFiReceiver import key_recovery as kr
        xml = (
            '<Config version="2.1">'
            '<Card><MacAddress>0018561234 60</MacAddress>'
            '<UploadKey>33333333333333333333333333333333</UploadKey></Card>'
            '<Card><MacAddress>00-18-56-99-99-99</MacAddress>'
            '<UploadKey>ffffffffffffffffffffffffffffffff</UploadKey></Card>'
            '</Config>')
        # 대상 카드만 정확히
        self.assertEqual(kr.extract_from_text(xml, "00-18-56-12-34-60"),
                         "33333333333333333333333333333333")
        self.assertEqual(kr.extract_from_text(xml, "00-18-56-99-99-99"),
                         "ffffffffffffffffffffffffffffffff")
        # 없는 MAC → None
        self.assertIsNone(kr.extract_from_text(xml, "00-18-56-aa-bb-cc"))

    def test_extract_from_sqlite(self):
        from EyeFiReceiver import key_recovery as kr
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "client.db")
            con = sqlite3.connect(p)
            con.execute("CREATE TABLE o_devices (o_mac_address TEXT, o_upload_key TEXT, note TEXT)")
            con.execute("INSERT INTO o_devices VALUES (?,?,?)",
                        ("00-18-56-12-34-60", "33333333333333333333333333333333", "clinic"))
            con.execute("INSERT INTO o_devices VALUES (?,?,?)",
                        ("0018561234aa", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "x"))
            con.commit(); con.close()
            self.assertEqual(kr.extract_from_sqlite(p, "00-18-56-12-34-60"),
                             "33333333333333333333333333333333")
            self.assertEqual(kr.extract_from_sqlite(p, "00:18:56:12:34:aa"),
                             "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
            self.assertIsNone(kr.extract_from_sqlite(p, "00-18-56-00-00-00"))

    def test_recover_from_file_dispatch(self):
        from EyeFiReceiver import key_recovery as kr
        with tempfile.TemporaryDirectory() as d:
            xp = os.path.join(d, "Settings.xml")
            with open(xp, "w", encoding="utf-8") as f:
                f.write('<Config><MacAddress>00-18-56-12-34-60</MacAddress>'
                        '<UploadKey>33333333333333333333333333333333</UploadKey></Config>')
            self.assertEqual(kr.recover_from_file(xp, "00-18-56-12-34-60"),
                             "33333333333333333333333333333333")

    def test_recover_upload_key_prefers_extra_file(self):
        from EyeFiReceiver import key_recovery as kr
        with tempfile.TemporaryDirectory() as d:
            xp = os.path.join(d, "Settings.xml")
            with open(xp, "w", encoding="utf-8") as f:
                f.write('<Config><MacAddress>00-18-56-12-34-60</MacAddress>'
                        '<UploadKey>33333333333333333333333333333333</UploadKey></Config>')
            hit = kr.recover_upload_key("00-18-56-12-34-60", extra_files=[xp])
            self.assertIsNotNone(hit)
            key, src = hit
            self.assertEqual(key, "33333333333333333333333333333333")
            self.assertEqual(src, xp)
            # 없는 카드 → None (기본 위치에 없다고 가정)
            self.assertIsNone(kr.recover_upload_key("00-18-56-de-ad-00", extra_files=[xp]))


class TestFreshCardBootstrap(unittest.TestCase):
    """공장 초기 카드 지원 — 시퀀스 가드·메일박스 부트스트랩·판별 (실카드 없이 검증)."""

    def test_init_seq_erased_flash_guard(self):
        # RSPC 가 지워진 플래시(0xFF..)를 돌려줘도 안전한 시작값(0x1234)으로 초기화
        from EyeFiReceiver import card_mailbox as cm
        orig_r, orig_w = cm._read_raw, cm._write_raw
        cm._read_raw = lambda path, n=cm.BUF_SIZE: b"\xff" * n
        cm._write_raw = lambda path, data: None
        try:
            mb = cm.CardMailbox("X:\\")
            self.assertEqual(mb.seq, 0x1234 + 1)
        finally:
            cm._read_raw, cm._write_raw = orig_r, orig_w

    def test_ensure_mailbox_creates_16k_files(self):
        from EyeFiReceiver import card_mailbox as cm
        orig_w = cm._write_raw
        cm._write_raw = lambda path, data: None  # 캐시 우회 쓰기는 실카드 전용 → 무력화
        try:
            with tempfile.TemporaryDirectory() as td:
                cm.ensure_mailbox(td)
                for name in cm.MAILBOX_FILES:
                    p = os.path.join(td, cm.EYEFI_DIR, name)
                    self.assertTrue(os.path.isfile(p), name)
                    self.assertEqual(os.path.getsize(p), cm.BUF_SIZE, name)
        finally:
            cm._write_raw = orig_w

    def test_probe_bootstrap_recovers_formatted_card(self):
        """카메라 포맷 카드(EYEFI 폴더·'Eye-Fi' 라벨 소실): 탐침이 메일박스를 만들고
        카드 펌웨어가 응답하면 True + 메일박스 유지(복구). 가짜 펌웨어 = REQC 에코."""
        from EyeFiReceiver import card_mailbox as cm
        orig = (cm._is_removable, cm._volume_label, cm._read_raw, cm._write_raw)
        state = {"reqc": b"\x00\x00\x00\x00"}

        def fake_write(path, data):
            if path.endswith("REQC"):
                state["reqc"] = bytes(data[:4])

        def fake_read(path, n=cm.BUF_SIZE):
            if path.endswith("RSPC"):
                return state["reqc"]                      # 카드가 처리완료 신호(에코)
            if path.endswith("RSPM"):
                return bytes([6, 0x00, 0x18, 0x56, 0x12, 0x34, 0x5a]) + b"\x00" * 64
            return b"\x00" * n

        cm._is_removable = lambda root: True
        cm._volume_label = lambda root: "NIKON D40"       # 카메라 포맷 라벨
        cm._read_raw, cm._write_raw = fake_read, fake_write
        try:
            with tempfile.TemporaryDirectory() as td:
                self.assertFalse(cm.is_fresh_card(td))    # 라벨 판별로는 못 잡음(전제)
                self.assertTrue(cm.probe_bootstrap(td, poll_tries=2))
                self.assertTrue(os.path.isdir(os.path.join(td, cm.EYEFI_DIR)))  # 복구 유지
        finally:
            cm._is_removable, cm._volume_label, cm._read_raw, cm._write_raw = orig

    def test_probe_bootstrap_cleans_up_non_eyefi(self):
        """일반 USB(무응답): 탐침 실패 시 만든 EYEFI 폴더를 지워 원상복구하고 False."""
        from EyeFiReceiver import card_mailbox as cm
        orig = (cm._is_removable, cm._read_raw, cm._write_raw)
        cm._is_removable = lambda root: True
        cm._read_raw = lambda path, n=cm.BUF_SIZE: b"\xff" * n   # 영원히 무응답
        cm._write_raw = lambda path, data: None
        try:
            with tempfile.TemporaryDirectory() as td:
                self.assertFalse(cm.probe_bootstrap(td, poll_tries=1))
                self.assertFalse(os.path.exists(os.path.join(td, cm.EYEFI_DIR)))  # 청소됨
        finally:
            cm._is_removable, cm._read_raw, cm._write_raw = orig

    def test_read_card_info_empty_uploadkey_diagnostics(self):
        """업로드키 빈 응답(타 PC 12345b 실사례): 재시도 후에도 비면 펌웨어·raw hex
        진단정보를 함께 반환한다(경고 표시는 GUI 몫)."""
        from EyeFiReceiver import card_mailbox as cm

        class _Stub:
            mount = "X:\\"
            def get_mac(self):
                return "00-18-56-12-34-5b"
            def _info(self, sub):
                return b"\x00" + b"\xab" * 20      # pascal 길이 0 = 빈 업로드키
            def _pascal(self, resp):
                n = resp[0]
                return resp[1:1 + n]
            def get_firmware(self):
                return "5.0 TEST FW"

        orig_find, orig_cls = cm.find_card_mount, cm.CardMailbox
        cm.find_card_mount = lambda drive=None, probe_formatted=False: "X:\\"
        cm.CardMailbox = lambda mount: _Stub()
        try:
            data = cm.read_card_info()
            self.assertEqual(data["uploadkey"], "")
            self.assertEqual(data["firmware"], "5.0 TEST FW")
            self.assertTrue(data["uploadkey_raw"].startswith("00abab"))
            self.assertFalse(data["recovered"])
        finally:
            cm.find_card_mount, cm.CardMailbox = orig_find, orig_cls

    def test_firmware_exposes_upload_key(self):
        """업로드키 노출은 펌웨어 의존: 5.2010(우리 카드)만 노출, 5.0/4.5/3.0 은 빈 값.
        (eyefi-config 커뮤니티 확정 — 구펌웨어는 키가 있어도 토큰 0xFD 가 len 0)."""
        from EyeFiReceiver.card_mailbox import firmware_exposes_upload_key as f
        self.assertTrue(f("5.2010 Aug 27 2013 18:12:44"))
        self.assertTrue(f("5.2 something"))
        self.assertFalse(f("5.0 Jan 1 2011"))
        self.assertFalse(f("4.5 xx"))
        self.assertFalse(f("3.0"))
        self.assertFalse(f(""))
        self.assertFalse(f("garbage"))

    def test_collect_diagnostics_reports_old_firmware(self):
        """구펌웨어(5.0)+빈 업로드키 → 진단 로그가 원인·해결(펌웨어 5.2010 업데이트)을 명시."""
        from EyeFiReceiver import card_mailbox as cm
        orig = (cm._is_removable, cm._volume_label, cm._read_raw, cm._write_raw,
                cm.find_card_mount, cm.CardMailbox)

        class _Stub:
            def _info(self, tok):
                if tok == cm.FIRMWARE_INFO:
                    v = b"5.0 Jan 1 2011"
                    return bytes([len(v)]) + v
                if tok == cm.MAC_ADDRESS:
                    return bytes([6, 0x00, 0x18, 0x56, 0x12, 0x34, 0x5b])
                if tok == cm.UPLOAD_KEY:
                    return b"\x00"                    # 빈 업로드키(구펌웨어)
                return b"\x00"
            def _pascal(self, resp):
                n = resp[0] if resp else 0
                return resp[1:1 + n]
            def list_configured(self):
                return ["Eyefi"]

        cm.find_card_mount = lambda drive=None, probe_formatted=False: "X:\\"
        cm._volume_label = lambda root: "NIKON"
        cm.CardMailbox = lambda mount: _Stub()
        try:
            text = cm.collect_diagnostics(app_version="9.9.9")
            self.assertIn("00-18-56-12-34-5b", text)
            self.assertIn("5.0 Jan 1 2011", text)
            self.assertIn("5.2010", text)               # 참고 안내에 등장
            self.assertIn("복구", text)                  # 키 복구를 권장 해결책으로 안내
        finally:
            (cm._is_removable, cm._volume_label, cm._read_raw, cm._write_raw,
             cm.find_card_mount, cm.CardMailbox) = orig

    def test_read_firmware_verdict(self):
        """펌웨어 확인: 구펌웨어(5.0)는 exposes_key=False, 신펌웨어(5.2010)는 True."""
        from EyeFiReceiver import card_mailbox as cm
        orig = (cm.find_card_mount, cm.CardMailbox)

        class _Stub:
            def __init__(self, fw):
                self._fw = fw
            def get_mac(self):
                return "00-18-56-12-34-5a"
            def get_firmware(self):
                return self._fw

        cm.find_card_mount = lambda drive=None, probe_formatted=False: "F:\\"
        try:
            cm.CardMailbox = lambda mount: _Stub("5.0 Jan 1 2011")
            self.assertFalse(cm.read_firmware()["exposes_key"])
            cm.CardMailbox = lambda mount: _Stub("5.2010 Aug 27 2013 18:12:44")
            info = cm.read_firmware()
            self.assertTrue(info["exposes_key"])
            self.assertEqual(info["mac"], "00-18-56-12-34-5a")
        finally:
            cm.find_card_mount, cm.CardMailbox = orig

    def test_collect_diagnostics_no_card(self):
        """카드 없음: 예외 없이 '찾지 못함' 사실을 로그로 남긴다."""
        from EyeFiReceiver import card_mailbox as cm
        orig = cm.find_card_mount
        cm.find_card_mount = lambda drive=None, probe_formatted=False: None
        try:
            text = cm.collect_diagnostics()
            self.assertIn("찾지 못함", text)
        finally:
            cm.find_card_mount = orig

    def test_is_fresh_card_by_label(self):
        from EyeFiReceiver import card_mailbox as cm
        orig_rm, orig_lbl = cm._is_removable, cm._volume_label
        cm._is_removable = lambda root: True
        cm._volume_label = lambda root: "Eye-Fi"
        try:
            with tempfile.TemporaryDirectory() as td:
                self.assertTrue(cm.is_fresh_card(td))
                self.assertTrue(cm.is_eyefi_drive(td))
                # EYEFI 폴더가 생기면 더 이상 '공장 초기'가 아님(설정된 카드)
                os.makedirs(os.path.join(td, cm.EYEFI_DIR))
                self.assertFalse(cm.is_fresh_card(td))
                self.assertTrue(cm.is_eyefi_drive(td))
            cm._volume_label = lambda root: "MYSD"
            with tempfile.TemporaryDirectory() as td:
                self.assertFalse(cm.is_fresh_card(td))
                self.assertFalse(cm.is_eyefi_drive(td))
        finally:
            cm._is_removable, cm._volume_label = orig_rm, orig_lbl


if __name__ == "__main__":
    unittest.main(verbosity=2)
