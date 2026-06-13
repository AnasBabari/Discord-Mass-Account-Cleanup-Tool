import pytest
import responses
import requests
import json
from unittest.mock import patch, MagicMock

import discord_mass_cleanup as dmc

BASE_URL = "https://discord.com/api/v10"


@pytest.fixture
def mock_responses():
    with responses.RequestsMock() as rsps:
        yield rsps


# --- parse_selection tests ---


def test_parse_selection_valid():
    assert dmc.parse_selection("1, 3, 5-7", 10) == [1, 3, 5, 6, 7]


def test_parse_selection_reverse_range():
    assert dmc.parse_selection("7-5", 10) == [5, 6, 7]


def test_parse_selection_oob():
    assert dmc.parse_selection("1, 15", 10) == [1]


def test_parse_selection_empty():
    assert dmc.parse_selection("", 10) == []
    assert dmc.parse_selection("   ,  ,, ", 10) == []


def test_parse_selection_invalid():
    with pytest.raises(ValueError):
        dmc.parse_selection("abc", 10)
    with pytest.raises(ValueError):
        dmc.parse_selection("1-abc", 10)


# --- get_guilds tests ---


def test_get_guilds_success(mock_responses):
    page1 = [{"id": f"{i}"} for i in range(200)]
    page2 = [{"id": "200"}]

    mock_responses.add(
        responses.GET,
        f"{BASE_URL}/users/@me/guilds",
        json=page1,
        status=200,
        match=[responses.matchers.query_param_matcher({"limit": "200"})],
    )
    mock_responses.add(
        responses.GET,
        f"{BASE_URL}/users/@me/guilds",
        json=page2,
        status=200,
        match=[
            responses.matchers.query_param_matcher({"limit": "200", "after": "199"})
        ],
    )

    guilds = dmc.get_guilds("test_token")
    assert len(guilds) == 201


def test_get_guilds_empty(mock_responses):
    mock_responses.add(
        responses.GET,
        f"{BASE_URL}/users/@me/guilds",
        json=[],
        status=200,
        match=[responses.matchers.query_param_matcher({"limit": "200"})],
    )
    guilds = dmc.get_guilds("test_token")
    assert len(guilds) == 0


def test_get_guilds_401(mock_responses):
    mock_responses.add(responses.GET, f"{BASE_URL}/users/@me/guilds", status=401)
    with pytest.raises(ValueError, match="Invalid token"):
        dmc.get_guilds("bad_token")


@patch("time.sleep", return_value=None)
def test_get_guilds_429(mock_sleep, mock_responses):
    mock_responses.add(
        responses.GET,
        f"{BASE_URL}/users/@me/guilds",
        json={"retry_after": 0.5},
        status=429,
    )
    mock_responses.add(
        responses.GET, f"{BASE_URL}/users/@me/guilds", json=[{"id": "1"}], status=200
    )
    guilds = dmc.get_guilds("test_token")
    assert len(guilds) == 1
    mock_sleep.assert_called_once_with(0.5)


@patch("time.sleep", return_value=None)
def test_get_guilds_429_html(mock_sleep, mock_responses):
    # Testing Cloudflare HTML 429
    mock_responses.add(
        responses.GET,
        f"{BASE_URL}/users/@me/guilds",
        body="<html>Cloudflare Rate Limit</html>",
        status=429,
    )
    mock_responses.add(
        responses.GET, f"{BASE_URL}/users/@me/guilds", json=[{"id": "1"}], status=200
    )
    guilds = dmc.get_guilds("test_token")
    assert len(guilds) == 1


def test_get_guilds_http_error(mock_responses):
    mock_responses.add(responses.GET, f"{BASE_URL}/users/@me/guilds", status=500)
    with pytest.raises(requests.RequestException):
        dmc.get_guilds("test_token")


# --- leave_guild tests ---


def test_leave_guild_success(mock_responses):
    mock_responses.add(
        responses.DELETE, f"{BASE_URL}/users/@me/guilds/123", body="", status=204
    )
    assert dmc.leave_guild("token", "123") == (204, "")


