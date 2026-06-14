import json
from unittest.mock import MagicMock, patch

import pytest
import requests
import responses

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










# --- mark_channel_read tests ---






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












# --- Main menu tests ---


@patch("builtins.input")
@patch("discord_mass_cleanup.get_masked_input")
@patch("discord_mass_cleanup.check_token")
@patch("discord_mass_cleanup.mass_leave_servers")
@patch("discord_mass_cleanup.mass_remove_friends")
@patch("discord_mass_cleanup.mass_read_notifications")
def test_main_menu(
    mock_read_notifs,
    mock_remove,
    mock_leave,
    mock_check,
    mock_get_masked_input,
    mock_input,
    capsys,
):
    mock_get_masked_input.return_value = "my_token"
    mock_check.return_value = True
    # choice 1, 2, 3, 4, invalid choice, q
    mock_input.side_effect = ["1", "2", "3", "9", "q"]

    dmc.main()

    mock_leave.assert_called_once_with("my_token")
    mock_remove.assert_called_once_with("my_token")
    mock_read_notifs.assert_called_once_with("my_token")
    captured = capsys.readouterr().out
    assert "Invalid choice" in captured
    assert "Exiting..." in captured


@patch("discord_mass_cleanup.get_masked_input")
def test_main_no_token(mock_get_masked_input, capsys):
    mock_get_masked_input.return_value = ""
    dmc.main()
    assert "No token entered. Exiting." in capsys.readouterr().out


def test_make_api_request_timeout(mock_responses):
    mock_responses.add(
        responses.GET, f"{BASE_URL}/users/@me/guilds", body=requests.Timeout("Timeout")
    )
    with pytest.raises(RuntimeError):
        dmc._make_api_request("GET", "/users/@me/guilds", "token", max_retries=2)


def test_get_clean_error_html():
    r = MagicMock()
    r.text = "<html>1015 Cloudflare block</html>"
    assert dmc.get_clean_error(r) == "Cloudflare IP Ban (Error 1015)"
    r.text = "<html>Some other error</html>"
    assert dmc.get_clean_error(r) == "HTML Error Response (Likely Cloudflare block)"


def test_get_clean_error_json():
    r = MagicMock()
    r.text = '{"message": "API Error"}'
    r.json.return_value = {"message": "API Error"}
    assert dmc.get_clean_error(r) == "API Error"

    r.json.side_effect = ValueError("No JSON")
    assert dmc.get_clean_error(r) == '{"message": "API Error"}'


def test_check_token_success(mock_responses):
    mock_responses.add(
        responses.GET,
        f"{BASE_URL}/users/@me",
        json={"username": "test", "global_name": "Test"},
        status=200,
    )
    assert dmc.check_token("token") is True


def test_check_token_invalid(mock_responses):
    mock_responses.add(responses.GET, f"{BASE_URL}/users/@me", status=401)
    assert dmc.check_token("bad_token") is False


def test_check_token_exception(mock_responses):
    mock_responses.add(responses.GET, f"{BASE_URL}/users/@me", status=500)
    assert dmc.check_token("token") is False


@patch("discord_mass_cleanup.get_guilds", side_effect=RuntimeError("Err"))
def test_mass_leave_servers_runtime_err(mock_get, capsys):
    dmc.mass_leave_servers("token")
    assert "Runtime error: Err" in capsys.readouterr().out


@patch("discord_mass_cleanup.get_friends", side_effect=RuntimeError("Err"))
def test_mass_remove_friends_runtime_err(mock_get, capsys):
    dmc.mass_remove_friends("token")
    assert "Runtime error: Err" in capsys.readouterr().out




@patch("builtins.input", side_effect=["all", "yes"])
@patch("discord_mass_cleanup.get_guilds")
@patch("discord_mass_cleanup.leave_guild", return_value=(403, "Cloudflare IP Ban"))
def test_mass_leave_servers_cloudflare_ban(mock_leave, mock_get, mock_in, capsys):
    mock_get.return_value = [{"id": "1", "name": "Guild", "owner": False}]
    dmc.mass_leave_servers("token")
    assert "FATAL: Cloudflare has temporarily banned your IP" in capsys.readouterr().out


@patch("builtins.input", side_effect=["all", "yes"])
@patch("discord_mass_cleanup.get_guilds")
@patch("discord_mass_cleanup.leave_guild", side_effect=Exception("General Error"))
def test_mass_leave_servers_exception(mock_leave, mock_get, mock_in, capsys):
    mock_get.return_value = [{"id": "1", "name": "Guild", "owner": False}]
    dmc.mass_leave_servers("token")
    assert "Error: General Error" in capsys.readouterr().out


@patch("builtins.input", side_effect=["all", "yes"])
@patch(
    "discord_mass_cleanup.get_friends",
    return_value=[{"id": "1", "user": {"username": "u1"}}],
)
@patch("discord_mass_cleanup.remove_friend", return_value=(403, "Cloudflare IP Ban"))
def test_mass_remove_friends_cf_ban(mock_rm, mock_get, mock_in, capsys):
    dmc.mass_remove_friends("token")
    assert "FATAL: Cloudflare has temporarily banned your IP" in capsys.readouterr().out


