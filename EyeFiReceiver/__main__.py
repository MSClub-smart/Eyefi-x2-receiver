"""
실행 진입점.

  GUI:      python -m EyeFiReceiver
  콘솔전용: python -m EyeFiReceiver --headless
  포트지정: python -m EyeFiReceiver --headless --port 59279

콘솔 모드는 GUI 없이 서버만 띄운다(원격/서비스용, 실카드 테스트용).
"""
from __future__ import annotations
import argparse
import os
import sys
import time

from .config import Config
from .eyefi_server import EyeFiServer
from . import paths


def _default_config_path() -> str:
    return paths.data_path("cards.json")


def run_headless(config_path: str, port: int | None):
    cfg = Config.load(config_path)
    if port:
        cfg.port = port
    if not cfg.default_folder and not any(c.get("folder") for c in cfg.cards.values()):
        print("경고: 저장폴더 미설정. cards.json 의 default_folder 를 채우세요.")

    def on_event(kind, info):
        ts = time.strftime("%H:%M:%S")
        if kind == "received":
            gap = info.get("gap_sec")
            g = "첫 장" if gap is None else f"간격 {gap}s"
            print(f"{ts} [받음] {os.path.basename(info['path'])} ({info['size']//1024}KB, {g}) {info['mac']}")
        elif kind == "session":
            print(f"{ts} [세션] {info['mac']} {'등록' if info['known'] else '미등록!'}")
        elif kind == "unknown_card":
            print(f"{ts} [경고] 미등록 카드 {info['mac']}")
        elif kind == "photo_status":
            ok = "OK" if info.get("cred_ok") else "불일치(비강제)"
            print(f"{ts} [인증] {info['mac']} cred={ok} file={info.get('filename','')} "
                  f"filesize={info.get('filesize')} offset={info.get('offset')}")
        elif kind == "upload_start":
            print(f"{ts} [업로드시작] Content-Length={info.get('total')}")
        elif kind == "upload_progress":
            print(f"{ts} [진행] {info.get('got')}/{info.get('total')} bytes")
        elif kind == "partial":
            print(f"{ts} [부분수신-끊김] file={info.get('file')} got={info.get('got')}/{info.get('need')} bytes")
        elif kind == "upload_empty":
            print(f"{ts} [빈업로드] {info.get('diag')}")
        elif kind == "mark_last":
            print(f"{ts} [마크종료] {info.get('mac')}")
        elif kind == "error":
            print(f"{ts} [오류] {info.get('msg')}")
        else:
            print(f"{ts} [{kind}] {info}")

    try:
        server = EyeFiServer(cfg, on_event=on_event)
    except OSError as e:
        print(f"포트 {cfg.port} 사용 불가(공식 수신앱 종료 필요?): {e}")
        sys.exit(1)
    print(f"Eye-Fi 수신기 시작 — 0.0.0.0:{cfg.port}, 카드 {len(cfg.cards)}개. Ctrl+C 로 종료.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료 중…")
        server.shutdown()
        server.server_close()


def main():
    ap = argparse.ArgumentParser(description="Eye-Fi X2 로컬 수신기")
    ap.add_argument("--headless", action="store_true", help="GUI 없이 콘솔 서버")
    ap.add_argument("--tray", action="store_true",
                    help="트레이 상주(창 숨김) + 서버 자동시작 — 부팅 자동시작용")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--config", default=_default_config_path())
    args = ap.parse_args()

    if args.headless:
        run_headless(args.config, args.port)
    else:
        from .gui import ReceiverGUI
        ReceiverGUI(
            args.config,
            with_tray=args.tray,
            start_hidden=args.tray,
            autostart_server=args.tray,
        ).run()


if __name__ == "__main__":
    main()