@patch("time.sleep", return_value=None)
def test_leave_guild_429(mock_sleep, mock_responses):
    mock_responses.add(
        responses.DELETE,
        f"{BASE_URL}/users/@me/guilds/123",
        json={"retry_after": 0.5},
        status=429,
    )
    mock_responses.add(
        responses.DELETE, f"{BASE_URL}/users/@me/guilds/123", body="", status=204
    )
    assert dmc.leave_guild("token", "123") == (204, "")
    mock_sleep.assert_called_once_with(0.5)


# --- get_friends tests ---


def test_get_friends_success(mock_responses):
    mock_responses.add(
        responses.GET,
        f"{BASE_URL}/users/@me/relationships",
        json=[{"type": 1, "id": "1"}, {"type": 2, "id": "2"}],
        status=200,
    )
    friends = dmc.get_friends("token")
    assert len(friends) == 1
    assert friends[0]["id"] == "1"


def test_get_friends_401(mock_responses):
    mock_responses.add(responses.GET, f"{BASE_URL}/users/@me/relationships", status=401)
    with pytest.raises(ValueError, match="Invalid token"):
        dmc.get_friends("token")


@patch("time.sleep", return_value=None)
def test_get_friends_429(mock_sleep, mock_responses):
    mock_responses.add(
        responses.GET,
        f"{BASE_URL}/users/@me/relationships",
        json={"retry_after": 0.5},
        status=429,
    )
    mock_responses.add(
        responses.GET,
        f"{BASE_URL}/users/@me/relationships",
        json=[{"type": 1, "id": "1"}],
        status=200,
    )
    friends = dmc.get_friends("token")
    assert len(friends) == 1
    mock_sleep.assert_called_once_with(0.5)


def test_get_friends_http_error(mock_responses):
    mock_responses.add(responses.GET, f"{BASE_URL}/users/@me/relationships", status=500)
    with pytest.raises(requests.RequestException):
        dmc.get_friends("test_token")


# --- remove_friend tests ---


def test_remove_friend_success(mock_responses):
    mock_responses.add(
        responses.DELETE, f"{BASE_URL}/users/@me/relationships/123", body="", status=204
    )
    assert dmc.remove_friend("token", "123") == (204, "")


@patch("time.sleep", return_value=None)
def test_remove_friend_429(mock_sleep, mock_responses):
    mock_responses.add(
        responses.DELETE,
        f"{BASE_URL}/users/@me/relationships/123",
        json={"retry_after": 0.5},
        status=429,
    )
    mock_responses.add(
        responses.DELETE, f"{BASE_URL}/users/@me/relationships/123", body="", status=204
    )
    assert dmc.remove_friend("token", "123") == (204, "")
    mock_sleep.assert_called_once_with(0.5)


# --- get_dms tests ---


def test_get_dms_success(mock_responses):
    mock_responses.add(
        responses.GET, f"{BASE_URL}/users/@me/channels", json=[{"id": "1"}], status=200
    )
    dms = dmc.get_dms("token")
    assert len(dms) == 1


def test_get_dms_401(mock_responses):
    mock_responses.add(responses.GET, f"{BASE_URL}/users/@me/channels", status=401)
    with pytest.raises(ValueError, match="Invalid token"):
        dmc.get_dms("token")


@patch("time.sleep", return_value=None)
def test_get_dms_429(mock_sleep, mock_responses):
    mock_responses.add(
        responses.GET,
        f"{BASE_URL}/users/@me/channels",
        json={"retry_after": 0.5},
        status=429,
    )
    mock_responses.add(
        responses.GET, f"{BASE_URL}/users/@me/channels", json=[{"id": "1"}], status=200
    )
    dms = dmc.get_dms("token")
    assert len(dms) == 1
    mock_sleep.assert_called_once_with(0.5)


def test_get_dms_http_error(mock_responses):
    mock_responses.add(responses.GET, f"{BASE_URL}/users/@me/channels", status=500)
    with pytest.raises(requests.RequestException):
        dmc.get_dms("test_token")


# --- mark_channel_read tests ---


def test_mark_channel_read_success(mock_responses):
    mock_responses.add(
        responses.POST, f"{BASE_URL}/channels/123/messages/456/ack", body="", status=204
    )
    assert dmc.mark_channel_read("token", "123", "456") == (204, "")


