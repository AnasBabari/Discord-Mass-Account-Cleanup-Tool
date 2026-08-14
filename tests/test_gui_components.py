from discord_cleanup.ui.components import (
    GlassCard,
    LoadingOverlay,
    SectionHeader,
    StatBadge,
    StatCard,
    ToastOverlay,
    get_length_str,
)


class TestUiComponents:
    def test_get_length_str_snowflake(self):
        # 175928847299117063 is Discord epoch snowflake from ~2016
        length = get_length_str("175928847299117063")
        assert "year" in length or "month" in length

    def test_get_length_str_iso_fallback(self):
        length = get_length_str(None, fallback_timestamp="2020-01-01T00:00:00Z")
        assert "year" in length

    def test_get_length_str_invalid(self):
        assert get_length_str(None, None) == "Unknown"
        assert get_length_str("not_a_snowflake", None) == "Unknown"

    def test_glass_card_widget(self, qapp, qtbot):
        card = GlassCard()
        qtbot.addWidget(card)
        assert card.graphicsEffect() is not None

    def test_stat_card_widget(self, qapp, qtbot):
        stat = StatCard(title="Active Servers", initial_val="42", icon_name="fa5s.server")
        qtbot.addWidget(stat)
        assert stat.title_lbl.text() == "ACTIVE SERVERS"
        assert stat.val_lbl.text() == "42"

        stat.set_value("100")
        assert stat.val_lbl.text() == "100"

    def test_stat_badge_widget(self, qapp, qtbot):
        badge = StatBadge()
        qtbot.addWidget(badge)
        assert badge.label.text() == "Selected: 0 / 0"
        badge.setText("Selected: 5 / 10")
        assert badge.label.text() == "Selected: 5 / 10"

    def test_loading_overlay(self, qapp, qtbot):
        overlay = LoadingOverlay()
        qtbot.addWidget(overlay)
        overlay.set_status("Loading servers...")
        overlay.set_detail("Please wait")
        assert overlay.status_label.text() == "Loading servers..."
        assert overlay.detail_label.text() == "Please wait"

    def test_section_header(self, qapp, qtbot):
        header = SectionHeader("fa5s.server", "Server Management")
        qtbot.addWidget(header)
        assert header.layout().count() >= 2

    def test_toast_overlay_messages(self, qapp, qtbot):
        parent = GlassCard()
        parent.resize(800, 600)
        qtbot.addWidget(parent)

        toast = ToastOverlay(parent)
        toast.show_message("Operation complete!", duration=1000, msg_type="success")
        assert toast.text_label.text() == "Operation complete!"
        assert toast.is_showing is True

        toast.show_message("Second notice", duration=1000, msg_type="info")
        assert len(toast.queue) == 1

        toast._fade_out()
        toast._on_fade_out_finished()
        assert toast.text_label.text() == "Second notice"
