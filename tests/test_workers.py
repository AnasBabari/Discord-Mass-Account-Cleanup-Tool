from __future__ import annotations

from unittest.mock import MagicMock, patch

from discord_cleanup.api.exceptions import AuthenticationError
from discord_cleanup.models.domain import Guild, Relationship, RelationshipType, User
from discord_cleanup.workers.batch import (
    LeaveServersWorker,
    RemoveFriendsWorker,
)
from discord_cleanup.workers.fetch import (
    FetchBlockedWorker,
    FetchFriendsWorker,
    FetchServersWorker,
)
from discord_cleanup.workers.login import LoginWorker
from discord_cleanup.workers.notifications import ReadNotifsWorker


class TestWorkers:
    def test_login_worker_success(self, qapp, qtbot):
        mock_client = MagicMock()
        mock_client.verify_token.return_value = User(
            id="123", username="testuser", global_name="Test User", avatar="abc"
        )
        mock_resp = MagicMock(status_code=200, content=b"avatar_bytes")

        with patch("requests.get", return_value=mock_resp):
            worker = LoginWorker(token="valid_token", client=mock_client)
            with qtbot.waitSignal(worker.result_signal, timeout=3000) as blocker:
                worker.start()

        success, name, username, token, avatar_bytes, save = blocker.args
        assert success is True
        assert name == "Test User"
        assert username == "testuser"
        assert avatar_bytes == b"avatar_bytes"
        assert worker.token == ""  # token scrubbed after completion

    def test_login_worker_invalid_token(self, qapp, qtbot):
        mock_client = MagicMock()
        mock_client.verify_token.side_effect = AuthenticationError("Invalid Token")

        worker = LoginWorker(token="invalid_token", client=mock_client)
        with qtbot.waitSignal(worker.result_signal, timeout=3000) as blocker:
            worker.start()

        success, name, _, _, _, _ = blocker.args
        assert success is False
        assert name == "INVALID TOKEN"

    def test_fetch_servers_worker(self, qapp, qtbot):
        mock_client = MagicMock()
        mock_client.get_guilds.return_value = [Guild(id="1", name="Guild 1", owner=False)]

        worker = FetchServersWorker(token="token", client=mock_client)
        with qtbot.waitSignal(worker.result_signal, timeout=3000) as blocker:
            worker.start()

        guilds, err = blocker.args
        assert len(guilds) == 1
        assert guilds[0]["name"] == "Guild 1"
        assert err == ""

    def test_fetch_friends_worker(self, qapp, qtbot):
        mock_client = MagicMock()
        mock_client.get_friends.return_value = [
            Relationship(id="1", user=User(id="1", username="User 1"), rel_type=RelationshipType.FRIEND),
        ]

        worker = FetchFriendsWorker(token="token", client=mock_client)
        with qtbot.waitSignal(worker.result_signal, timeout=3000) as blocker:
            worker.start()

        friends, err = blocker.args
        assert len(friends) == 1
        assert friends[0]["user"]["username"] == "User 1"
        assert err == ""

    def test_fetch_blocked_worker(self, qapp, qtbot):
        mock_client = MagicMock()
        mock_client.get_blocked_users.return_value = [
            Relationship(id="2", user=User(id="2", username="Blocked 1"), rel_type=RelationshipType.BLOCKED),
        ]

        worker = FetchBlockedWorker(token="token", client=mock_client)
        with qtbot.waitSignal(worker.result_signal, timeout=3000) as blocker:
            worker.start()

        blocked, err = blocker.args
        assert len(blocked) == 1
        assert blocked[0]["user"]["username"] == "Blocked 1"
        assert err == ""

    def test_batch_action_worker_leave_servers(self, qapp, qtbot):
        mock_client = MagicMock()
        mock_client.leave_guild.return_value = (204, "")

        guilds = [{"id": "1", "name": "Server 1"}, {"id": "2", "name": "Server 2"}]
        worker = LeaveServersWorker(token="token", servers_to_leave=guilds, client=mock_client)

        with qtbot.waitSignal(worker.finished_signal, timeout=3000) as blocker:
            worker.start()

        assert blocker.args[0] == 2
        assert mock_client.leave_guild.call_count == 2
        assert worker.token == ""

    def test_batch_action_worker_401_immediate_break(self, qapp, qtbot):
        mock_client = MagicMock()
        mock_client.remove_friend.side_effect = [(204, ""), (401, "Unauthorized")]

        items = [
            {"id": "1", "user": {"username": "User 1"}},
            {"id": "2", "user": {"username": "User 2"}},
            {"id": "3", "user": {"username": "User 3"}},
        ]
        worker = RemoveFriendsWorker(token="token", friends_to_remove=items, client=mock_client)

        with qtbot.waitSignal(worker.finished_signal, timeout=3000) as blocker:
            worker.start()

        assert blocker.args[0] == 1
        assert mock_client.remove_friend.call_count == 2  # Breaks early on 401

    def test_read_notifs_worker(self, qapp, qtbot):
        mock_client = MagicMock()
        mock_client.ack_read_states_chunk.return_value = (200, "")

        mock_gateway = MagicMock()
        mock_gateway.fetch_unread_channels.return_value = {"Server 1": ["c1", "c2"]}

        worker = ReadNotifsWorker(token="token", client=mock_client, gateway=mock_gateway)
        with qtbot.waitSignal(worker.finished_signal, timeout=3000) as blocker:
            worker.start()

        assert blocker.args[0] == 2
        assert worker.token == ""