@patch("time.sleep", return_value=None)
def test_mark_channel_read_429(mock_sleep, mock_responses):
    mock_responses.add(
        responses.POST,
        f"{BASE_URL}/channels/123/messages/456/ack",
        json={"retry_after": 0.5},
        status=429,
    )
    mock_responses.add(
        responses.POST, f"{BASE_URL}/channels/123/messages/456/ack", body="", status=204
    )
    assert dmc.mark_channel_read("token", "123", "456") == (204, "")
    mock_sleep.assert_called_once_with(0.5)


# --- Mass operations full tests (covering print branches) ---


@patch("builtins.input")
@patch("discord_mass_cleanup.get_guilds")
@patch("discord_mass_cleanup.leave_guild")
def test_mass_leave_servers_success(
    mock_leave_guild, mock_get_guilds, mock_input, capsys
):
    mock_get_guilds.return_value = [
        {"id": "1", "name": "Leavable 1"},
        {"id": "2", "name": "Owned 1", "owner": True},
        {"id": "3", "name": "Leavable 2"},
    ]
    mock_input.side_effect = ["1, 2", "yes"]
    mock_leave_guild.side_effect = [(204, ""), (400, "Error")]

    dmc.mass_leave_servers("token")

    mock_leave_guild.assert_any_call("token", "1")
    mock_leave_guild.assert_any_call("token", "3")
    captured = capsys.readouterr().out
    assert "Left:   Leavable 1" in captured
    assert "Failed: Leavable 2  (HTTP 400 - Error)" in captured


@patch("builtins.input")
@patch("discord_mass_cleanup.get_guilds")
def test_mass_leave_servers_no_servers(mock_get_guilds, mock_input, capsys):
    mock_get_guilds.return_value = []
    dmc.mass_leave_servers("token")
    captured = capsys.readouterr().out
    assert "No servers found." in captured


@patch("builtins.input")
@patch("discord_mass_cleanup.get_guilds")
def test_mass_leave_servers_all_owned(mock_get_guilds, mock_input, capsys):
    mock_get_guilds.return_value = [{"id": "2", "name": "Owned 1", "owner": True}]
    dmc.mass_leave_servers("token")
    captured = capsys.readouterr().out
    assert "You own all your servers — nothing to leave." in captured


@patch("builtins.input")
@patch("discord_mass_cleanup.get_guilds")
def test_mass_leave_servers_cancel_q(mock_get_guilds, mock_input, capsys):
    mock_get_guilds.return_value = [{"id": "1", "name": "Leavable 1"}]
    mock_input.side_effect = ["q"]
    dmc.mass_leave_servers("token")
    captured = capsys.readouterr().out
    assert "Cancelled." in captured


@patch("builtins.input")
@patch("discord_mass_cleanup.get_guilds")
@patch("discord_mass_cleanup.leave_guild")
def test_mass_leave_servers_all_and_cancel_confirm(
    mock_leave_guild, mock_get_guilds, mock_input, capsys
):
    mock_get_guilds.return_value = [{"id": "1", "name": "Leavable 1"}]
    mock_input.side_effect = ["all", "no"]
    dmc.mass_leave_servers("token")
    captured = capsys.readouterr().out
    assert "Cancelled." in captured
    mock_leave_guild.assert_not_called()


@patch("builtins.input")
@patch("discord_mass_cleanup.get_guilds")
def test_mass_leave_servers_invalid_input(mock_get_guilds, mock_input, capsys):
    mock_get_guilds.return_value = [{"id": "1", "name": "Leavable 1"}]
    mock_input.side_effect = ["abc"]
    dmc.mass_leave_servers("token")
    captured = capsys.readouterr().out
    assert "Invalid input" in captured


@patch("discord_mass_cleanup.get_guilds")
def test_mass_leave_servers_value_error(mock_get_guilds, capsys):
    mock_get_guilds.side_effect = ValueError("Invalid token test")
    dmc.mass_leave_servers("token")
    captured = capsys.readouterr().out
    assert "Invalid token test" in captured


