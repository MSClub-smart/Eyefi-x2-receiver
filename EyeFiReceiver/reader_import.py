"""
reader_import.py — 리더에 꽂힌 Eye-Fi 카드에서 사진을 '직접 복사'로 가져오기 (공식앱의
카드리더 가져오기 대응). Wi-Fi 수신과 독립 — 카드는 일반 SD 라 DCIM 폴더를 그냥 읽으면 된다.

중복 방지: 파일명+크기가 대상 폴더(기본 폴더 + 최근 날짜 하위폴더)에 이미 있으면 건너뜀.
→ 리더로 먼저 가져온 뒤 카드가 카메라로 돌아가 Wi-Fi 재업로드해도(카드엔 '미전송'으로
  남아 있으므로) 서버 쪽 같은 검사로 중복 저장이 안 된다.
"""
from __future__ import annotations
import datetime
import json
import os
import shutil
import threading
import time

from . import paths

MEDIA_EXTS = {".jpg", ".jpeg", ".nef", ".raw", ".cr2", ".avi", ".mov", ".mp4"}

# ---- 수신 장부(ledger): "이름:크기" 로 이미 받은 사진 기록 ----
# 직원이 저장폴더에서 사진을 옮겨가도(폴더가 비어도) 리더 재삽입 시 재복사하지 않기 위함.
# Wi-Fi 수신(save_media)과 리더 가져오기 양쪽이 기록한다.
LEDGER_PATH = paths.data_path("received_ledger.json")
LEDGER_KEEP_DAYS = 90
_ledger_lock = threading.Lock()


def _ledger_load() -> dict:
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def ledger_has(filename: str, size: int) -> bool:
    return f"{filename}:{size}" in _ledger_load()


def ledger_record(filename: str, size: int) -> None:
    """받은 사진을 장부에 기록(+오래된 항목 정리). 실패해도 조용히 무시."""
    try:
        with _ledger_lock:
            d = _ledger_load()
            now = time.time()
            d[f"{filename}:{size}"] = int(now)
            cutoff = now - LEDGER_KEEP_DAYS * 86400
            d = {k: v for k, v in d.items() if v >= cutoff}
            tmp = LEDGER_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, LEDGER_PATH)
    except Exception:
        pass


def find_eyefi_drives() -> list[str]:
    """EYEFI 마커 + DCIM 폴더가 있는 이동식 드라이브 루트('F:\\' 형태) 나열."""
    import string
    out = []
    for letter in string.ascii_uppercase:
        root = letter + ":\\"
        try:
            if os.path.isdir(os.path.join(root, "EYEFI")) and os.path.isdir(os.path.join(root, "DCIM")):
                out.append(root)
        except OSError:
            continue
    return out


def iter_card_photos(drive: str) -> list[str]:
    """카드 DCIM 아래의 미디어 파일 전체 경로(정렬)."""
    photos = []
    dcim = os.path.join(drive, "DCIM")
    for base, _dirs, files in os.walk(dcim):
        for f in files:
            if os.path.splitext(f)[1].lower() in MEDIA_EXTS:
                photos.append(os.path.join(base, f))
    photos.sort(key=lambda p: os.path.basename(p))
    return photos


def _recent_date_dirs(root: str, days: int) -> list[str]:
    """root + 최근 days 일의 날짜 하위폴더(존재하는 것만). 중복검사 대상."""
    dirs = [root]
    today = datetime.date.today()
    for i in range(days + 1):
        d = os.path.join(root, (today - datetime.timedelta(days=i)).isoformat())
        if os.path.isdir(d):
            dirs.append(d)
    return dirs


def find_duplicate(root: str, filename: str, size: int, days: int = 30) -> str | None:
    """대상 루트/최근 날짜폴더에서 같은 이름+크기의 파일 검색. 있으면 그 경로."""
    for d in _recent_date_dirs(root, days):
        p = os.path.join(d, filename)
        try:
            if os.path.getsize(p) == size:
                return p
        except OSError:
            continue
    return None


def import_photos(drive: str, dest_root: str, date_subfolders: bool = True,
                  dedup_days: int = 30, progress_cb=None) -> dict:
    """카드→폴더 복사. 반환 {copied:[dest..], skipped:int, errors:[(src,msg)..]}.
    저장 날짜폴더는 '파일 수정시각'(촬영일) 기준 — Wi-Fi 수신(당일 폴더)과 대부분 일치."""
    copied, errors = [], []
    skipped = 0
    for src in iter_card_photos(drive):
        name = os.path.basename(src)
        try:
            size = os.path.getsize(src)
            # 1) 장부에 있으면(과거에 이미 받음 — 옮겨졌어도) 스킵, 2) 폴더에 있으면 스킵
            if ledger_has(name, size) or find_duplicate(dest_root, name, size, days=dedup_days):
                skipped += 1
                continue
            folder = dest_root
            if date_subfolders:
                day = datetime.date.fromtimestamp(os.path.getmtime(src)).isoformat()
                folder = os.path.join(dest_root, day)
            os.makedirs(folder, exist_ok=True)
            dest = os.path.join(folder, name)
            base, ext = os.path.splitext(dest)
            n = 1
            while os.path.exists(dest):   # 같은 이름·다른 크기 → 별도 저장
                dest = f"{base}_r{n}{ext}"
                n += 1
            shutil.copy2(src, dest)
            ledger_record(name, size)
            copied.append(dest)
            if progress_cb:
                try:
                    progress_cb(dest, len(copied))
                except Exception:
                    pass
        except OSError as e:
            errors.append((src, str(e)))
    return {"copied": copied, "skipped": skipped, "errors": errors}
