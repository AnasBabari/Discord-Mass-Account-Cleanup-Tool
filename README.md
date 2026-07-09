# Discord Mass Account Cleanup Tool

A desktop app and CLI tool for quickly cleaning up your Discord account. Mass leave servers, remove friends, and clear notifications — all in one place.

![Screenshot](source/assets/screenshot.png)

## Download

> **No Python required** — grab the latest pre-built Windows executable from the [**Releases page**](https://github.com/AnasBabari/Discord-Mass-Account-Cleanup-Tool/releases/latest).

Just download `Discord-Mass-Cleanup-Tool.exe`, double-click, and go.

## Features

- **Mass Leave Servers** — select individually, by range, or all at once
- **Mass Remove Friends** — same flexible selection with search filtering
- **Mass Block Users** — bulk-block selected friends
- **Mass Read Notifications** — instantly mark all DMs, group chats, and server channels as read
- **Desktop GUI** — polished PyQt5 interface with dark theme, real-time progress, and logging
- **CLI Mode** — fully interactive terminal interface, no GUI required
- **Secure Token Storage** — token saved to your OS credential manager via `keyring`
- **Rate-Limit Handling** — automatic retry with backoff on Discord 429s and Cloudflare blocks

## For Developers

All source code lives in the [`source/`](source/) directory.

### Requirements

- Python 3.10+

```bash
cd source
pip install -r requirements.txt
```

### Run from source

**GUI (recommended):**
```bash
cd source
python gui_app.py
```

**CLI:**
```bash
cd source
python discord_mass_cleanup.py
```

Follow the on-screen instructions. You'll be prompted to paste your user token securely and then choose which cleanup operation to perform.

### Build the exe yourself

```bash
cd source
pip install pyinstaller
pyinstaller gui_app.spec
```

The built exe will be at `source/dist/gui_app.exe`.

## How to Get Your Discord User Token

1. Open a browser and go to the [Discord Web App](https://discord.com/app).
2. Log in to your account.
3. Open Developer Tools (`F12` or `Ctrl+Shift+I` / `Cmd+Option+I`).
4. Go to the **Application** tab (click `>>` if hidden).
5. In the left sidebar, expand **Local Storage** → click `https://discord.com`.
6. Press `Ctrl+Shift+M` (`Cmd+Shift+M` on Mac) to toggle mobile device emulation — Discord hides the token on desktop views.
7. Type `token` in the filter bar. Copy the value (without surrounding quotes).

## Testing

```bash
cd source
pytest test_discord_mass_cleanup.py -v
```

## Project Structure

```
├── README.md
├── LICENSE
├── .github/workflows/release.yml   # CI: auto-build exe on tag push
└── source/
    ├── gui_app.py                   # PyQt5 desktop GUI entry point
    ├── discord_mass_cleanup.py      # Core API logic & CLI entry point
    ├── workers.py                   # Background thread workers
    ├── gui_app.spec                 # PyInstaller build config
    ├── requirements.txt
    ├── ui/
    │   ├── theme.py                 # Color constants & QSS loader
    │   ├── theme.qss                # Qt stylesheet (dark theme)
    │   ├── components.py            # Reusable widgets (loading overlay, toasts)
    │   └── pages/
    │       ├── login.py             # Token input & auth page
    │       ├── servers.py           # Server list & leave functionality
    │       ├── friends.py           # Friends list, remove & block
    │       ├── notifications.py     # Bulk mark-as-read
    │       └── logs.py              # Terminal-style log viewer
    ├── test_discord_mass_cleanup.py
    ├── test_gui.py
    └── assets/
        └── screenshot.png
```

## Disclaimer

> ⚠️ This tool uses your personal Discord user token. Automating user accounts ("self-botting") is against Discord's Terms of Service. Use at your own discretion. It is generally low risk for a one-off cleanup, but you are solely responsible for your account.

## License

This project is licensed under the [MIT License](LICENSE).
