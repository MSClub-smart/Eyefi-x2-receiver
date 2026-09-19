"""
gui.py — Eye-Fi 로컬 수신기 설정/모니터 창 (Tkinter, 표준 라이브러리)

기능:
  - 서버 시작/중지 (포트·기본 저장폴더 설정)
  - 카드 목록: 추가/수정/삭제 (MAC·UploadKey·이름·카드별 폴더)
  - 실시간 전송 로그 + 도착 간격(딜레이) 표시
설정은 cards.json 에 저장(레지스트리·공식앱 불필요).
"""
from __future__ import annotations
import os
import queue
import time
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .config import Config, DEFAULT_PORT, normalize_mac
from .eyefi_server import EyeFiServer
from . import autostart
from . import reader_import
from . import paths


class ReceiverGUI:
    def __init__(self, config_path: str, with_tray: bool = False,
                 start_hidden: bool = False, autostart_server: bool = False):
        self.config = Config.load(config_path)
        self.server: EyeFiServer | None = None
        self.events: "queue.Queue" = queue.Queue()
        self.tray = None
        self._quitting = False

        self.thumbs: list = []  # 최근 썸네일 셀들
        self._toasts: list = []  # 활성 팝업들
        self._prog = None        # 현재 진행중 팝업(수신 중 → 완료)
        self._logpath = paths.data_path("receiver.log")

        self.root = tk.Tk()
        from . import __version__
        self.root.title(f"Eye-Fi 로컬 수신기 v{__version__}")
        self.root.geometry("780x720")
        self._build()
        self._refresh_cards()
        self.root.after(200, self._drain_events)
        self.root.after(60000, self._watch_server)   # 서버 자가복구 워치독
        self._known_reader_drives: set = set(reader_import.find_eyefi_drives())
        self._importing = False
        self.root.after(10000, self._poll_reader)    # 리더 자동 가져오기

        if autostart_server:
            self._start_server(silent=True)
        if with_tray:
            self._setup_tray()
            self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        if start_hidden:
            self.root.withdraw()

    # ---------------------------------------------------------------- UI 구성
    def _build(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="포트").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar(value=str(self.config.port or DEFAULT_PORT))
        ttk.Entry(top, textvariable=self.port_var, width=8).grid(row=0, column=1, sticky="w")

        ttk.Label(top, text="기본 저장폴더").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.folder_var = tk.StringVar(value=self.config.default_folder)
        ttk.Entry(top, textvariable=self.folder_var, width=40).grid(row=0, column=3, sticky="we")
        ttk.Button(top, text="찾기…", command=self._pick_default_folder).grid(row=0, column=4, padx=4)

        self.date_var = tk.BooleanVar(value=self.config.date_subfolders)
        ttk.Checkbutton(top, text="날짜별 하위폴더", variable=self.date_var).grid(row=1, column=3, sticky="w", pady=4)

        self.autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        self.autostart_chk = ttk.Checkbutton(top, text="부팅 시 자동시작", variable=self.autostart_var,
                                             command=self._toggle_autostart)
        self.autostart_chk.grid(row=1, column=2, sticky="w", pady=4)
        if not autostart.is_supported():
            self.autostart_chk.state(["disabled"])

        self.toast_var = tk.BooleanVar(value=self.config.show_toast)
        ttk.Checkbutton(top, text="전송 팝업", variable=self.toast_var,
                        command=self._toggle_toast).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)

        self.start_btn = ttk.Button(top, text="▶ 서버 시작", command=self._toggle_server)
        self.start_btn.grid(row=0, column=5, rowspan=2, padx=8)
        self.status_var = tk.StringVar(value="중지됨")
        ttk.Label(top, textvariable=self.status_var, foreground="#888").grid(row=1, column=5)
        top.columnconfigure(3, weight=1)

        # 카드 목록
        mid = ttk.LabelFrame(self.root, text="등록 카드", padding=8)
        mid.pack(fill="both", expand=False, padx=8, pady=4)
        # 버튼 툴바(목록 위) — 항상 보이도록
        bar = ttk.Frame(mid)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="🔑 카드를 앱에 등록", command=self._read_from_reader).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="📶 카드 Wi-Fi", command=self._card_wifi_dialog).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="💾 리더에서 가져오기", command=self._manual_reader_import).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="🔎 펌웨어 확인", command=self._check_firmware).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="🩺 진단 로그", command=self._save_diagnostics).pack(side="left", padx=(0, 6))
        self.reader_auto_var = tk.BooleanVar(value=self.config.reader_autoimport)
        ttk.Checkbutton(bar, text="리더 자동", variable=self.reader_auto_var,
                        command=self._toggle_reader_auto).pack(side="left")
        ttk.Button(bar, text="수정", command=self._edit_card).pack(side="left", padx=3)
        ttk.Button(bar, text="삭제", command=self._del_card).pack(side="left", padx=3)
        cols = ("mac", "name", "uploadkey", "folder")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=6)
        for c, w in zip(cols, (130, 140, 240, 110)):
            self.tree.heading(c, text=c.upper())
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)

        # 미리보기 (하단 고정 썸네일 스트립) — 공식앱처럼 화면 하단에서 전송 확인
        prev = ttk.LabelFrame(self.root, text="실시간 미리보기 (최근 도착 사진)", padding=6)
        prev.pack(side="bottom", fill="x", padx=8, pady=(4, 8))
        self.thumb_row = ttk.Frame(prev, height=125)
        self.thumb_row.pack(fill="x")
        self.thumb_hint = ttk.Label(self.thumb_row, text="아직 받은 사진이 없습니다. 촬영하면 여기 썸네일이 나타납니다.",
                                    foreground="#888")
        self.thumb_hint.pack(pady=40)

        # 로그 (미리보기 위)
        low = ttk.LabelFrame(self.root, text="전송 로그 (도착 간격 = 딜레이)", padding=8)
        low.pack(fill="both", expand=True, padx=8, pady=4)
        self.log = tk.Text(low, height=7, state="disabled", wrap="none")
        self.log.pack(fill="both", expand=True)

    # ---------------------------------------------------------------- 동작
    def _pick_default_folder(self):
        d = filedialog.askdirectory(title="기본 저장폴더 선택")
        if d:
            self.folder_var.set(d)

    def _apply_top_to_config(self):
        try:
            self.config.port = int(self.port_var.get())
        except ValueError:
            self.config.port = DEFAULT_PORT
        self.config.default_folder = self.folder_var.get().strip()
        self.config.date_subfolders = bool(self.date_var.get())

    def _toggle_server(self):
        if self.server:
            self._stop_server()
        else:
            self._start_server(silent=False)

    def _start_server(self, silent: bool = False):
        if self.server:
            return
        self._apply_top_to_config()
        self.config.save()
        if not self.config.default_folder and not any(c.get("folder") for c in self.config.cards.values()):
            if not silent:
                messagebox.showwarning("폴더 없음", "기본 저장폴더를 지정하세요.")
            self._append("서버 시작 취소 — 저장폴더 미설정")
            return
        import threading
        self.server = None
        last_err = None
        for attempt in range(3):   # 재시작 직후 이전 소켓 정리 대기 (최대 ~1.6초)
            try:
                self.server = EyeFiServer(self.config, on_event=lambda k, i: self.events.put((k, i)))
                break
            except OSError as e:
                last_err = e
                time.sleep(0.8)
        if self.server is None:
            msg = f"포트 {self.config.port} 사용 불가 — 공식 EyeFiX2Receiver 가 켜져 있으면 종료하세요."
            self._append(f"[시작 실패] {msg}")
            if not silent:
                messagebox.showerror("시작 실패", f"{msg}\n\n{last_err}")
            return
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.start_btn.config(text="■ 서버 중지")
        self.status_var.set(f"수신 대기 :{self.config.port}")
        from . import __version__
        self._append(f"서버 시작 — 0.0.0.0:{self.config.port}, 카드 {len(self.config.cards)}개 (v{__version__})")
        self._update_tray()

    def _stop_server(self):
        if not self.server:
            return
        self.server.shutdown()
        self.server.server_close()
        self.server = None
        self.start_btn.config(text="▶ 서버 시작")
        self.status_var.set("중지됨")
        self._append("서버 중지.")
        self._update_tray()

    # ---- 자동시작 ----
    def _toggle_toast(self):
        self.config.show_toast = bool(self.toast_var.get())
        self.config.save()
        self._append("전송 팝업 " + ("켜짐" if self.config.show_toast else "꺼짐"))

    def _toggle_autostart(self):
        try:
            if self.autostart_var.get():
                autostart.enable()
                self._append("부팅 자동시작 등록됨.")
                if autostart.official_autostart_enabled():
                    self._append("주의: 공식 앱 자동시작도 켜져 있음 → 둘 다 부팅되면 포트 충돌. 하나만 두세요.")
            else:
                autostart.disable()
                self._append("부팅 자동시작 해제됨.")
        except Exception as e:
            messagebox.showerror("자동시작 오류", str(e))
            self.autostart_var.set(autostart.is_enabled())

    # ---- 트레이 ----
    def _setup_tray(self):
        try:
            import pystray
            from PIL import Image
        except Exception as e:
            self._append(f"[트레이 비활성] pystray/Pillow 없음: {e}")
            return
        img = self._tray_image(Image)
        self.tray = pystray.Icon("EyeFiReceiver", img, "Eye-Fi 수신기", menu=self._tray_menu(pystray))
        import threading
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _tray_image(self, Image):
        ico = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Autorun", "EyeFi.ico")
        try:
            return Image.open(ico)
        except Exception:
            from PIL import ImageDraw
            im = Image.new("RGB", (64, 64), "#2b6cb0")
            d = ImageDraw.Draw(im)
            d.ellipse((14, 14, 50, 50), fill="white")
            return im

    def _tray_menu(self, pystray):
        return pystray.Menu(
            pystray.MenuItem("설정 열기", lambda: self.root.after(0, self._show_window), default=True),
            pystray.MenuItem(lambda item: "■ 서버 중지" if self.server else "▶ 서버 시작",
                             lambda: self.root.after(0, self._toggle_server)),
            pystray.MenuItem("부팅 시 자동시작",
                             lambda: self.root.after(0, self._tray_toggle_autostart),
                             checked=lambda item: autostart.is_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", lambda: self.root.after(0, self._quit_app)),
        )

    def _tray_toggle_autostart(self):
        self.autostart_var.set(not autostart.is_enabled())
        self._toggle_autostart()
        self._update_tray()

    def _update_tray(self):
        if self.tray:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _hide_to_tray(self):
        if self.tray:
            self.root.withdraw()
            self._append("트레이로 숨김 — 트레이 아이콘 더블클릭으로 다시 엽니다.")
        else:
            self._quit_app()

    def _quit_app(self):
        self._quitting = True
        self._stop_server()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    # ---- 카드 편집 ----
    def _refresh_cards(self):
        self.tree.delete(*self.tree.get_children())
        for mac, info in self.config.cards.items():
            self.tree.insert("", "end", iid=mac,
                             values=(mac, info.get("name", ""), info.get("uploadkey", ""),
                                     info.get("folder", "(기본)")))

    def _card_dialog(self, mac="", name="", uploadkey="", folder="", firmware="", exposes_key=None):
        dlg = tk.Toplevel(self.root)
        dlg.title("카드 정보")
        dlg.transient(self.root)
        dlg.grab_set()
        rows = [("MAC (00-18-56-..)", mac), ("이름", name), ("UploadKey (32 hex)", uploadkey)]
        ent = {}
        for i, (lab, v) in enumerate(rows):
            ttk.Label(dlg, text=lab).grid(row=i, column=0, sticky="w", padx=8, pady=4)
            var = tk.StringVar(value=v)
            ttk.Entry(dlg, textvariable=var, width=44).grid(row=i, column=1, padx=8, pady=4)
            ent[lab] = var
        # 펌웨어(읽기전용) — 카드를 실제로 읽어 넘어온 경우에만 표시
        r = 3
        if firmware:
            ttk.Label(dlg, text="펌웨어").grid(row=r, column=0, sticky="w", padx=8, pady=4)
            verdict = "" if exposes_key is None else (
                "  (업로드키 읽기 가능)" if exposes_key else "  (구펌웨어 — 업로드키 숨김)")
            fw_lbl = ttk.Label(dlg, text=firmware + verdict,
                               foreground=("#0a7d00" if exposes_key else "#b00000")
                               if exposes_key is not None else None)
            fw_lbl.grid(row=r, column=1, sticky="w", padx=8, pady=4)
            r += 1
        ttk.Label(dlg, text="저장폴더(비우면 기본)").grid(row=r, column=0, sticky="w", padx=8, pady=4)
        fvar = tk.StringVar(value=folder)
        ttk.Entry(dlg, textvariable=fvar, width=44).grid(row=r, column=1, padx=8, pady=4)
        ttk.Button(dlg, text="찾기…", command=lambda: fvar.set(filedialog.askdirectory() or fvar.get())).grid(row=r, column=2)
        result = {}

        def ok():
            m = normalize_mac(ent["MAC (00-18-56-..)"].get())
            uk = ent["UploadKey (32 hex)"].get().strip().lower()
            if len(m.replace("-", "")) != 12 or len(uk) != 32:
                messagebox.showerror("입력 오류", "MAC 12자리, UploadKey 32자리 hex 를 확인하세요.")
                return
            result.update(mac=m, name=ent["이름"].get().strip(), uploadkey=uk, folder=fvar.get().strip())
            dlg.destroy()

        ttk.Button(dlg, text="확인", command=ok).grid(row=r + 1, column=1, sticky="e", pady=8)
        ttk.Button(dlg, text="취소", command=dlg.destroy).grid(row=r + 1, column=2, pady=8)
        self.root.wait_window(dlg)
        return result

    def _read_from_reader(self):
        # 공식 앱이 켜져 있으면 카드 접근이 막힘 → 안내
        import subprocess
        try:
            running = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq EyeFiX2Receiver.exe"],
                capture_output=True, text=True, timeout=8,
            ).stdout
        except Exception:
            running = ""
        if "EyeFiX2Receiver.exe" in running:
            if not messagebox.askyesno(
                "공식 앱 실행 중",
                "공식 EyeFiX2Receiver 가 켜져 있으면 카드 판독이 실패할 수 있습니다.\n"
                "그래도 시도할까요? (권장: 공식 앱 종료 후 진행)"):
                return
        self._append("카드를 앱에 등록 중… 리더에서 카드 정보 읽는 중 (수 초)")
        self.root.config(cursor="watch")

        def worker():
            from .card_reader import read_card, CardReadError
            try:
                # probe_formatted: 카메라 포맷으로 EYEFI 폴더·라벨이 지워진 카드도
                # 메일박스 재생성으로 복구 시도 (수동 버튼 조작에서만 허용)
                data = read_card(probe_formatted=True)
                self.root.after(0, lambda: self._reader_done(data, None))
            except CardReadError as e:
                msg = str(e)  # except 블록 밖에서 e 가 삭제되므로 미리 담아둠
                self.root.after(0, lambda: self._reader_done(None, msg))
            except Exception as e:
                msg = f"예상치 못한 오류: {e}"
                self.root.after(0, lambda: self._reader_done(None, msg))

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _reader_done(self, data, err):
        self.root.config(cursor="")
        if err:
            self._append(f"[읽기 실패] {err}")
            messagebox.showerror("카드 읽기 실패", err)
            return
        if data.get("recovered"):
            self._append(f"[복구] 카메라 포맷으로 지워진 EYEFI 메일박스를 재생성했습니다 ({data.get('drive','')})")
        self._append(f"[읽음] {data['mac']} ({data.get('ssid','')}) — 등록 창을 확인하세요")
        fw = data.get("firmware", "")
        key_was_empty = not data.get("uploadkey")
        if key_was_empty:
            fwx = fw or "?"
            self._append(f"[경고] 카드에서 업로드키를 읽지 못했습니다(빈 응답) — "
                         f"FW={fwx} raw={data.get('uploadkey_raw','')[:40]}")
            if data.get("key_hidden_by_fw"):
                self._append("[안내] 카드 펌웨어가 5.2 미만이라 업로드키가 숨겨집니다"
                             " — Eye-Fi X2 Utility 로 5.2010 업데이트 후 재시도하세요")
                messagebox.showwarning(
                    "업로드키 없음 — 카드 펌웨어 구버전",
                    f"카드 펌웨어가 '{fwx}' 입니다.\n\n"
                    "Eye-Fi X2 카드는 펌웨어 5.0 이하에서는 업로드키가 카드에 있어도\n"
                    "읽히지 않습니다(빈 값). 펌웨어 5.2010 이상에서만 읽을 수 있습니다.\n\n"
                    "해결: 공식 'Eye-Fi X2 Utility' 로 이 카드의 펌웨어를 업데이트한 뒤\n"
                    "다시 '카드를 앱에 등록' 을 눌러 주세요.\n\n"
                    "잠시 후 진단 파일을 자동으로 만들어 폴더를 열어드립니다.")
            else:
                messagebox.showwarning(
                    "업로드키 없음",
                    "카드가 업로드키에 빈 값을 돌려줬습니다.\n"
                    "잠시 후 진단 파일을 자동으로 만들어 폴더를 열어드립니다.\n"
                    "그 파일을 개발자에게 보내주세요.")
        exposes = None
        if fw and fw != "?":
            from .card_mailbox import firmware_exposes_upload_key
            exposes = firmware_exposes_upload_key(fw)
            self._append(f"[펌웨어] {data['mac']} = '{fw}' — 업로드키 {'읽기 가능' if exposes else '숨김(구펌웨어)'}")
        r = self._card_dialog(mac=data["mac"], name=data.get("ssid", ""),
                              uploadkey=data.get("uploadkey", ""),
                              firmware=fw if fw != "?" else "", exposes_key=exposes)
        if r:
            self.config.add_or_update_card(r["mac"], r["uploadkey"], r["name"], r["folder"])
            self.config.save()
            self._refresh_cards()
            self._append(f"[등록] {r['mac']} 저장 완료")
        elif key_was_empty:
            # 등록 실패(키 없음) → 진단 파일을 자동으로 뽑아 폴더 열고 개발자 공유 안내
            self._save_diagnostics(auto=True)

    def _check_firmware(self):
        """리더의 카드 펌웨어 버전을 읽어 '키 읽기 가능 여부'와 함께 보여준다.
        업로드키가 안 읽히는 카드의 원인(구펌웨어)을 빠르게 확인하는 용도."""
        self._append("카드 펌웨어 확인 중… (수 초)")
        self.root.config(cursor="watch")

        def worker():
            from .card_reader import read_firmware, CardReadError
            try:
                info = read_firmware()
                self.root.after(0, lambda: self._firmware_done(info, None))
            except CardReadError as e:
                msg = str(e)
                self.root.after(0, lambda: self._firmware_done(None, msg))
            except Exception as e:
                msg = f"예상치 못한 오류: {e}"
                self.root.after(0, lambda: self._firmware_done(None, msg))

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _firmware_done(self, info, err):
        self.root.config(cursor="")
        if err:
            self._append(f"[펌웨어] 확인 실패: {err}")
            messagebox.showerror("펌웨어 확인 실패", err)
            return
        fw = info.get("firmware", "?")
        mac = info.get("mac", "?")
        exposes = info.get("exposes_key")
        self._append(f"[펌웨어] {mac} = '{fw}' — 업로드키 {'읽기 가능' if exposes else '숨김(구펌웨어)'}")
        if exposes:
            messagebox.showinfo(
                "펌웨어 확인",
                f"카드: {mac}\n펌웨어: {fw}\n\n"
                "✅ 이 펌웨어(5.2 이상)는 업로드키를 읽을 수 있습니다.\n"
                "'🔑 카드를 앱에 등록' 을 누르면 UploadKey 가 자동으로 채워집니다.")
        else:
            messagebox.showwarning(
                "펌웨어 확인 — 구버전",
                f"카드: {mac}\n펌웨어: {fw}\n\n"
                "⚠️ 이 카드는 펌웨어 5.2 미만이라 업로드키가 숨겨집니다.\n"
                "(카드에 키가 있어도 프로그램이 읽어올 수 없습니다.)\n\n"
                "해결: 공식 'Eye-Fi X2 Utility' 로 펌웨어를 5.2010 으로\n"
                "업데이트한 뒤 다시 시도하거나, 업로드키를 직접 입력하세요.\n\n"
                "자세한 원인 분석은 '🩺 진단 로그' 로 파일을 저장해 보내주세요.")

    def _diagnostics_dir(self) -> str:
        """진단 파일 저장 폴더: 찾기 쉬운 바탕화면 우선(없으면 홈)."""
        for cand in (os.path.join(os.path.expanduser("~"), "Desktop"),
                     os.path.join(os.path.expanduser("~"), "바탕 화면")):
            if os.path.isdir(cand):
                return cand
        return os.path.expanduser("~")

    def _save_diagnostics(self, auto=False):
        """카드 진단 정보를 모아 바탕화면에 자동 저장하고, 폴더를 열어 파일을 보여준 뒤
        '개발자에게 보내세요' 안내를 띄운다. 컴맹도 파일선택 없이 한 번에.
        auto=True 는 등록 실패(빈 업로드키) 시 자동 호출된 경우."""
        self._append("카드 진단 정보 수집 중… (수 초)")
        self.root.config(cursor="watch")

        def worker():
            import datetime
            from . import __version__
            from .card_reader import collect_diagnostics
            try:
                text = collect_diagnostics(app_version=__version__)
            except Exception as e:
                text = f"[진단 수집 실패] {e!r}"
            fname = "EyeFi-진단-" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".txt"
            path = os.path.join(self._diagnostics_dir(), fname)
            self.root.after(0, lambda: self._diagnostics_done(text, path, auto))

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _diagnostics_done(self, text, path, auto=False):
        self.root.config(cursor="")
        try:
            # utf-8-sig(BOM): 윈도우 메모장이 한글을 깨짐 없이 표시하도록
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(text)
        except OSError as e:
            self._append(f"[진단] 저장 실패: {e}")
            messagebox.showerror("진단 저장 실패", str(e))
            return
        self._append(f"[진단] 저장 완료: {path}")
        self._reveal_in_explorer(path)   # 탐색기에서 파일 선택 상태로 폴더 열기
        messagebox.showinfo(
            "진단 파일이 준비됐어요 — 개발자에게 보내주세요",
            "카드 진단 파일을 저장하고, 그 폴더를 열었습니다.\n\n"
            f"파일: {os.path.basename(path)}\n"
            f"위치: {os.path.dirname(path)}\n\n"
            "이 파일을 개발자에게 보내주세요:\n"
            "  • 카카오톡 ID: msclub77\n"
            "  • 이메일: msclub@naver.com\n\n"
            "방법: 방금 열린 폴더에서 이 파일을\n"
            "카카오톡 대화창으로 끌어다 놓으면 전송됩니다.")

    def _reveal_in_explorer(self, path):
        """탐색기에서 해당 파일이 선택된 채로 폴더를 연다(컴맹이 바로 찾도록)."""
        import subprocess
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        except Exception:
            try:
                os.startfile(os.path.dirname(path))   # 폴백: 폴더만 열기
            except Exception:
                pass

    # ---- 카드 Wi-Fi 설정 (클라우드 불필요) ----
    def _official_app_running(self) -> bool:
        import subprocess
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq EyeFiX2Receiver.exe"],
                capture_output=True, text=True, timeout=8,
            ).stdout
        except Exception:
            out = ""
        return "EyeFiX2Receiver.exe" in out

    def _card_wifi_dialog(self):
        if self._official_app_running():
            if not messagebox.askyesno(
                "공식 앱 실행 중",
                "공식 EyeFiX2Receiver 가 켜져 있으면 카드 접근이 실패할 수 있습니다.\n"
                "그래도 열까요? (권장: 공식 앱 종료 후 진행)"):
                return
        dlg = tk.Toplevel(self.root)
        dlg.title("카드 Wi-Fi 설정 — 클라우드 불필요")
        dlg.transient(self.root)
        dlg.geometry("600x560")

        st = tk.StringVar(value="카드를 리더에 꽂고 [카드 읽기]를 누르세요 (공식앱은 종료 상태여야 함)")
        ttk.Label(dlg, textvariable=st, foreground="#555", wraplength=570).pack(anchor="w", padx=10, pady=(10, 4))

        lists = ttk.Frame(dlg)
        lists.pack(fill="both", expand=True, padx=10)
        lf = ttk.LabelFrame(lists, text="카드에 저장된 망", padding=6)
        lf.pack(side="left", fill="both", expand=True, padx=(0, 5))
        conf_lb = tk.Listbox(lf, height=8, exportselection=False)
        conf_lb.pack(fill="both", expand=True)
        rf = ttk.LabelFrame(lists, text="카드가 지금 보는 망 (스캔)", padding=6)
        rf.pack(side="left", fill="both", expand=True, padx=(5, 0))
        scan_tv = ttk.Treeview(rf, columns=("ssid", "rssi", "flags"), show="headings", height=8)
        for c, w in (("ssid", 140), ("rssi", 45), ("flags", 45)):
            scan_tv.heading(c, text=c.upper())
            scan_tv.column(c, width=w, anchor="w")
        scan_tv.pack(fill="both", expand=True)

        form = ttk.Frame(dlg)
        form.pack(fill="x", padx=10, pady=6)
        ttk.Label(form, text="SSID").grid(row=0, column=0, sticky="w")
        ssid_var = tk.StringVar(value=self.config.wifi_ssid or "Eyefi")
        ttk.Entry(form, textvariable=ssid_var, width=22).grid(row=0, column=1, sticky="w", padx=(4, 14))
        ttk.Label(form, text="비밀번호").grid(row=0, column=2, sticky="w")
        key_var = tk.StringVar(value=self.config.wifi_key)
        ttk.Entry(form, textvariable=key_var, width=22).grid(row=0, column=3, sticky="w", padx=4)

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=10, pady=(0, 6))
        out = tk.Text(dlg, height=6, state="disabled", wrap="word")
        out.pack(fill="both", padx=10, pady=(0, 10))

        def say(txt):
            self._append(txt)
            try:
                out.config(state="normal")
                out.insert("end", txt + "\n")
                out.see("end")
                out.config(state="disabled")
            except Exception:
                pass

        all_btns: list = []

        def busy(on, msg=""):
            for b in all_btns:
                try:
                    b.config(state="disabled" if on else "normal")
                except Exception:
                    pass
            if msg:
                st.set(msg)
            try:
                dlg.config(cursor="watch" if on else "")
            except Exception:
                pass

        def show_lists(data):
            conf_lb.delete(0, "end")
            for n in data.get("configured") or []:
                conf_lb.insert("end", n.get("ssid", ""))
            scan_tv.delete(*scan_tv.get_children())
            for n in data.get("scanned") or []:
                scan_tv.insert("", "end", values=(n.get("ssid", ""), n.get("rssi", ""), n.get("flags", "")))

        def run_op(name, fn, done):
            busy(True, f"{name} 중… (수 초 ~ 수십 초, 카드가 응답할 때까지)")
            def worker():
                from .card_wifi import CardWifiError
                try:
                    r = fn()
                    self.root.after(0, lambda: (busy(False), done(r)))
                except CardWifiError as e:
                    msg, data = str(e), getattr(e, "data", None)
                    def fail():
                        busy(False)
                        say(f"[Wi-Fi 실패] {name}: {msg}")
                        if data:
                            show_lists(data)
                    self.root.after(0, fail)
                except Exception as e:
                    msg = f"예상치 못한 오류: {e}"
                    self.root.after(0, lambda: (busy(False), say(f"[Wi-Fi 오류] {name}: {msg}")))
            import threading
            threading.Thread(target=worker, daemon=True).start()

        def do_read():
            from . import card_wifi
            def done(r):
                st.set(f"카드 {r.get('mac', '?')} — 저장된 망 {len(r.get('configured') or [])}개, "
                       f"스캔 {len(r.get('scanned') or [])}개")
                show_lists(r)
                say(f"[카드 읽기] {r.get('mac')} status={r.get('status')}")
            run_op("카드 읽기", card_wifi.list_networks, done)

        def do_test():
            from . import card_wifi
            s, k = ssid_var.get().strip(), key_var.get()
            if not s:
                return
            def done(r):
                say(f"[테스트] {s}: rc={r.get('rc')} auth={r.get('auth_used')} "
                    f"status_after={r.get('status_after')}")
            run_op("연결 테스트", lambda: card_wifi.test_network(s, k), done)

        def do_add():
            from . import card_wifi
            s, k = ssid_var.get().strip(), key_var.get()
            if not s:
                return
            def done(r):
                okmsg = "등록 확인됨 ✅" if r.get("registered") else \
                    f"rc={r.get('rc')} — 재조회에서 미확인, 목록을 확인하세요"
                say(f"[카드에 등록] {s} (auth={r.get('auth_used')}): {okmsg}")
                show_lists(r)
                self.config.wifi_ssid, self.config.wifi_key = s, k
                self.config.save()
            run_op("카드에 등록", lambda: card_wifi.add_network(s, k), done)

        def do_delete():
            from . import card_wifi
            sel = conf_lb.curselection()
            if not sel:
                messagebox.showinfo("선택 없음", "왼쪽 '카드에 저장된 망'에서 삭제할 망을 고르세요.", parent=dlg)
                return
            s = conf_lb.get(sel[0])
            if not messagebox.askyesno("망 삭제", f"카드에서 '{s}' 를 삭제할까요?", parent=dlg):
                return
            def done(r):
                say(f"[망 삭제] {s}: " + ("삭제 확인됨 ✅" if r.get("removed") else f"rc={r.get('rc')}"))
                show_lists(r)
            run_op("망 삭제", lambda: card_wifi.delete_network(s), done)

        for text, cmd in (("▷ 카드 읽기", do_read), ("연결 테스트", do_test),
                          ("★ 카드에 등록", do_add), ("선택 망 삭제", do_delete)):
            b = ttk.Button(btns, text=text, command=cmd)
            b.pack(side="left", padx=3)
            all_btns.append(b)
        ttk.Button(btns, text="닫기", command=dlg.destroy).pack(side="right")

    # ---- 리더 직접 가져오기 (공식앱의 카드리더 가져오기 대응) ----
    def _toggle_reader_auto(self):
        self.config.reader_autoimport = bool(self.reader_auto_var.get())
        self.config.save()
        self._append("리더 자동 가져오기 " + ("켜짐" if self.config.reader_autoimport else "꺼짐"))

    def _poll_reader(self):
        """10초 주기: 리더에 꽂힌 Eye-Fi 카드를 감지·분류해 알린다.
        - 새 카드(공장 초기: EYEFI 없음, 라벨 'Eye-Fi') → 인식 후 등록 안내
        - 구형 카드(EYEFI 있음) → 등록 여부 표시, 등록 카드 + DCIM 있으면 자동 가져오기
        자동 가져오기는 '등록된 카드'만 — 낯선/개인 카드를 무분별하게 빨아들이지 않도록."""
        try:
            from . import card_mailbox
            import string
            cards, fresh = set(), set()
            for c in string.ascii_uppercase:
                root = c + ":\\"
                try:
                    if card_mailbox.is_fresh_card(root):
                        fresh.add(root); cards.add(root)
                    elif card_mailbox.is_eyefi_drive(root):
                        cards.add(root)
                except OSError:
                    continue
            known = self._known_reader_drives
            self._known_reader_drives = cards
            for d in cards - known:
                if d in fresh:
                    self._append(f"[리더] 🆕 새 카드(공장 초기) 감지({d}) — 인식 중…")
                    self._notify_fresh_card(d)
                else:
                    self._append(f"[리더] 구형 카드 감지({d}) — 등록 여부 확인 중…")
                    self._identify_card(d)
        except Exception:
            self._flog("리더 폴링 오류:\n" + traceback.format_exc())
        finally:
            try:
                self.root.after(10000, self._poll_reader)
            except Exception:
                pass

    def _notify_fresh_card(self, drive: str):
        """공장 초기 카드: 메일박스를 만들고(MAC 판독이 곧 부트스트랩) 등록을 안내."""
        def worker():
            mac, err = None, None
            try:
                from .card_reader import read_card
                info = read_card(drive=drive.rstrip("\\"))
                mac = normalize_mac(info.get("mac", ""))
            except Exception as e:
                err = str(e)

            def done():
                if mac and not self.config.get_card(mac):
                    self._append(f"[리더] 🆕 새 카드(공장 초기) {mac} 인식 완료 — '🔑 카드를 앱에 등록' 버튼으로 등록하세요")
                    self._show_alert_toast("🆕 새 Eye-Fi 카드",
                                           [f"새 카드(공장 초기) {mac} 를 인식했습니다",
                                            "'🔑 카드를 앱에 등록' 버튼으로 등록하세요"],
                                           timeout_ms=10000)
                elif mac:
                    self._append(f"[리더] 카드 {mac} — 공장 초기 상태지만 이미 등록된 카드입니다")
                else:
                    self._append(f"[리더] 새 카드 인식 실패: {err}")
            self.root.after(0, done)

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _identify_card(self, drive: str):
        """구형(EYEFI 있는) 카드: MAC 을 읽어 새/구형·등록 여부를 알리고,
        등록 카드 + DCIM 있음 + 리더 자동 켬이면 자동 가져오기. 미등록/식별불가는
        수동 버튼으로 유도(로그+토스트) — 자동이 임의 카드를 복사하는 사고 방지."""
        def worker():
            mac, err = None, None
            try:
                from .card_reader import read_card
                info = read_card(drive=drive.rstrip("\\"))
                mac = normalize_mac(info.get("mac", ""))
            except Exception as e:
                err = str(e)

            def done():
                has_photos = os.path.isdir(os.path.join(drive, "DCIM"))
                if mac and self.config.get_card(mac):
                    name = (self.config.get_card(mac) or {}).get("name", mac)
                    self._append(f"[리더] 구형 카드 — 등록된 카드 {name}({mac})")
                    if has_photos and self.config.reader_autoimport and not self._importing:
                        self._append(f"[리더] {name} → 자동 가져오기 시작")
                        self._run_reader_import(drive)
                elif mac:
                    self._append(f"[리더] 구형 카드 {mac} — 이 PC에는 미등록 "
                                 f"(등록: '🔑 카드를 앱에 등록' / 사진만: '💾 리더에서 가져오기')")
                    self._show_alert_toast("ℹ 미등록 구형 카드",
                                           [f"구형 카드 {mac} 는 이 PC에 등록돼 있지 않습니다",
                                            "'🔑 카드를 앱에 등록' 버튼으로 등록할 수 있습니다"],
                                           timeout_ms=8000)
                else:
                    self._append(f"[리더] 카드 식별 실패 — 자동 건너뜀(수동 버튼 사용 가능): {err}")
            self.root.after(0, done)

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _manual_reader_import(self):
        drives = reader_import.find_eyefi_drives()
        if not drives:
            messagebox.showinfo("카드 없음", "리더에서 Eye-Fi 카드를 찾지 못했습니다.\n(EYEFI/DCIM 폴더가 있는 이동식 드라이브)")
            return
        if self._importing:
            self._append("[리더] 이미 가져오는 중")
            return
        self._run_reader_import(drives[0])

    def _run_reader_import(self, drive: str):
        dest = self.config.default_folder
        if not dest:
            self._append("[리더] 기본 저장폴더 미설정 — 가져오기 취소")
            return
        self._importing = True

        def worker():
            try:
                r = reader_import.import_photos(
                    drive, dest, date_subfolders=self.config.date_subfolders)
            except Exception as e:
                r = {"copied": [], "skipped": 0, "errors": [(drive, str(e))]}
            self.root.after(0, lambda: self._reader_import_done(drive, r))

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _reader_import_done(self, drive: str, r: dict):
        self._importing = False
        copied, skipped, errors = r["copied"], r["skipped"], r["errors"]
        self._append(f"[리더] {drive} 가져오기 완료 — 새로 {len(copied)}장, 중복 건너뜀 {skipped}장"
                     + (f", 오류 {len(errors)}건" if errors else ""))
        for src, msg in errors[:3]:
            self._append(f"  [리더 오류] {os.path.basename(src)}: {msg}")
        for p in copied[-self.MAX_THUMBS:]:
            try:
                self._add_thumbnail({"path": p, "size": os.path.getsize(p)})
            except Exception:
                pass
        if copied or errors:
            lines = [f"새로 {len(copied)}장 저장" + (f", 중복 {skipped}장 제외" if skipped else "")]
            if errors:
                lines.append(f"오류 {len(errors)}건 — 로그 확인")
            self._show_alert_toast("💾 리더 가져오기 완료", lines, timeout_ms=6000)

    def _edit_card(self):
        sel = self.tree.selection()
        if not sel:
            return
        mac = sel[0]
        info = self.config.get_card(mac) or {}
        r = self._card_dialog(mac, info.get("name", ""), info.get("uploadkey", ""), info.get("folder", ""))
        if r:
            if r["mac"] != mac:
                self.config.remove_card(mac)
            self.config.add_or_update_card(r["mac"], r["uploadkey"], r["name"], r["folder"])
            self.config.save()
            self._refresh_cards()

    def _del_card(self):
        sel = self.tree.selection()
        if sel and messagebox.askyesno("삭제", f"{sel[0]} 삭제?"):
            self.config.remove_card(sel[0])
            self.config.save()
            self._refresh_cards()

    # ---- 이벤트/로그 ----
    def _drain_events(self):
        # 어떤 이벤트에서 예외가 나도 루프가 죽지 않도록 이벤트별 격리 + 항상 재스케줄.
        try:
            while True:
                try:
                    kind, info = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._on_event(kind, info)
                except Exception:
                    self._flog("이벤트 처리 오류 (" + str(kind) + "):\n" + traceback.format_exc())
        finally:
            try:
                self.root.after(200, self._drain_events)
            except Exception:
                pass

    def _flog(self, text: str):
        """파일 로그 (pythonw 에서도 원인 추적 가능). 실패해도 무시."""
        try:
            with open(self._logpath, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  " + text + "\n")
        except Exception:
            pass

    def _on_event(self, kind, info):
        if kind == "upload_start":
            self._start_progress_popup(info.get("total", 0))
        elif kind == "upload_progress":
            self._update_progress_popup(info.get("got", 0), info.get("total", 0))
        elif kind == "received":
            gap = info.get("gap_sec")
            gtxt = "첫 장" if gap is None else f"간격 {gap}s"
            self._append(f"[받음] {os.path.basename(info['path'])}  ({info['size']//1024}KB, {gtxt})  {info['mac']}")
            self._add_thumbnail(info)
            # 진행 팝업이 있으면 그걸 썸네일로 전환, 없으면 일반 썸네일 팝업
            if self._prog is not None:
                self._finish_progress_popup(info)
            else:
                self._show_toast(info)
        elif kind == "session":
            self._append(f"[세션] {info['mac']} {'(등록됨)' if info['known'] else '(미등록!)'}")
        elif kind == "unknown_card":
            self._append(f"[경고] 미등록 카드 {info['mac']} — 등록 필요")
        elif kind == "photo_status":
            if info.get("cred_ok"):
                self._append(f"[인증] {info['mac']} credential=OK[{info.get('cred_variant','')}] {info.get('filename','')}")
            else:
                self._append(f"[인증] {info['mac']} credential=거부(불일치, 403) {info.get('filename','')} — 카드가 StartSession부터 재시도")
        elif kind == "error":
            self._append(f"[오류] {info.get('msg')}")
            self._close_progress_popup()
        elif kind == "partial":
            need = info.get("need") or 0
            got = info.get("got") or 0
            pct = int(100 * got / need) if need else 0
            self._append(f"[이어받기] {info.get('file')}  {got // 1024}/{need // 1024}KB ({pct}%)")
            self._close_progress_popup()   # 이번 연결은 부분 → 다음 이어받기에서 새 팝업
        elif kind == "upload_empty":
            self._append(f"[알림] {info.get('mac')} 업로드에 미디어 없음")
            self._close_progress_popup()
        elif kind == "duplicate":
            self._append(f"[중복 건너뜀] {os.path.basename(info.get('path', ''))} — 이미 저장돼 있음 (리더 가져오기와 중복)")
            self._close_progress_popup()
        elif kind == "stalled":
            lines = []
            for s in info.get("files", []):
                need, got = s.get("need") or 0, s.get("got") or 0
                pct = f" ({int(100 * got / need)}%)" if need else ""
                mins = s.get("age_sec", 0) // 60
                line = f"{s.get('file')} {got // 1024}KB" + \
                    (f"/{need // 1024}KB{pct}" if need else "") + f" — {mins}분째 멈춤"
                self._append("[미전송 대기] " + line)
                lines.append(line)
            if lines:
                self._show_alert_toast("⚠ 전송이 끝나지 않은 사진",
                                       lines + ["카메라 전원을 켜면 이어서 수신됩니다"])
        elif kind == "stall_giveup":
            f = info.get("file", "")
            self._append(f"[전송 포기] {f} — {info.get('age_days')}일째 진행 없어 대기 종료 "
                         f"(카메라에서 삭제된 사진일 수 있음)")
            self._show_alert_toast("⏹ 전송 대기 종료",
                                   [f"{f} — {info.get('age_days')}일째 진행 없음",
                                    "사진이 필요하면 카드를 리더에 꽂으세요 (자동 가져오기)"])

    MAX_THUMBS = 8

    def _add_thumbnail(self, info):
        path = info.get("path")
        if not path:
            return
        try:
            from PIL import Image, ImageTk, ImageOps
        except Exception:
            return
        try:
            im = Image.open(path)
            im = ImageOps.exif_transpose(im)   # 카메라 회전 반영
            im.thumbnail((100, 100))           # 작게 (스트립에 여러 장)
            photo = ImageTk.PhotoImage(im)
        except Exception as e:
            self._append(f"[미리보기 실패] {os.path.basename(path)}: {e}")
            return
        if self.thumb_hint is not None:
            self.thumb_hint.destroy()
            self.thumb_hint = None
        cell = ttk.Frame(self.thumb_row)
        img_lbl = ttk.Label(cell, image=photo)
        img_lbl.image = photo  # GC 방지: 참조 유지
        img_lbl.pack()
        gap = info.get("gap_sec")
        sub = f"{info.get('size', 0) // 1024}KB" + ("" if gap is None else f" · {gap}s")
        ttk.Label(cell, text=os.path.basename(path), font=("", 8)).pack()
        ttk.Label(cell, text=sub, font=("", 8), foreground="#888").pack()
        # 최신은 맨 왼쪽
        if self.thumbs:
            cell.pack(side="left", padx=4, anchor="n", before=self.thumbs[0])
        else:
            cell.pack(side="left", padx=4, anchor="n")
        self.thumbs.insert(0, cell)
        while len(self.thumbs) > self.MAX_THUMBS:
            self.thumbs.pop().destroy()  # 가장 오래된 것 제거

    def _position_popup(self, win):
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        margin, taskbar = 14, 52
        offset = 0
        for x in self._toasts:
            try:
                if x.winfo_exists():
                    offset += x.winfo_height() + 8
            except Exception:
                pass
        win.geometry(f"+{sw - w - margin}+{sh - h - taskbar - margin - offset}")

    def _start_progress_popup(self, total):
        """업로드 시작 → '수신 중' 진행바 팝업 (우하단)."""
        if not self.toast_var.get():
            return
        self._close_progress_popup()
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        try:
            t.attributes("-alpha", 0.96)
        except Exception:
            pass
        frm = tk.Frame(t, bg="#1f2430", bd=1, relief="solid")
        frm.pack(fill="both", expand=True)
        tk.Label(frm, text="📥 사진 수신 중…", fg="#7fd1ff", bg="#1f2430",
                 font=("맑은 고딕", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 6))
        var = tk.DoubleVar(value=0)
        ttk.Progressbar(frm, length=230, mode="determinate", maximum=100, variable=var).pack(padx=12)
        pct = tk.Label(frm, text="0%", fg="#aab2c0", bg="#1f2430", font=("맑은 고딕", 8))
        pct.pack(anchor="w", padx=12, pady=(4, 10))
        self._prog = {"win": t, "var": var, "pct": pct, "total": total or 0}
        self._position_popup(t)
        # 전송이 멈춰버린 경우 대비 안전 자동정리
        t.after(45000, lambda w=t: self._close_progress_popup()
                if (self._prog and self._prog.get("win") is w) else None)

    def _update_progress_popup(self, got, total):
        if not self._prog:
            return
        total = total or self._prog.get("total") or 0
        if not total:
            return
        p = min(100, int(100 * got / total))
        try:
            self._prog["var"].set(p)
            self._prog["pct"].config(text=f"{p}%    {got // 1024}/{total // 1024}KB")
        except Exception:
            pass

    def _finish_progress_popup(self, info):
        """진행 팝업을 '수신 완료 + 썸네일'로 전환 후 잠시 뒤 닫기."""
        pr = self._prog
        self._prog = None
        if not pr:
            self._show_toast(info)
            return
        win = pr["win"]
        try:
            if not win.winfo_exists():
                self._show_toast(info)
                return
            for c in win.winfo_children():
                c.destroy()
        except Exception:
            self._show_toast(info)
            return
        path = info.get("path")
        photo = None
        try:
            from PIL import Image, ImageTk, ImageOps
            im = Image.open(path)
            im = ImageOps.exif_transpose(im)
            im.thumbnail((84, 84))
            photo = ImageTk.PhotoImage(im)
        except Exception:
            pass
        frm = tk.Frame(win, bg="#1f2430", bd=1, relief="solid")
        frm.pack(fill="both", expand=True)
        if photo is not None:
            il = tk.Label(frm, image=photo, bg="#1f2430")
            il.image = photo
            il.pack(side="left", padx=8, pady=8)
        txt = tk.Frame(frm, bg="#1f2430")
        txt.pack(side="left", padx=(0, 12), pady=8, fill="y")
        tk.Label(txt, text="✅ 수신 완료", fg="#8fe388", bg="#1f2430",
                 font=("맑은 고딕", 10, "bold")).pack(anchor="w")
        tk.Label(txt, text=os.path.basename(path or ""), fg="#eeeeee", bg="#1f2430",
                 font=("맑은 고딕", 9)).pack(anchor="w")
        gap = info.get("gap_sec")
        sub = f"{info.get('size', 0) // 1024}KB" + ("" if gap is None else f"  ·  간격 {gap}s")
        tk.Label(txt, text=sub, fg="#aab2c0", bg="#1f2430", font=("맑은 고딕", 8)).pack(anchor="w")
        self._position_popup(win)
        self._toasts.append(win)

        def close():
            try:
                self._toasts.remove(win)
            except ValueError:
                pass
            try:
                win.destroy()
            except Exception:
                pass

        win.bind("<Button-1>", lambda e: close())
        win.after(3500, close)

    def _close_progress_popup(self):
        if self._prog:
            try:
                self._prog["win"].destroy()
            except Exception:
                pass
            self._prog = None

    def _show_toast(self, info):
        """전송 시 화면 우하단에 썸네일 팝업 (앱 창 안 열어도 보임). 4.5초 뒤 자동 사라짐."""
        if not self.toast_var.get():
            return
        path = info.get("path")
        if not path:
            return
        try:
            from PIL import Image, ImageTk, ImageOps
        except Exception:
            return
        photo = None
        try:
            im = Image.open(path)
            im = ImageOps.exif_transpose(im)
            im.thumbnail((84, 84))
            photo = ImageTk.PhotoImage(im)
        except Exception:
            pass
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)          # 테두리 없음
        t.attributes("-topmost", True)    # 항상 위
        try:
            t.attributes("-alpha", 0.96)
        except Exception:
            pass
        frm = tk.Frame(t, bg="#1f2430", bd=1, relief="solid")
        frm.pack(fill="both", expand=True)
        if photo is not None:
            il = tk.Label(frm, image=photo, bg="#1f2430")
            il.image = photo  # GC 방지
            il.pack(side="left", padx=8, pady=8)
        txt = tk.Frame(frm, bg="#1f2430")
        txt.pack(side="left", padx=(0, 12), pady=8, fill="y")
        tk.Label(txt, text="📷 사진 수신", fg="#7fd1ff", bg="#1f2430", font=("맑은 고딕", 10, "bold")).pack(anchor="w")
        tk.Label(txt, text=os.path.basename(path), fg="#eeeeee", bg="#1f2430", font=("맑은 고딕", 9)).pack(anchor="w")
        gap = info.get("gap_sec")
        sub = f"{info.get('size', 0) // 1024}KB" + ("" if gap is None else f"  ·  간격 {gap}s")
        tk.Label(txt, text=sub, fg="#aab2c0", bg="#1f2430", font=("맑은 고딕", 8)).pack(anchor="w")

        t.update_idletasks()
        w, h = t.winfo_width(), t.winfo_height()
        sw, sh = t.winfo_screenwidth(), t.winfo_screenheight()
        margin, taskbar = 14, 52
        offset = 0
        for x in self._toasts:
            try:
                if x.winfo_exists():
                    offset += x.winfo_height() + 8
            except Exception:
                pass
        px = sw - w - margin
        py = sh - h - taskbar - margin - offset
        t.geometry(f"+{px}+{py}")
        self._toasts.append(t)

        def close():
            try:
                self._toasts.remove(t)
            except ValueError:
                pass
            try:
                t.destroy()
            except Exception:
                pass

        t.bind("<Button-1>", lambda e: close())   # 클릭하면 닫힘
        t.after(4500, close)

    def _show_alert_toast(self, title, lines, timeout_ms=12000):
        """사진 없는 경고성 팝업(주황) — 미전송 대기, 서버 복구 등. 클릭 닫기."""
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        try:
            t.attributes("-alpha", 0.97)
        except Exception:
            pass
        frm = tk.Frame(t, bg="#3a2b18", bd=1, relief="solid")
        frm.pack(fill="both", expand=True)
        tk.Label(frm, text=title, fg="#ffcf7f", bg="#3a2b18",
                 font=("맑은 고딕", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 4))
        for ln in lines[:6]:
            tk.Label(frm, text=ln, fg="#eeeeee", bg="#3a2b18",
                     font=("맑은 고딕", 9)).pack(anchor="w", padx=12)
        tk.Label(frm, text="", bg="#3a2b18").pack(pady=(0, 8))
        self._position_popup(t)
        self._toasts.append(t)

        def close():
            try:
                self._toasts.remove(t)
            except ValueError:
                pass
            try:
                t.destroy()
            except Exception:
                pass

        t.bind("<Button-1>", lambda e: close())
        t.after(timeout_ms, close)

    def _watch_server(self):
        """서버 자가복구: 켜져 있어야 할 서버가 죽었으면 자동 재시작 (1분 주기)."""
        try:
            if self.server is not None and not self._quitting:
                import socket
                ok = False
                try:
                    with socket.create_connection(("127.0.0.1", self.config.port), timeout=2):
                        ok = True
                except OSError:
                    ok = False
                if not ok:
                    self._append("[워치독] 수신 서버 응답 없음 → 자동 재시작")
                    try:
                        self._stop_server()
                    except Exception:
                        self.server = None
                    self._start_server(silent=True)
                    if self.server:
                        self._show_alert_toast("🔁 수신 서버 자동 복구",
                                               ["서버가 멈춰 있어 자동 재시작했습니다."])
        except Exception:
            self._flog("서버 워치독 오류:\n" + traceback.format_exc())
        finally:
            try:
                self.root.after(60000, self._watch_server)
            except Exception:
                pass

    def _append(self, text):
        self._flog(text)
        try:
            self.log.config(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.config(state="disabled")
            # 로그 폭주 방지: 500줄 넘으면 앞부분 잘라냄
            if int(self.log.index("end-1c").split(".")[0]) > 500:
                self.log.config(state="normal")
                self.log.delete("1.0", "100.0")
                self.log.config(state="disabled")
        except Exception:
            pass

    def run(self):
        self.root.mainloop()
        if self.server:
            try:
                self.server.shutdown()
            except Exception:
                pass
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass


def main(with_tray: bool = False, start_hidden: bool = False, autostart_server: bool = False):
    from . import paths
    ReceiverGUI(paths.data_path("cards.json"), with_tray=with_tray,
                start_hidden=start_hidden, autostart_server=autostart_server).run()


if __name__ == "__main__":
    main()
