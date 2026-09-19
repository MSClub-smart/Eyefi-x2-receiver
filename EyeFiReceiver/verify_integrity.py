"""
verify_integrity.py — INTEGRITYDIGEST 계산식 확정 도구 (실카드 캡처 대조)

병원 PC의 captures/integrity/ 에 쌓인 실카드 캡처(NN_*.bin + NN_*.txt 쌍)를 읽어,
카드가 보낸 integritydigest 와 여러 후보 계산식의 결과를 대조한다.
모든 완전(full=yes) 캡처와 일치하는 후보가 곧 확정 공식.

실행(캡처가 있는 PC에서):
    python -m EyeFiReceiver.verify_integrity            # 기본 캡처 폴더
    python -m EyeFiReceiver.verify_integrity <폴더경로>  # 폴더 직접 지정

확정되면: eyefi_protocol.integrity_digest 가 그 공식인지 확인하고
README 로드맵 체크 + (다음 단계) 서버측 검증 활성화를 진행한다.
"""
from __future__ import annotations
import binascii
import hashlib
import os
import struct
import sys

from . import paths
from .eyefi_protocol import _unhex, tcp_checksum, integrity_digest


# ------------------------------------------------------------------ 후보 공식들
def _checksum_stream(data: bytes, invert: bool, swap_pack: bool) -> bytearray:
    """블록별 16비트 1의 보수 합 스트림.
    (합산의 워드 endian 은 결과 바이트에 영향 없음 — 1의 보수 합은 바이트 스왑과
    가환이라 LE합+LE출력 == BE합+BE출력. 그래서 변형은 출력 스왑 여부만 둔다.)"""
    out = bytearray()
    for off in range(0, len(data), 512):
        block = data[off:off + 512]
        if len(block) % 2:
            block += b"\x00"
        total = 0
        for i in range(0, len(block), 2):
            total += block[i] | (block[i + 1] << 8)
        while total >> 16:
            total = (total & 0xFFFF) + (total >> 16)
        if invert:
            total = ~total & 0xFFFF
        out += struct.pack(">H" if swap_pack else "<H", total)
    return out


def _cand_tcpsum(data: bytes, key: str, invert: bool, swap_pack: bool) -> str:
    sums = _checksum_stream(data, invert, swap_pack)
    sums += _unhex(key)
    return hashlib.md5(bytes(sums)).hexdigest()


def _cand_key_in_data(data: bytes, key: str) -> str:
    """키 바이트를 데이터 끝에 붙인 뒤 전체를 512B 블록 체크섬 (변형 구현체 대비)."""
    return hashlib.md5(bytes(_checksum_stream(data + _unhex(key), True, False))).hexdigest()


CANDIDATES = {
    "tcpsum-inv (현 integrity_digest)": lambda d, k: integrity_digest(d, k),
    "tcpsum-noinv": lambda d, k: _cand_tcpsum(d, k, False, False),
    "tcpsum-inv-swapped": lambda d, k: _cand_tcpsum(d, k, True, True),
    "tcpsum-noinv-swapped": lambda d, k: _cand_tcpsum(d, k, False, True),
    "tcpsum-inv-keyindata": _cand_key_in_data,
    "md5(data+key)": lambda d, k: hashlib.md5(d + _unhex(k)).hexdigest(),
    "md5(key+data)": lambda d, k: hashlib.md5(_unhex(k) + d).hexdigest(),
    "md5(data)": lambda d, k: hashlib.md5(d).hexdigest(),
}


# ------------------------------------------------------------------ 캡처 로딩
def load_captures(capdir: str) -> list[dict]:
    caps = []
    for name in sorted(os.listdir(capdir)):
        if not name.endswith(".txt"):
            continue
        meta: dict = {"name": name[:-4]}
        with open(os.path.join(capdir, name), encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k:
                    meta[k] = v
        binpath = os.path.join(capdir, name[:-4] + ".bin")
        if not os.path.exists(binpath):
            continue
        with open(binpath, "rb") as f:
            meta["data"] = f.read()
        caps.append(meta)
    return caps


def main(argv: list[str]) -> int:
    # 한국어 Windows 콘솔(cp949)에서도 출력 깨지지 않게
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    capdir = argv[1] if len(argv) > 1 else paths.data_path("captures", "integrity")
    if not os.path.isdir(capdir):
        print(f"캡처 폴더 없음: {capdir}")
        print("(캡처는 수신기가 실카드 업로드 시 자동 저장 — 캡처가 있는 PC에서 실행할 것)")
        return 2

    caps = load_captures(capdir)
    if not caps:
        print(f"캡처 없음: {capdir}")
        return 2

    print(f"캡처 {len(caps)}개 로드: {capdir}\n")
    # 후보별 전적: full=yes 캡처만 판정에 사용(부분 수신분은 참고 표시)
    full_total = 0
    score = {c: 0 for c in CANDIDATES}
    for cap in caps:
        digest = (cap.get("integritydigest") or "").strip().lower()
        key = cap.get("uploadkey", "")
        data = cap["data"]
        is_full = cap.get("full") == "yes"
        tag = "완전" if is_full else "부분"
        print(f"[{cap['name']}] {tag} {len(data)}B digest={digest or '(없음)'}")
        if not digest or not key:
            print("   → 메타 부족, 건너뜀")
            continue
        if is_full:
            full_total += 1
        matched = False
        for cname, fn in CANDIDATES.items():
            try:
                got = fn(data, key)
            except Exception as e:
                got = f"오류:{e}"
            if got == digest:
                matched = True
                print(f"   ✔ 일치: {cname}")
                if is_full:
                    score[cname] += 1
        if not matched:
            print("   ✘ 일치 후보 없음")
    print()

    if full_total == 0:
        print("완전(full=yes) 캡처가 없어 확정 불가 — 캡처가 더 쌓인 뒤 재실행.")
        return 2
    winners = [c for c, s in score.items() if s == full_total]
    print(f"완전 캡처 {full_total}개 전부 일치한 후보: {winners or '없음'}")
    if len(winners) == 1:
        print(f"\n★ 확정: {winners[0]}")
        if "(현 integrity_digest)" in winners[0]:
            print("→ eyefi_protocol.integrity_digest 그대로 사용 가능. README 로드맵 체크할 것.")
        else:
            print("→ eyefi_protocol.integrity_digest 를 이 공식으로 교체할 것.")
        return 0
    if len(winners) > 1:
        print("→ 복수 후보 일치(데이터가 우연히 대칭적일 수 있음) — 캡처 추가 후 재실행 권장.")
        return 1
    print("→ 일치 후보 없음: 카드가 다이제스트를 chunk 가 아닌 전체 파일 기준으로 보냈거나"
          " 미지의 공식. 부분 캡처 여부와 filesize/chunklen 을 확인할 것.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