@patch("discord_mass_cleanup.get_guilds")
def test_mass_leave_servers_request_error(mock_get_guilds, capsys):
    mock_get_guilds.side_effect = requests.RequestException("Network error")
    dmc.mass_leave_servers("token")
    captured = capsys.readouterr().out
    assert "Network/API error fetching servers: Network error" in captured


@patch("builtins.input")
@patch("discord_mass_cleanup.get_guilds")
def test_mass_leave_servers_nothing_selected(mock_get_guilds, mock_input, capsys):
    mock_get_guilds.return_value = [{"id": "1", "name": "Leavable 1"}]
    mock_input.side_effect = [""]
    dmc.mass_leave_servers("token")
    captured = capsys.readouterr().out
    assert "Nothing selected." in captured


@patch("builtins.input")
@patch("discord_mass_cleanup.get_friends")
@patch("discord_mass_cleanup.remove_friend")
def test_mass_remove_friends_full(
    mock_remove_friend, mock_get_friends, mock_input, capsys
):
    mock_get_friends.return_value = [
        {"id": "1", "user": {"username": "user1", "global_name": "User 1"}},
        {"id": "2", "user": {"username": "user2"}},
    ]
    mock_input.side_effect = ["all", "yes"]
    mock_remove_friend.return_value = (204, "")
    dmc.mass_remove_friends("token")
    assert mock_remove_friend.call_count == 2
    captured = capsys.readouterr().out
    assert "Removed: User 1" in captured
    assert "Removed: user2" in captured


@patch("discord_mass_cleanup.get_friends")
def test_mass_remove_friends_no_friends(mock_get_friends, capsys):
    mock_get_friends.return_value = []
    dmc.mass_remove_friends("token")
    captured = capsys.readouterr().out
    assert "No friends found." in captured


@patch("builtins.input")
@patch("discord_mass_cleanup.get_friends")
def test_mass_remove_friends_cancel(mock_get_friends, mock_input, capsys):
    mock_get_friends.return_value = [{"id": "1", "user": {"username": "user1"}}]
    mock_input.side_effect = ["q"]
    dmc.mass_remove_friends("token")
    captured = capsys.readouterr().out
    assert "Cancelled." in captured


@patch("builtins.input")
@patch("discord_mass_cleanup.get_friends")
def test_mass_remove_friends_invalid(mock_get_friends, mock_input, capsys):
    mock_get_friends.return_value = [{"id": "1", "user": {"username": "user1"}}]
    mock_input.side_effect = ["abc"]
    dmc.mass_remove_friends("token")
    captured = capsys.readouterr().out
    assert "Invalid input" in captured


@patch("builtins.input")
@patch("discord_mass_cleanup.get_friends")
@patch("discord_mass_cleanup.remove_friend")
def test_mass_remove_friends_cancel_confirm(
    mock_remove, mock_get_friends, mock_input, capsys
):
    mock_get_friends.return_value = [{"id": "1", "user": {"username": "user1"}}]
    mock_input.side_effect = ["all", "no"]
    dmc.mass_remove_friends("token")
    captured = capsys.readouterr().out
    assert "Cancelled." in captured
    mock_remove.assert_not_called()


@patch("discord_mass_cleanup.get_friends")
def test_mass_remove_friends_value_error(mock_get_friends, capsys):
    mock_get_friends.side_effect = ValueError("Invalid token")
    dmc.mass_remove_friends("token")
    assert "Invalid token" in capsys.readouterr().out


@patch("discord_mass_cleanup.get_friends")
def test_mass_remove_friends_req_error(mock_get_friends, capsys):
    mock_get_friends.side_effect = requests.RequestException("Err")
    dmc.mass_remove_friends("token")
    assert "Err" in capsys.readouterr().out


@patch("builtins.input")
@patch("discord_mass_cleanup.get_friends")
def test_mass_remove_friends_empty_selection(mock_get_friends, mock_input, capsys):
    mock_get_friends.return_value = [{"id": "1", "user": {"username": "user1"}}]
    mock_input.side_effect = [""]
    dmc.mass_remove_friends("token")
    assert "Nothing selected." in capsys.readouterr().out


