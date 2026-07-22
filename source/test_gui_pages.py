import pytest
from unittest.mock import patch
from PyQt5.QtWidgets import QApplication, QMessageBox
from ui.pages.friends import FriendsPage
from ui.pages.servers import ServersPage
from gui_app import MainWindow

@pytest.fixture
def app(qtbot):
    main_window = MainWindow()
    qtbot.addWidget(main_window)
    return main_window

def test_friends_page_remove_selected(qtbot):
    page = FriendsPage()
    qtbot.addWidget(page)
    page.show()
    
    # Setup dummy data
    page.friends_data = [
        {"id": "1", "user": {"username": "User1"}},
        {"id": "2", "user": {"username": "User2"}},
    ]
    page.populate_table()
    
    # Select both
    page.friends_table.item(0, 0).setCheckState(2) # Qt.Checked
    page.friends_table.item(1, 0).setCheckState(2)
    
    # Mock the worker and QMessageBox
    with patch("ui.pages.friends.QMessageBox.question", return_value=QMessageBox.Yes):
        with patch("ui.pages.friends.RemoveFriendsWorker") as MockWorker:
            with qtbot.waitSignal(page.action_finished, timeout=1000) as blocker:
                # Click the remove button
                qtbot.mouseClick(page.remove_friends_btn, 1) # Qt.LeftButton
                
                # Verify UI state locked
                assert not page.remove_friends_btn.isEnabled()
                assert page.friends_progress.isVisible()
                
                # Simulate worker progress
                page.on_remove_progress(1, "[+] REMOVED: User1")
                assert page.friends_progress.value() == 1
                
                # Simulate worker finished
                page.on_remove_finished(2, 0)
            
        # Verify UI unlocked
        assert page.remove_friends_btn.isEnabled()
        assert not page.friends_progress.isVisible()

def test_servers_page_leave_selected(qtbot):
    page = ServersPage()
    qtbot.addWidget(page)
    page.show()
    
    page.servers_data = [
        {"id": "1", "name": "Server1", "owner": False},
        {"id": "2", "name": "Server2", "owner": False}
    ]
    page.populate_table()
    
    page.servers_table.item(0, 0).setCheckState(2)
    page.servers_table.item(1, 0).setCheckState(2)
    
    with patch("ui.pages.servers.QMessageBox.question", return_value=QMessageBox.Yes):
        with patch("ui.pages.servers.LeaveServersWorker") as MockWorker:
            with qtbot.waitSignal(page.action_finished, timeout=1000) as blocker:
                qtbot.mouseClick(page.leave_servers_btn, 1)
                
                assert not page.leave_servers_btn.isEnabled()
                assert page.servers_progress.isVisible()
                
                page.on_leave_progress(1, "[+] LEFT: Server1")
                assert page.servers_progress.value() == 1
                
                page.on_leave_finished(1, 1) # 1 success, 1 fail
            
        assert page.leave_servers_btn.isEnabled()
        assert not page.servers_progress.isVisible()