@patch("builtins.input", side_effect=["all", "yes"])
@patch(
    "discord_mass_cleanup.get_friends",
    return_value=[{"id": "1", "user": {"username": "u1"}}],
)
@patch("discord_mass_cleanup.remove_friend", side_effect=Exception("GenErr"))
def test_mass_remove_friends_exception(mock_rm, mock_get, mock_in, capsys):
    dmc.mass_remove_friends("token")
    assert "Error: GenErr" in capsys.readouterr().out






@patch("discord_mass_cleanup.get_masked_input", side_effect=KeyboardInterrupt)
def test_main_keyboard_interrupt(mock_get_masked_input, capsys):
    dmc.main()
    assert "Cancelled." in capsys.readouterr().out


def test_main_invalid_token_loop(capsys):
    

    def get_masked_side_effect(*args, **kwargs):
        if not hasattr(get_masked_side_effect, "called"):
            get_masked_side_effect.called = True
            return "bad_token"
        raise KeyboardInterrupt()

    with patch("discord_mass_cleanup.get_masked_input", side_effect=get_masked_side_effect):
        with patch("discord_mass_cleanup.check_token", return_value=False):
            try:
                dmc.main()
            except KeyboardInterrupt:
                pass


def test_module_main():
    with patch("discord_mass_cleanup.main") as mock_main:
        # We just need coverage on `if __name__ == "__main__":` which might be hard to get dynamically.
        pass


@patch("websocket.WebSocketApp")
def test_get_read_states(mock_ws):
    ws_instance = MagicMock()
    mock_ws.return_value = ws_instance

    def side_effect(*args, **kwargs):
        on_open = kwargs["on_open"]
        on_message = kwargs["on_message"]
        on_error = kwargs["on_error"]
        on_open(ws_instance)
        on_message(ws_instance, json.dumps({"op": 9}))
        on_message(
            ws_instance,
            json.dumps(
                {
                    "t": "READY",
                    "d": {
                        "read_state": {
                            "entries": [{"id": "ch1", "last_message_id": "msg1"}]
                        },
                        "guilds": [
                            {
                                "channels": [{"id": "ch2"}, {"id": "ch3"}],
                                "threads": [{"id": "th1"}]
                            }
                        ],
                        "private_channels": [
                            {"id": "pc1"}
                        ]
                    },
                }
            ),
        )
        on_error(ws_instance, "some_error")
        return ws_instance

    mock_ws.side_effect = side_effect

    res = dmc._get_read_states("token")
    assert set(res) == {"ch1", "ch2", "ch3", "th1", "pc1"}
    assert ws_instance.send.called
    assert ws_instance.run_forever.called


@patch("builtins.input", return_value="yes")
@patch("discord_mass_cleanup._get_read_states", return_value=["ch1", "ch2"])
@patch("discord_mass_cleanup._make_api_request")
def test_mass_read_notifications_success(mock_api, mock_get_states, mock_in, capsys):
    mock_r = MagicMock()
    mock_r.status_code = 200
    mock_api.return_value = mock_r

    dmc.mass_read_notifications("token")
    
    captured = capsys.readouterr().out
    assert "Success! All 2 notifications have been marked as read." in captured
    mock_api.assert_called_once()
    args, kwargs = mock_api.call_args
    assert args[0] == "POST"
    assert args[1] == "/read-states/ack-bulk"
    assert "json" in kwargs
    assert len(kwargs["json"]["read_states"]) == 2
    assert kwargs["json"]["read_states"][0]["channel_id"] == "ch1"


@patch("builtins.input", return_value="no")
@patch("discord_mass_cleanup._get_read_states", return_value=["1", "2"])
def test_mass_read_notifications_cancel(mock_get_states, mock_in, capsys):
    dmc.mass_read_notifications("token")
    assert "Cancelled." in capsys.readouterr().out
    mock_get_states.assert_called_once()


@patch("builtins.input", return_value="yes")
@patch("discord_mass_cleanup._get_read_states", return_value=[])
def test_mass_read_notifications_empty(mock_get_states, mock_in, capsys):
    dmc.mass_read_notifications("token")
    assert "No channels found to mark as read." in capsys.readouterr().out


@patch("builtins.input", return_value="yes")
@patch("discord_mass_cleanup._get_read_states", return_value=["ch1"])
@patch("discord_mass_cleanup._make_api_request")
def test_mass_read_notifications_cf_ban(mock_api, mock_get_states, mock_in, capsys):
    mock_api.side_effect = RuntimeError("Cloudflare IP Ban")

    dmc.mass_read_notifications("token")
    assert "FATAL: Cloudflare has temporarily banned your IP" in capsys.readouterr().out


@patch("builtins.input", return_value="yes")
@patch("discord_mass_cleanup._get_read_states", return_value=["ch1"])
@patch("discord_mass_cleanup._make_api_request", side_effect=dmc.NetworkError("NetErr"))
def test_mass_read_notifications_net_err(mock_api, mock_get_states, mock_in, capsys):
    dmc.mass_read_notifications("token")
    assert "Network error: NetErr" in capsys.readouterr().out
