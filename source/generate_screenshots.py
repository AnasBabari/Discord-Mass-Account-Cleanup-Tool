import sys
import os
import time
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QPixmap, QIcon
import qtawesome as qta
from gui_app import MainWindow
from ui.theme import ACCENT, BG_DARKEST

def generate_screenshots():
    # Ensure offscreen rendering or proper DPI scaling
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
    
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow()
    win.resize(1100, 750)
    win.show()
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()

    # Simulate logged-in account state
    win.token = "demo_token"
    win.set_authenticated(True)
    win.account_name = "DemoUser"
    win.account_name_label.setText("DemoUser")
    
    # Set a nice avatar
    avatar_pix = qta.icon('fa5s.user', color=BG_DARKEST).pixmap(QSize(24, 24))
    win.account_avatar.setPixmap(avatar_pix)

    assets_dir = os.path.join(os.path.dirname(__file__), "assets")
    os.makedirs(assets_dir, exist_ok=True)

    # ── 1. Servers Page Screenshot ──────────────────────────────────────────
    win.switch_page("servers")
    win.servers_page.servers_data = [
        {"id": "847291038472910293", "name": "Community Lounge", "owner": False},
        {"id": "736182940192837465", "name": "Gaming Hangout", "owner": False},
        {"id": "625193847261948271", "name": "Open Source Developers", "owner": False},
        {"id": "514082736152837460", "name": "Anime & Manga Club", "owner": False},
        {"id": "403971625041726359", "name": "Music & Audio Production", "owner": False},
        {"id": "392860514930615248", "name": "Tech News & Discussion", "owner": False},
        {"id": "281759403829504137", "name": "Study & Productivity", "owner": False},
    ]
    win.servers_page.populate_table()
    win.servers_page.servers_table.item(0, 0).setCheckState(Qt.Checked)
    win.servers_page.servers_table.item(2, 0).setCheckState(Qt.Checked)
    win.servers_page.update_status()
    win.servers_page.table_stack.setCurrentIndex(0)
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()

    servers_path = os.path.join(assets_dir, "servers.png")
    win.grab().save(servers_path, "PNG")
    print(f"[✓] Saved {servers_path}")

    # ── 2. Friends Page Screenshot ──────────────────────────────────────────
    win.switch_page("friends")
    win.friends_page.friends_data = [
        {"id": "192837465019283746", "user": {"global_name": "Sarah Connor", "username": "sarah_c"}, "since": "2022-04-15T14:20:00Z"},
        {"id": "283746501928374650", "user": {"global_name": "Neo Anderson", "username": "the_one"}, "since": "2021-11-03T09:12:00Z"},
        {"id": "374650192837465019", "user": {"global_name": "Gordon Freeman", "username": "gfreeman"}, "since": "2023-01-20T18:45:00Z"},
        {"id": "465019283746501928", "user": {"global_name": "Ellen Ripley", "username": "ripley_lv426"}, "since": "2020-08-10T12:00:00Z"},
        {"id": "556019283746501928", "user": {"global_name": "Arthur Dent", "username": "adent42"}, "since": "2023-06-25T16:30:00Z"},
        {"id": "647019283746501928", "user": {"global_name": "Ada Lovelace", "username": "adalove"}, "since": "2019-12-01T10:00:00Z"},
    ]
    win.friends_page.populate_table()
    win.friends_page.friends_table.item(0, 0).setCheckState(Qt.Checked)
    win.friends_page.friends_table.item(1, 0).setCheckState(Qt.Checked)
    win.friends_page.update_status()
    win.friends_page.table_stack.setCurrentIndex(0)
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()

    friends_path = os.path.join(assets_dir, "friends.png")
    win.grab().save(friends_path, "PNG")
    print(f"[✓] Saved {friends_path}")

    # ── 3. Blocked Page Screenshot ──────────────────────────────────────────
    win.switch_page("blocked")
    win.blocked_page.blocked_data = [
        {"id": "112233445566778899", "user": {"global_name": "Crypto Spammer 3000", "username": "free_crypto_now"}},
        {"id": "223344556677889900", "user": {"global_name": "Nitro Giveaway Bot", "username": "nitro_bot_claim"}},
        {"id": "334455667788990011", "user": {"global_name": "Steam Gift Card DM", "username": "steam_giftcards_dm"}},
        {"id": "445566778899001122", "user": {"global_name": "Phishing Bot Support", "username": "discord_mod_support"}},
    ]
    win.blocked_page.populate_table()
    win.blocked_page.blocked_table.item(0, 0).setCheckState(Qt.Checked)
    win.blocked_page.blocked_table.item(1, 0).setCheckState(Qt.Checked)
    win.blocked_page.update_status()
    win.blocked_page.table_stack.setCurrentIndex(0)
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()

    blocked_path = os.path.join(assets_dir, "blocked.png")
    win.grab().save(blocked_path, "PNG")
    print(f"[✓] Saved {blocked_path}")

    # ── 4. Notifications Page Screenshot ────────────────────────────────────
    win.switch_page("notifications")
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()

    notifs_path = os.path.join(assets_dir, "notifications.png")
    win.grab().save(notifs_path, "PNG")
    print(f"[✓] Saved {notifs_path}")

    win.close()
    print("All screenshots generated successfully!")

if __name__ == "__main__":
    generate_screenshots()
