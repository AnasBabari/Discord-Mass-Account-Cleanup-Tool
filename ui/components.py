from PyQt5.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel, QWidget, QPushButton, QGraphicsDropShadowEffect, QGraphicsOpacityEffect
from PyQt5.QtCore import QSize, Qt, QTimer, QPoint, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import QColor, QCursor
import qtawesome as qta
from ui.theme import *

def get_length_str(snowflake_id, fallback_timestamp=None):
    from datetime import datetime
    dt = None
    if fallback_timestamp:
        try:
            dt = datetime.fromisoformat(fallback_timestamp.replace('Z', '+00:00'))
        except (ValueError, TypeError, OverflowError):
            pass
    if not dt and snowflake_id and str(snowflake_id).isdigit():
        ms = (int(snowflake_id) >> 22) + 1420070400000
        dt = datetime.fromtimestamp(ms / 1000.0)
    if not dt:
        return "Unknown"
    # Normalize to naive datetime to avoid aware/naive comparison crash
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    now = datetime.now()
    diff = now - dt
    days = diff.days
    if days < 0: return "0 days"
    if days < 30: return f"{days} days"
    if days < 365: return f"{days // 30} months"
    years = days // 365
    return f"{years} year{'s' if years > 1 else ''}"

class LoadingOverlay(QWidget):
    """A full-area loading splash with animated spinner and status text."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"background: transparent;")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.setSpacing(20)

        # Spinner icon — uses opacity animation to pulse
        self.spinner_label = QLabel()
        self.spinner_label.setFixedSize(48, 48)
        self.spinner_label.setAlignment(Qt.AlignCenter)
        self.spinner_label.setPixmap(
            qta.icon('mdi.loading', color=ACCENT, animation=qta.Spin(self.spinner_label)).pixmap(QSize(40, 40))
        )
        layout.addWidget(self.spinner_label, 0, Qt.AlignCenter)

        self.status_label = QLabel("Loading...")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet(f"""
            color: {TEXT_SECONDARY};
            font-size: 14px;
            font-weight: 600;
            background: transparent;
        """)
        layout.addWidget(self.status_label, 0, Qt.AlignCenter)

        self.detail_label = QLabel("")
        self.detail_label.setAlignment(Qt.AlignCenter)
        self.detail_label.setStyleSheet(f"""
            color: {TEXT_DIM};
            font-size: 12px;
            font-weight: 400;
            background: transparent;
        """)
        layout.addWidget(self.detail_label, 0, Qt.AlignCenter)

    def set_status(self, text):
        self.status_label.setText(text)

    def set_detail(self, text):
        self.detail_label.setText(text)


class ToastOverlay(QFrame):
    """A slide-in notification toast with auto-dismiss."""
    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setStyleSheet(f"""
            QFrame#Toast {{
                background-color: {BG_CARD};
                border: 1px solid {BORDER};
                border-radius: 12px;
                padding: 0px;
            }}
        """)
        self.setFixedWidth(320)
        self.hide()
        self.queue = []
        self.is_showing = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        self.icon_label = QLabel()
        self.icon_label.setFixedSize(24, 24)
        layout.addWidget(self.icon_label)

        self.text_label = QLabel()
        self.text_label.setWordWrap(True)
        self.text_label.setStyleSheet(f"color: {TEXT_PRIMARY}; font-size: 13px; font-weight: 500;")
        layout.addWidget(self.text_label, 1)


        self.close_btn = QPushButton()
        self.close_btn.setIcon(qta.icon('fa5s.times', color=TEXT_DIM))
        self.close_btn.setIconSize(QSize(12, 12))
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setStyleSheet("background: transparent; border: none; border-radius: 4px;")
        self.close_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self.close_btn.clicked.connect(self._fade_out)
        layout.addWidget(self.close_btn)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 100))
        shadow.setOffset(0, 8)
        self.setGraphicsEffect(shadow)

        self.dismiss_timer = QTimer()
        self.dismiss_timer.setSingleShot(True)
        self.dismiss_timer.timeout.connect(self._fade_out)

        self.opacity_effect = QGraphicsOpacityEffect(self)

    def show_message(self, message, duration=3500, msg_type="info"):
        self.queue.append((message, duration, msg_type))
        if not self.is_showing:
            self._process_queue()

    def _process_queue(self):
        if not self.queue:
            self.is_showing = False
            return
            
        self.is_showing = True
        message, duration, msg_type = self.queue.pop(0)
        
        icon_map = {
            "info":    ("fa5s.check-circle", SUCCESS),
            "error":   ("fa5s.exclamation-circle", DANGER),
            "warning": ("fa5s.exclamation-triangle", WARNING),
            "success": ("fa5s.check-circle", SUCCESS),
        }
        icon_name, icon_color = icon_map.get(msg_type, icon_map["info"])
        self.icon_label.setPixmap(
            qta.icon(icon_name, color=icon_color).pixmap(QSize(20, 20))
        )
        self.text_label.setText(message)
        self.adjustSize()

        parent_rect = self.parent().rect()
        target_x = parent_rect.width() - self.width() - 24
        target_y = 48  # Shifted down slightly

        self.move(parent_rect.width() + 10, target_y)
        self.show()
        self.raise_()

        self.slide_anim = QPropertyAnimation(self, b"pos")
        self.slide_anim.setDuration(350)
        self.slide_anim.setStartValue(QPoint(parent_rect.width() + 10, target_y))
        self.slide_anim.setEndValue(QPoint(target_x, target_y))
        self.slide_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.slide_anim.start()

        self.dismiss_timer.start(duration)

    def _fade_out(self):
        self.dismiss_timer.stop()
        parent_rect = self.parent().rect()
        self.slide_out_anim = QPropertyAnimation(self, b"pos")
        self.slide_out_anim.setDuration(300)
        self.slide_out_anim.setStartValue(self.pos())
        self.slide_out_anim.setEndValue(QPoint(parent_rect.width() + 10, self.pos().y()))
        self.slide_out_anim.setEasingCurve(QEasingCurve.InCubic)
        self.slide_out_anim.finished.connect(self._on_fade_out_finished)
        self.slide_out_anim.start()
        
    def _on_fade_out_finished(self):
        self.hide()
        self._process_queue()

class SectionHeader(QWidget):
    """A reusable page section header with icon and title."""
    def __init__(self, icon_name, title, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 16)
        layout.setSpacing(10)

        icon_lbl = QLabel()
        icon_lbl.setPixmap(qta.icon(icon_name, color=ACCENT).pixmap(QSize(22, 22)))
        icon_lbl.setFixedSize(28, 28)
        icon_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(icon_lbl)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"""
            font-size: 20px;
            font-weight: 700;
            color: {TEXT_PRIMARY};
            letter-spacing: -0.3px;
        """)
        layout.addWidget(title_lbl)
        layout.addStretch()

class StatBadge(QFrame):
    """A small badge displaying a stat like 'Selected: 3 / 10'."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: rgba(56, 189, 248, 0.08);
                border: 1px solid rgba(56, 189, 248, 0.15);
                border-radius: 8px;
                padding: 0px;
            }}
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(4)

        self.label = QLabel("Selected: 0 / 0")
        self.label.setStyleSheet(f"color: {ACCENT}; font-weight: 600; font-size: 12px; border: none; background: transparent;")
        layout.addWidget(self.label)

    def setText(self, text):
        self.label.setText(text)
