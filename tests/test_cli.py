from unittest.mock import MagicMock, patch

from discord_cleanup.api.exceptions import NetworkError
from discord_cleanup.cli.main import (
    get_masked_input,
    parse_selection,
    prompt_preview_and_confirmation,
    run_blocked_cleanup,
    run_friends_cleanup,
    run_notifications_cleanup,
    run_servers_cleanup,
)
from discord_cleanup.models.domain import Guild, OperationPreview, Relationship, RelationshipType, User


class TestCliSelectionParser:
    def test_parse_individual_numbers(self):
        assert parse_selection("1, 3, 5", 5) == [0, 2, 4]
        assert parse_selection("2 4", 5) == [1, 3]

    def test_parse_hyphenated_ranges(self):
        assert parse_selection("1-3", 5) == [0, 1, 2]
        assert parse_selection("3-1", 5) == [0, 1, 2]
        assert parse_selection("2-4, 5", 5) == [1, 2, 3, 4]

    def test_parse_all(self):
        assert parse_selection("all", 3) == [0, 1, 2]
        assert parse_selection("ALL", 0) == []

    def test_parse_invalid_and_out_of_bounds(self):
        assert parse_selection("0, 6, abc, -", 5) == []
        assert parse_selection("", 5) == []


class TestCliPreviewAndConfirmation:
    def test_prompt_confirmation_yes(self):
        preview = OperationPreview(action_name="Leave Servers", target_count=2, target_descriptions=["S1", "S2"])
        with patch("builtins.input", return_value="yes"):
            assert prompt_preview_and_confirmation(preview) is True

    def test_prompt_confirmation_no(self):
        preview = OperationPreview(action_name="Leave Servers", target_count=2, target_descriptions=["S1", "S2"])
        with patch("builtins.input", return_value="no"):
            assert prompt_preview_and_confirmation(preview) is False

    def test_prompt_confirmation_large_target_preview(self):
        preview = OperationPreview(
            action_name="Leave Servers",
            target_count=12,
            target_descriptions=[f"Server {i}" for i in range(12)],
        )
        with patch("builtins.input", return_value="yes"):
            assert prompt_preview_and_confirmation(preview) is True


class TestCliOperations:
    def test_run_servers_cleanup_flow(self):
        mock_client = MagicMock()
        mock_client.get_guilds.return_value = [
            Guild(id="1", name="Guild 1", owner=False),
            Guild(id="2", name="Owned Guild", owner=True),
        ]
        mock_client.leave_guild.return_value = (204, "")

        with patch("builtins.input", side_effect=["1", "yes"]):
            result = run_servers_cleanup(mock_client)
            assert result.success_count == 1
            assert result.failure_count == 0
            mock_client.leave_guild.assert_called_once_with("1")

    def test_run_servers_cleanup_cancel_choice(self):
        mock_client = MagicMock()
        mock_client.get_guilds.return_value = [Guild(id="1", name="Guild 1", owner=False)]
        with patch("builtins.input", return_value="q"):
            result = run_servers_cleanup(mock_client)
            assert result.cancelled is True

    def test_run_servers_cleanup_fetch_error(self):
        mock_client = MagicMock()
        mock_client.get_guilds.side_effect = NetworkError("Network timeout")
        result = run_servers_cleanup(mock_client)
        assert len(result.errors) == 1

    def test_run_friends_cleanup_flow(self):
        mock_client = MagicMock()
        mock_client.get_friends.return_value = [
            Relationship(id="f1", user=User(id="f1", username="Alice"), rel_type=RelationshipType.FRIEND),
        ]
        mock_client.remove_friend.return_value = (204, "")

        with patch("builtins.input", side_effect=["1", "yes"]):
            result = run_friends_cleanup(mock_client)
            assert result.success_count == 1
            mock_client.remove_friend.assert_called_once_with("f1")

    def test_run_friends_cleanup_empty(self):
        mock_client = MagicMock()
        mock_client.get_friends.return_value = []
        result = run_friends_cleanup(mock_client)
        assert result.total_processed == 0

    def test_run_blocked_cleanup_flow(self):
        mock_client = MagicMock()
        mock_client.get_blocked_users.return_value = [
            Relationship(id="b1", user=User(id="b1", username="Spammer"), rel_type=RelationshipType.BLOCKED),
        ]
        mock_client.unblock_user.return_value = (204, "")

        with patch("builtins.input", side_effect=["1", "yes"]):
            result = run_blocked_cleanup(mock_client)
            assert result.success_count == 1
            mock_client.unblock_user.assert_called_once_with("b1")

    def test_run_blocked_cleanup_empty(self):
        mock_client = MagicMock()
        mock_client.get_blocked_users.return_value = []
        result = run_blocked_cleanup(mock_client)
        assert result.total_processed == 0

    def test_run_notifications_cleanup_flow(self):
        mock_client = MagicMock()
        mock_client.token = "test_token"
        mock_client.ack_read_states_chunk.return_value = (200, "")
        mock_gateway = MagicMock()
        mock_gateway.fetch_unread_channels.return_value = {"Server A": ["c1", "c2"]}

        with patch("builtins.input", return_value="yes"):
            result = run_notifications_cleanup(mock_client, mock_gateway)
            assert result.success_count == 2
            mock_client.ack_read_states_chunk.assert_called_once()

    def test_run_notifications_cleanup_empty(self):
        mock_client = MagicMock()
        mock_client.token = "test_token"
        mock_gateway = MagicMock()
        mock_gateway.fetch_unread_channels.return_value = {}
        result = run_notifications_cleanup(mock_client, mock_gateway)
        assert result.total_processed == 0

    def test_get_masked_input_windows(self):
        with patch("msvcrt.getwch", side_effect=["s", "e", "c", "r", "e", "t", chr(13)]):
            assert get_masked_input("Password: ") == "secret"

    def test_get_masked_input_fallback(self):
        with patch("sys.platform", "linux"), patch("getpass.getpass", return_value="secret_pass"):
            assert get_masked_input("Password: ") == "secret_pass"
