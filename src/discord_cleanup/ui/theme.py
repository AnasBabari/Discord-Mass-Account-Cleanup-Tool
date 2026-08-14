import os
import sys

# Palette Tokens
BG_DARKEST = "#06090e"
BG_SIDEBAR = "#090e15"
BG_MAIN = "#080d15"
BG_CARD = "#0f1824"
BG_CARD_HOVER = "#152030"
BG_INPUT = "#0c141f"

# Accent Colors
ACCENT = "#38bdf8"
ACCENT_HOVER = "#7dd3fc"
ACCENT_MUTED = "#0284c7"

# Status Colors
DANGER = "#f43f5e"
DANGER_HOVER = "#fb7185"
DANGER_BG = "rgba(244, 63, 94, 0.12)"
SUCCESS = "#34d399"
WARNING = "#fbbf24"

# Typography Colors
TEXT_PRIMARY = "#f8fafc"
TEXT_SECONDARY = "#94a3b8"
TEXT_MUTED = "#94a3b8"
TEXT_DIM = "#64748b"


def get_qss_path() -> str:
    """Resolve theme.qss path whether running from source or packaged executable."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "ui", "theme.qss")  # type: ignore[attr-defined]
    return os.path.join(os.path.dirname(__file__), "theme.qss")


def load_stylesheet() -> str:
    path = get_qss_path()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return ""
