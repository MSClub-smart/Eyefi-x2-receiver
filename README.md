# EyeFiReceiver

Local, cloud-free receiver for **Eye-Fi X2** Wi-Fi SD cards.

Eye-Fi shut down its cloud service in 2016, which left many X2 cards unable to
transfer photos. EyeFiReceiver revives them: it registers a card and receives
its photos over Wi-Fi entirely on your own PC — no Eye-Fi cloud, no official
app, no registry changes. Pure Python (standard library + `pystray`/`pillow`
for the tray/thumbnails).

**Made by Minsik Choi** — contact / update requests: msclub@naver.com (KakaoTalk ID: msclub77)

## Features
- SOAP upload receiver on TCP port 59278 (StartSession / GetPhotoStatus / UploadPhoto)
- Resumable, streaming spool — a dropped transfer resumes from its exact offset (no data loss)
- Pure-Python card mailbox (`card_mailbox.py`) — register cards & set their Wi-Fi
  directly through the card's `EYEFI/` mailbox, no `EyeFiCard.dll` required
- Reads a card's firmware and warns when an old firmware hides the upload key
- One-click diagnostic log export for troubleshooting a card that won't register
- Tray app with live thumbnails and transfer pop-ups; optional boot auto-start

## Run
```
python -m EyeFiReceiver --tray                          # tray-resident (omit --tray for a window)
python -m unittest EyeFiReceiver.tests.test_protocol    # tests
```

## Build a Windows installer
See [installer/BUILD.md](installer/BUILD.md) (PyInstaller + Inno Setup).

## Layout
- `EyeFiReceiver/eyefi_server.py`, `eyefi_protocol.py` — SOAP upload receiver + resumable spool
- `EyeFiReceiver/card_mailbox.py` — direct card communication (register / firmware / Wi-Fi)
- `EyeFiReceiver/card_reader.py`, `card_wifi.py` — card read & Wi-Fi helpers
- `EyeFiReceiver/gui.py` — Tkinter window + tray
- `EyeFiReceiver/config.py` — `cards.json` settings (per-PC, git-ignored)
- `EyeFiReceiver/docs/` — Eye-Fi protocol reference (upstream eyefi-config, GPLv2)

## Notes
- `cards.json` (your card keys) and received photos stay local and are git-ignored.
- Unsigned self-built installers may be blocked by Windows Smart App Control.

## License
MIT — see [LICENSE](LICENSE). The bundled protocol reference under
`EyeFiReceiver/docs/` is GPLv2 (upstream eyefi-config), included for documentation only.
