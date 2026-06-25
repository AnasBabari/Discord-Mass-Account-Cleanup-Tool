import sys
import pytest
from PyQt5.QtWidgets import QApplication
from gui_app import MainWindow
from ui.pages.login import LoginPage
from ui.pages.servers import ServersPage
from ui.pages.friends import FriendsPage
from ui.pages.notifications import NotificationsPage
from ui.pages.logs import LogsPage
from workers import LoginWorker

@pytest.fixture
def app(qtbot):
    main_window = MainWindow()
    qtbot.addWidget(main_window)
    return main_window

def test_main_window_initialization(app):
    assert app.windowTitle() == "Discord Mass Account Cleanup Tool"
    assert app.pages.count() == 5
    
def test_page_switching(app, qtbot):
    # Default is login
    assert app.pages.currentIndex() == 0
    
    # Try switching page through function
    app.switch_page("servers")
    assert app.pages.currentIndex() == 1
    
    app.switch_page("friends")
    assert app.pages.currentIndex() == 2

    app.switch_page("notifications")
    assert app.pages.currentIndex() == 3

def test_login_page(qtbot):
    page = LoginPage()
    qtbot.addWidget(page)
    
    # Check if UI elements exist
    assert page.token_entry is not None
    assert page.login_btn is not None
    
    # Simulate entering text
    qtbot.keyClicks(page.token_entry, "fake_token")
    assert page.token_entry.text() == "fake_token"

def test_servers_page(qtbot):
    page = ServersPage()
    qtbot.addWidget(page)
    
    # Initial table state
    assert page.servers_table.rowCount() == 0
    
    # Populate dummy data
    page.servers_data = [{"id": "123", "name": "Test Server", "joined_at": None}]
    page.populate_table()
    
    assert page.servers_table.rowCount() == 1
    assert page.servers_table.item(0, 1).text() == "Test Server"
