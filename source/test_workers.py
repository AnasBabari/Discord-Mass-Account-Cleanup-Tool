import pytest
from unittest.mock import patch, MagicMock
from PyQt5.QtCore import QThread
from workers import (
    LoginWorker, FetchServersWorker, FetchFriendsWorker, RemoveFriendsWorker,
    BlockUsersWorker, FetchBlockedWorker, UnblockUsersWorker, LeaveServersWorker, ReadNotifsWorker
)

def test_login_worker_success(qtbot):
    worker = LoginWorker("fake_token", save=False)
    
    with patch("discord_mass_cleanup._make_api_request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "global_name": "TestUser",
            "username": "testuser",
            "id": "123",
            "avatar": None
        }
        mock_req.return_value = mock_resp
        
        with qtbot.waitSignal(worker.result_signal, timeout=1000) as blocker:
            worker.start()
            
        success, message, username, token, avatar_bytes, save = blocker.args
        assert success is True
        assert message == "TestUser"
        assert token == "fake_token"

def test_remove_friends_worker_success_and_cancel(qtbot):
    friends_to_remove = [
        {"id": "1", "user": {"global_name": "User 1"}},
        {"id": "2", "user": {"global_name": "User 2"}},
        {"id": "3", "user": {"global_name": "User 3"}}
    ]
    worker = RemoveFriendsWorker("fake_token", friends_to_remove)
    
    with patch("discord_mass_cleanup.remove_friend") as mock_remove:
        # User 1 succeeds, then it cancels during time.sleep
        mock_remove.side_effect = [
            (204, "")
        ]
        
        # We will manually cancel after 1 iteration to test cancellation
        with patch("time.sleep", side_effect=lambda x: worker.cancel()):
            with qtbot.waitSignal(worker.finished_signal, timeout=1000) as blocker:
                worker.start()
                
            success, failed = blocker.args
            assert success == 1
            assert failed == 0
            assert mock_remove.call_count == 1

def test_leave_servers_worker_cloudflare_ban(qtbot):
    servers_to_leave = [{"id": "1", "name": "Server 1"}, {"id": "2", "name": "Server 2"}]
    worker = LeaveServersWorker("fake_token", servers_to_leave)
    
    with patch("discord_mass_cleanup.leave_guild") as mock_leave:
        mock_leave.side_effect = Exception("Cloudflare IP Ban: 1015")
        
        with patch("time.sleep"):
            with qtbot.waitSignal(worker.finished_signal, timeout=1000) as blocker:
                worker.start()
                
            success, failed = blocker.args
            assert success == 0
            assert failed == 1
            assert mock_leave.call_count == 1 # Should abort immediately after first Exception
