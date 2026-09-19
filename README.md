# EyeFiReceiver

**Local, cloud-free receiver for Eye-Fi X2 Wi-Fi SD cards.**

Eye-Fi shut down its cloud service in 2016, which stranded a whole generation of
Eye-Fi **X2** Wi-Fi SD cards — without the cloud, the official software could no
longer activate them or receive their photos. EyeFiReceiver brings them back to
life: it registers a card and receives its photos over Wi-Fi entirely on your own
PC. No Eye-Fi cloud, no account, no official app, no registry changes, no DLLs.

Written in **pure Python** (standard library only; `pystray` + `pillow` add the
tray icon and thumbnails). The Eye-Fi upload and card-configuration protocols were
reverse-engineered and are re-implemented from scratch here.

**Made by Minsik Choi** — contact / update requests: **msclub@naver.com** (KakaoTalk ID: **msclub77**)

---

## Features

- **Receives photos over Wi-Fi** — a SOAP upload server on TCP port `59278`,
  the same endpoint Eye-Fi X2 cards transfer to.
- **Resumable, loss-free transfers** — incoming bytes are streamed to a spool as
  they arrive, so a dropped Wi-Fi connection resumes from the exact byte offset
  instead of restarting. A stalled transfer is detected and can be picked up when
  the camera powers back on.
- **Register cards without the cloud** — reads a card's MAC and upload key directly
  from the card's on-board mailbox (no `EyeFiCard.dll`, no vendor software).
- **Set the card's Wi-Fi** — add/remove the networks the card connects to, straight
  from the app.
- **Handles factory-fresh and camera-formatted cards** — bootstraps the card's
  mailbox when a camera format has wiped it.
- **Firmware-aware** — reads the card firmware and, when an older firmware hides the
  upload key, explains exactly what's happening and what to do (see Troubleshooting).
- **One-click diagnostics** — export a full card report (firmware, tokens, verdict)
  to a text file for troubleshooting.
- **Tray app** — live thumbnails of incoming photos, transfer pop-ups, optional
  boot auto-start, and duplicate-safe direct import from a card reader.
- **Photos filed by capture date**, into per-card folders.
- **40 offline unit tests** — the whole flow is tested without a physical card.

## How it works

Eye-Fi X2 cards are HTTP clients. When a card joins Wi-Fi it connects to a receiver
and speaks a small SOAP protocol:

1. **StartSession** — the card sends its MAC and a nonce; the server answers with a
   credential, `MD5(unhexlify(mac + uploadkey + snonce))`, proving it knows the
   card's secret upload key. This is why each card must be registered with its key.
2. **GetPhotoStatus** — for each photo the card asks where to resume; the server
   replies with the byte offset already received (0 for a new file).
3. **UploadPhoto** — the photo arrives as `multipart/form-data` wrapping a `tar`;
   the server appends the bytes to a spool, and once the full file has arrived it
   extracts the JPEG and files it by capture date. If the connection drops mid-file,
   the next `GetPhotoStatus` resumes exactly where it left off — no corruption, no
   duplicate bytes.

Card **configuration** (registering, reading firmware, setting Wi-Fi) does not use
the network at all — it goes through the card's own `EYEFI/` mailbox (four 16 KB
files: `REQM`/`RSPM`/`REQC`/`RSPC`) using cache-bypassing raw disk writes, so the
commands actually reach the card's controller instead of sitting in the OS cache.
This replaces the vendor's 32-bit `EyeFiCard.dll` with portable Python.

## Requirements

- Windows (the card-mailbox raw I/O is currently Windows-only)
- Python 3.12+
- `python -m pip install pystray pillow`

## Run

```
python -m EyeFiReceiver --tray                          # tray-resident
python -m EyeFiReceiver                                  # plain window
python -m unittest EyeFiReceiver.tests.test_protocol     # run the tests
```

## Registering a card

1. Put the Eye-Fi card in a reader.
2. Open the app → **🔑 Register card** — it reads the card's MAC and upload key.
3. **📶 Card Wi-Fi** — write the Wi-Fi network the card should connect to.
4. Pick a save folder and start the server. Shoot a photo; it arrives automatically.

Your card list and received photos stay on your PC (`cards.json` is git-ignored).

## Troubleshooting: "UploadKey is blank"

Some genuine X2 cards read back an **empty upload key** even though the key is
physically on the card. This is a **firmware** quirk, not a broken card:

- Firmware **5.0 and older** do not expose the upload key (the read returns empty).
- Firmware **5.2010 (Aug 2013) and newer** return the 32-character key normally.

The app reads the firmware and tells you which case you're in (**🔎 Check firmware**).
If the key is hidden, options are:

- Enter the key manually if you already have it (e.g. from an old Eye-Fi Center
  install's `Settings.xml`, where activated cards' keys were stored), or
- Update the card's firmware to 5.2010 with the official Eye-Fi X2 Utility, if you
  can still run it.

Either way, once the receiver has the correct upload key, an old-firmware card
transfers photos exactly like any other — the firmware only affects *reading* the
key, not the transfer itself. If you're stuck, use **🩺 Diagnostics** to export a
report and open an issue.

## Build a Windows installer

See [installer/BUILD.md](installer/BUILD.md) — PyInstaller bundles the app, then
Inno Setup produces a user-level installer (no admin rights, Python bundled).

## Project layout

```
EyeFiReceiver/
  eyefi_server.py     SOAP upload server (port 59278) + resumable spool + watchdog
  eyefi_protocol.py   credential handshake, integrity digest, multipart/tar parsing
  card_mailbox.py     direct card communication (register / firmware / Wi-Fi), no DLL
  card_reader.py      card read helper (+ diagnostics export)
  card_wifi.py        card Wi-Fi query/set
  reader_import.py    direct import from a card reader, with a de-dup ledger
  gui.py              Tkinter window + system tray
  config.py           cards.json settings (per-PC, git-ignored)
  docs/               Eye-Fi protocol reference (upstream eyefi-config, GPLv2)
  tests/              offline unit tests (no physical card required)
```

## Credits

The protocol understanding builds on the community project
[**eyefi-config**](https://github.com/hansendc/eyefi-config) by Dave Hansen
(bundled under `EyeFiReceiver/docs/` as reference, GPLv2). The Python code here is
an independent, clean-room implementation.

## License

[MIT](LICENSE). The bundled protocol reference under `EyeFiReceiver/docs/` is GPLv2
(upstream eyefi-config), included for documentation only.
