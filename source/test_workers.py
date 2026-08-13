import pytest
from unittest.mock import patch, MagicMock
from PyQt5.QtCore import QThread
import discord_mass_cleanup as dmc
from workers import (
    LoginWorker, FetchServersWorker, FetchFriendsWorker, RemoveFriendsWorker,
    BlockUsersWorker, FetchBlockedWorker, UnblockUsersWorker, LeaveServersWorker, ReadNotifsWorker,
    BatchActionWorker
)

def test_sanitize_token():
    p1 = "N" * 24
    p2 = "A" * 6
    p3 = "B" * 27
    standard_token = f"{p1}.{p2}.{p3}"
    mfa_token = "mfa." + ("C" * 72)
    
    text = f"Logged in with token {standard_token} and mfa {mfa_token}"
    sanitized = dmc.sanitize_token(text)
    assert standard_token not in sanitized
    assert mfa_token not in sanitized
    assert "[REDACTED_TOKEN]" in sanitized

    json_str = '{"Authorization": "some_raw_token_value", "data": 123}'
    assert '"Authorization": "[REDACTED_TOKEN]"' in dmc.sanitize_token(json_str)

    empty_sanitized = dmc.sanitize_token("")
    assert empty_sanitized == ""


def test_login_worker_success_and_avatar(qtbot):
    worker = LoginWorker("fake_token", save=False)
    
    with patch("discord_mass_cleanup._make_api_request") as mock_req, \
         patch.object(dmc.HTTP_TRANSPORT, "get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "global_name": "TestUser",
            "username": "testuser",
            "id": "1234567890",
            "avatar": "abcdef123456"
        }
        mock_req.return_value = mock_resp

        mock_avatar_resp = MagicMock()
        mock_avatar_resp.status_code = 200
        mock_avatar_resp.content = b"fake_png_bytes"
        mock_get.return_value = mock_avatar_resp
        
        with qtbot.waitSignal(worker.result_signal, timeout=1000) as blocker:
            worker.start()
            
        success, message, username, token, avatar_bytes, save = blocker.args
        assert success is True
        assert message == "TestUser"
        assert username == "testuser"
        assert token == "fake_token"
        assert avatar_bytes == b"fake_png_bytes"
        assert save is False


def test_login_worker_invalid_token(qtbot):
    worker = LoginWorker("bad_token", save=False)
    with patch("discord_mass_cleanup._make_api_request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_req.return_value = mock_resp

        with qtbot.waitSignal(worker.result_signal, timeout=1000) as blocker:
            worker.start()

        success, message, username, token, avatar_bytes, save = blocker.args
        assert success is False
        assert message == "INVALID TOKEN"


def test_login_worker_empty_token(qtbot):
    worker = LoginWorker("", save=False)
    with qtbot.waitSignal(worker.result_signal, timeout=1000) as blocker:
        worker.start()
    success, message, username, token, avatar_bytes, save = blocker.args
    assert success is False
    assert message == "No token provided"


def test_fetch_servers_worker(qtbot):
    worker = FetchServersWorker("fake_token")
    with patch("discord_mass_cleanup.get_guilds", return_value=[{"id": "1", "name": "Guild"}]) as mock_get:
        with qtbot.waitSignal(worker.result_signal, timeout=1000) as blocker:
            worker.start()
        guilds, err = blocker.args
        assert len(guilds) == 1
        assert err == ""

    # Empty token test
    worker_empty = FetchServersWorker("")
    with qtbot.waitSignal(worker_empty.result_signal, timeout=1000) as blocker:
        worker_empty.start()
    guilds, err = blocker.args
    assert guilds == []
    assert err == "No token provided"


def test_fetch_friends_worker(qtbot):
    worker = FetchFriendsWorker("fake_token")
    with patch("discord_mass_cleanup.get_friends", return_value=[{"id": "1", "user": {}}]) as mock_get:
        with qtbot.waitSignal(worker.result_signal, timeout=1000) as blocker:
            worker.start()
        friends, err = blocker.args
        assert len(friends) == 1
        assert err == ""


def test_fetch_blocked_worker(qtbot):
    worker = FetchBlockedWorker("fake_token")
    with patch("discord_mass_cleanup.get_blocked_users", return_value=[{"id": "1", "user": {}}]) as mock_get:
        with qtbot.waitSignal(worker.result_signal, timeout=1000) as blocker:
            worker.start()
        blocked, err = blocker.args
        assert len(blocked) == 1
        assert err == ""


def test_remove_friends_worker_success_and_cancel(qtbot):
    friends_to_remove = [
        {"id": "1", "user": {"global_name": "User 1"}},
        {"id": "2", "user": {"global_name": "User 2"}},
        {"id": "3", "user": {"global_name": "User 3"}}
    ]
    worker = RemoveFriendsWorker("fake_token", friends_to_remove)
    
    with patch("discord_mass_cleanup.remove_friend") as mock_remove:
        mock_remove.side_effect = lambda *args, **kwargs: (worker.cancel() or (204, ""))
        with qtbot.waitSignal(worker.finished_signal, timeout=1000) as blocker:
            worker.start()

        success, failed = blocker.args
        assert success == 1
        assert failed == 0
        assert mock_remove.call_count == 1
        assert worker.token == ""


def test_block_users_worker(qtbot):
    users_to_block = [
        {"id": "1", "user": {"global_name": "User 1"}},
        {"id": "2", "user": {"username": "User 2"}}
    ]
    worker = BlockUsersWorker("fake_token", users_to_block)
    with patch("discord_mass_cleanup.block_user", side_effect=[(204, ""), (400, "Bad Request")]):
        with qtbot.waitSignal(worker.finished_signal, timeout=1000) as blocker:
            worker.start()
        success, failed = blocker.args
        assert success == 1
        assert failed == 1


def test_unblock_users_worker(qtbot):
    users_to_unblock = [
        {"id": "1", "name": "User 1"},
        {"id": "2", "name": "User 2"}
    ]
    worker = UnblockUsersWorker("fake_token", users_to_unblock)
    with patch("discord_mass_cleanup.unblock_user", return_value=(204, "")):
        with qtbot.waitSignal(worker.finished_signal, timeout=1000) as blocker:
            worker.start()
        success, failed = blocker.args
        assert success == 2
        assert failed == 0


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
            assert mock_leave.call_count == 1


def test_read_notifs_worker_success(qtbot):
    worker = ReadNotifsWorker("fake_token")
    mock_grouped = {
        "Server1": ["101", "102"],
        "Direct Messages": ["201"]
    }
    with patch("discord_mass_cleanup._get_read_states", return_value=mock_grouped), \
         patch("discord_mass_cleanup._make_api_request") as mock_req:
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_req.return_value = mock_resp
        
        progress_chunks = []
        worker.chunk_progress_signal.connect(lambda cur, tot: progress_chunks.append((cur, tot)))

        with qtbot.waitSignal(worker.finished_signal, timeout=1000) as blocker:
            worker.start()

        success, failed, err = blocker.args
        assert success == 3
        assert failed == 0
        assert err == ""
        assert len(progress_chunks) == 2
        assert progress_chunks[0] == (1, 2)
        assert progress_chunks[1] == (2, 2)


def test_read_notifs_worker_empty(qtbot):
    worker = ReadNotifsWorker("fake_token")
    with patch("discord_mass_cleanup._get_read_states", return_value={}):
        with qtbot.waitSignal(worker.finished_signal, timeout=1000) as blocker:
            worker.start()
        success, failed, err = blocker.args
        assert success == 0
        assert failed == 0
        assert "No unread channels found" in err