@patch("builtins.input")
@patch("discord_mass_cleanup.get_dms")
@patch("discord_mass_cleanup.mark_channel_read")
def test_mass_mark_read_full(mock_mark_read, mock_get_dms, mock_input, capsys):
    mock_get_dms.return_value = [
        {"id": "1", "last_message_id": "100", "name": "Test Group"},
        {"id": "2", "last_message_id": "200", "recipients": [{"username": "user1"}]},
        {
            "id": "3",
            "last_message_id": "300",
            "recipients": [{"username": "u1"}, {"username": "u2"}],
        },
        {"id": "4", "last_message_id": "400"},  # Unknown DM
        {"id": "5", "last_message_id": None},  # ignored
    ]
    mock_input.side_effect = ["yes"]
    mock_mark_read.side_effect = [(200, ""), (204, ""), (400, "Err"), (200, "")]

    dmc.mass_mark_read("token")

    assert mock_mark_read.call_count == 4
    captured = capsys.readouterr().out
    assert "Marked Read: Test Group" in captured
    assert "Marked Read: user1" in captured
    assert "Failed:      Group Chat  (HTTP 400 - Err)" in captured
    assert "Marked Read: Unknown DM" in captured


@patch("discord_mass_cleanup.get_dms")
def test_mass_mark_read_no_dms(mock_get_dms, capsys):
    mock_get_dms.return_value = []
    dmc.mass_mark_read("token")
    assert "No DM channels found." in capsys.readouterr().out


@patch("builtins.input")
@patch("discord_mass_cleanup.get_dms")
def test_mass_mark_read_cancel(mock_get_dms, mock_input, capsys):
    mock_get_dms.return_value = [
        {"id": "1", "last_message_id": "100", "name": "Test Group"}
    ]
    mock_input.side_effect = ["no"]
    dmc.mass_mark_read("token")
    assert "Cancelled." in capsys.readouterr().out


@patch("discord_mass_cleanup.get_dms")
def test_mass_mark_read_value_error(mock_get_dms, capsys):
    mock_get_dms.side_effect = ValueError("Invalid token")
    dmc.mass_mark_read("token")
    assert "Invalid token" in capsys.readouterr().out


@patch("discord_mass_cleanup.get_dms")
def test_mass_mark_read_req_error(mock_get_dms, capsys):
    mock_get_dms.side_effect = requests.RequestException("Err")
    dmc.mass_mark_read("token")
    assert "Err" in capsys.readouterr().out


# --- Main menu tests ---


@patch("builtins.input")
@patch("pwinput.pwinput")
@patch("os.getenv")
@patch("discord_mass_cleanup.mass_leave_servers")
@patch("discord_mass_cleanup.mass_remove_friends")
@patch("discord_mass_cleanup.mass_mark_read")
@patch("discord_mass_cleanup.mass_mark_guilds_read")
def test_main_menu(
    mock_mark_guilds,
    mock_mark,
    mock_remove,
    mock_leave,
    mock_getenv,
    mock_pwinput,
    mock_input,
    capsys,
):
    mock_getenv.return_value = None
    mock_pwinput.return_value = "my_token"
    # choice 1, 2, 3, 4, invalid choice, q
    mock_input.side_effect = ["1", "2", "3", "4", "9", "q"]

    dmc.main()

    mock_leave.assert_called_once_with("my_token")
    mock_remove.assert_called_once_with("my_token")
    mock_mark.assert_called_once_with("my_token")
    mock_mark_guilds.assert_called_once_with("my_token")
    captured = capsys.readouterr().out
    assert "Invalid choice" in captured
    assert "Exiting..." in captured


@patch("pwinput.pwinput")
@patch("os.getenv")
def test_main_no_token(mock_getenv, mock_pwinput, capsys):
    mock_getenv.return_value = None
    mock_pwinput.return_value = ""
    dmc.main()
    assert "No token entered. Exiting." in capsys.readouterr().out


@patch("builtins.input")
@patch("os.getenv")
def test_main_env_token(mock_getenv, mock_input, capsys):
    mock_getenv.return_value = "env_token"
    mock_input.side_effect = ["q"]
    dmc.main()
    assert "Using token from .env file." in capsys.readouterr().out
