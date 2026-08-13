BG_DARKEST   = "#070b12"
BG_DARK      = "#0b111a"
BG_CARD      = "#101924"
BG_CARD_HOVER= "#152232"
BG_SIDEBAR   = "#090e15"
BG_INPUT     = "#0c141f"
BORDER       = "#1e2d3d"
BORDER_SUBTLE= "rgba(255, 255, 255, 0.08)"
BORDER_FOCUS = "#38bdf8"
ACCENT       = "#38bdf8"
ACCENT_HOVER = "#7dd3fc"
ACCENT_DIM   = "#0369a1"
ACCENT_GLOW  = "rgba(56, 189, 248, 0.18)"
TEXT_PRIMARY  = "#f8fafc"
TEXT_SECONDARY= "#94a3b8"
TEXT_DIM      = "#64748b"
DANGER        = "#ef4444"
DANGER_HOVER  = "#f87171"
DANGER_DIM    = "#7f1d1d"
DANGER_SURFACE= "rgba(239, 68, 68, 0.12)"
SUCCESS       = "#10b981"
SUCCESS_SURFACE= "rgba(16, 185, 129, 0.12)"
WARNING       = "#f59e0b"
WARNING_SURFACE= "rgba(245, 158, 11, 0.12)"
SIDEBAR_ACTIVE= "#142233"

def load_stylesheet():
    import os, sys
    if hasattr(sys, '_MEIPASS'):
        base_dir = getattr(sys, '_MEIPASS')
    else:
        base_dir = os.path.dirname(os.path.dirname(__file__))
    assets_dir = os.path.join(base_dir, 'assets').replace('\\', '/')
    
    qss_path = os.path.join(os.path.dirname(__file__), "theme.qss")
    with open(qss_path, "r", encoding="utf-8") as f:
        qss = f.read()
    return qss.replace("ASSETS_DIR", assets_dir)
