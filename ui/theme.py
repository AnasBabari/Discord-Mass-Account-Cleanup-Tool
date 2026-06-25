BG_DARKEST   = "#0a0e17"
BG_DARK      = "#0f1923"
BG_CARD      = "#141e2b"
BG_SIDEBAR   = "#0d1520"
BG_INPUT     = "#111b27"
BORDER       = "#1e2d3d"
BORDER_FOCUS = "#38bdf8"
ACCENT       = "#38bdf8"
ACCENT_HOVER = "#7dd3fc"
ACCENT_DIM   = "#1e3a5f"
ACCENT_GLOW  = "rgba(56, 189, 248, 0.15)"
TEXT_PRIMARY  = "#e2e8f0"
TEXT_SECONDARY= "#94a3b8"
TEXT_DIM      = "#64748b"
DANGER        = "#ef4444"
DANGER_HOVER  = "#f87171"
DANGER_DIM    = "#7f1d1d"
SUCCESS       = "#22c55e"
WARNING       = "#f59e0b"
SIDEBAR_ACTIVE= "#162436"

def load_stylesheet():
    import os
    qss_path = os.path.join(os.path.dirname(__file__), "theme.qss")
    with open(qss_path, "r", encoding="utf-8") as f:
        return f.read()
