"""
config.py — 카드/폴더/서버 설정을 JSON 한 파일로 관리 (레지스트리·공식앱 불필요)

구조:
{
  "port": 59278,
  "default_folder": "C:/Eye-Fi Photos",
  "date_subfolders": true,
  "cards": {
     "00-18-56-12-34-56": {
        "name": "Eye-Fi Card 6b3587",
        "uploadkey": "00000000000000000000000000000000",
        "folder": "C:/Eye-Fi Photos"      # 생략 시 default_folder 사용
     }
  }
}
MAC 은 항상 소문자·콜론없는 '00-18-56-..' 표기로 정규화해 키로 사용.
"""
from __future__ import annotations
import json
import os

DEFAULT_PORT = 59278


def normalize_mac(mac: str) -> str:
    """'001856123456' / '00:18:56:6B:35:87' / '00-18-56-12-34-56' → '00-18-56-12-34-56'."""
    hexs = "".join(ch for ch in mac if ch in "0123456789abcdefABCDEF").lower()
    if len(hexs) != 12:
        return mac.strip().lower()
    return "-".join(hexs[i:i + 2] for i in range(0, 12, 2))


class Config:
    def __init__(self, path: str):
        self.path = path
        self.port = DEFAULT_PORT
        self.default_folder = ""
        self.date_subfolders = True
        self.show_toast = True   # 전송 시 우하단 썸네일 팝업
        self.wifi_ssid = ""      # 카드 Wi-Fi 등록 창의 기본 SSID (클리닉 망)
        self.wifi_key = ""       # 카드 Wi-Fi 등록 창의 기본 비밀번호 (로컬 파일 저장)
        self.reader_autoimport = True   # 리더에 카드 꽂으면 자동으로 사진 가져오기
        self.cards: dict[str, dict] = {}

    # ---- 영속화 ----
    @classmethod
    def load(cls, path: str) -> "Config":
        c = cls(path)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            c.port = int(d.get("port", DEFAULT_PORT))
            c.default_folder = d.get("default_folder", "")
            c.date_subfolders = bool(d.get("date_subfolders", True))
            c.show_toast = bool(d.get("show_toast", True))
            c.wifi_ssid = d.get("wifi_ssid", "")
            c.wifi_key = d.get("wifi_key", "")
            c.reader_autoimport = bool(d.get("reader_autoimport", True))
            for mac, info in (d.get("cards") or {}).items():
                c.cards[normalize_mac(mac)] = dict(info)
        return c

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        d = {
            "port": self.port,
            "default_folder": self.default_folder,
            "date_subfolders": self.date_subfolders,
            "show_toast": self.show_toast,
            "wifi_ssid": self.wifi_ssid,
            "wifi_key": self.wifi_key,
            "reader_autoimport": self.reader_autoimport,
            "cards": self.cards,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ---- 카드 관리 ----
    def add_or_update_card(self, mac: str, uploadkey: str, name: str = "", folder: str = "") -> None:
        mac = normalize_mac(mac)
        entry = self.cards.get(mac, {})
        entry["uploadkey"] = uploadkey.strip().lower()
        if name:
            entry["name"] = name
        elif "name" not in entry:
            entry["name"] = "Eye-Fi Card " + mac.replace("-", "")[-6:]
        if folder:
            entry["folder"] = folder
        self.cards[mac] = entry

    def remove_card(self, mac: str) -> None:
        self.cards.pop(normalize_mac(mac), None)

    def get_card(self, mac: str) -> dict | None:
        return self.cards.get(normalize_mac(mac))

    def folder_for(self, mac: str) -> str:
        card = self.get_card(mac) or {}
        return card.get("folder") or self.default_folder
