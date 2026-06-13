"""
Discord Mass Account Cleanup Tool
================================
Lets you mass leave servers and mass remove friends.

Requirements:
    pip install requests python-dotenv pwinput

Usage:
    python discord_mass_cleanup.py
"""

import requests
import time
import os
import pwinput
from dotenv import load_dotenv

BASE_URL = "https://discord.com/api/v10"
REQUEST_DELAY = 0.6  # seconds between requests (be polite to the API)


# ── Shared API Request Helper ─────────────────────────────────────────────────


def _make_api_request(
    method: str, endpoint: str, token: str, max_retries: int = 5, **kwargs
) -> requests.Response:
    """Helper for Discord API requests with consistent rate-limit and timeout handling."""
    headers = {"Authorization": token}
    url = f"{BASE_URL}{endpoint}"

    retries = 0
    while retries < max_retries:
        try:
            r = requests.request(method, url, headers=headers, timeout=10, **kwargs)
        except requests.Timeout:
            print("  ⏳  Request timed out — retrying…")
            retries += 1
            time.sleep(2)
            continue

        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 5.0))
            except ValueError:
                wait = 5.0
            print(f"  ⏳  Rate-limited — waiting {wait:.2f}s…")
            time.sleep(wait)
            retries += 1
            continue

        return r

    raise RuntimeError(f"Max retries ({max_retries}) exceeded for {method} {endpoint}")


# ── API helpers (Servers) ─────────────────────────────────────────────────────


def get_guilds(token: str) -> list[dict]:
    """Fetch all guilds the user is in (handles pagination)."""
    guilds = []
    after = None

    while True:
        params: dict = {"limit": 200}
        if after:
            params["after"] = after

        r = _make_api_request("GET", "/users/@me/guilds", token, params=params)

        if r.status_code == 401:
            raise ValueError("\n✗  Invalid token — please double-check and try again.")
        r.raise_for_status()

        page = r.json()
        guilds.extend(page)

        if len(page) < 200:
            break
        after = page[-1]["id"]

    return guilds


def leave_guild(token: str, guild_id: str) -> tuple[int, str]:
    """Leave a single guild. Returns final HTTP status code and text response."""
    r = _make_api_request("DELETE", f"/users/@me/guilds/{guild_id}", token)
    return r.status_code, r.text


# ── API helpers (Friends) ─────────────────────────────────────────────────────


def get_friends(token: str) -> list[dict]:
    """Fetch all friends."""
    r = _make_api_request("GET", "/users/@me/relationships", token)

    if r.status_code == 401:
        raise ValueError("\n✗  Invalid token — please double-check and try again.")
    r.raise_for_status()

    relationships = r.json()

    # type 1 is friend
    friends = [rel for rel in relationships if rel.get("type") == 1]
    return friends


def remove_friend(token: str, user_id: str) -> tuple[int, str]:
    """Remove a single friend. Returns final HTTP status code and text response."""
    r = _make_api_request("DELETE", f"/users/@me/relationships/{user_id}", token)
    return r.status_code, r.text


# ── API helpers (Read States) ─────────────────────────────────────────────────


def get_dms(token: str) -> list[dict]:
    """Fetch all direct message channels (DMs and Group DMs)."""
    r = _make_api_request("GET", "/users/@me/channels", token)

    if r.status_code == 401:
        raise ValueError("\n✗  Invalid token — please double-check and try again.")
    r.raise_for_status()
    return r.json()


def mark_channel_read(token: str, channel_id: str, message_id: str) -> tuple[int, str]:
    """Acknowledge a channel up to the given message_id."""
    payload = {"token": None}
    r = _make_api_request(
        "POST", f"/channels/{channel_id}/messages/{message_id}/ack", token, json=payload
    )
    return r.status_code, r.text


def mark_guild_read(token: str, guild_id: str) -> tuple[int, str]:
    """Acknowledge all messages in a guild."""
    r = _make_api_request("POST", f"/guilds/{guild_id}/ack", token, json={})
    return r.status_code, r.text


def check_token(token: str) -> bool:
    """Verifies if the provided token is valid."""
    print("Verifying token...")
    try:
        r = _make_api_request("GET", "/users/@me", token, max_retries=1)
        if r.status_code == 401:
            return False
        r.raise_for_status()
        user = r.json()
        display = user.get("global_name") or user.get("username")
        print(f"  ✓  Logged in as {display} (@{user['username']})")
        return True
    except Exception as e:
        print(f"  ✗  Error verifying token: {e}")
        return False


# ── Selection parser ──────────────────────────────────────────────────────────


def parse_selection(text: str, max_index: int) -> list[int]:
    """
    Turn a string like '1,3,5-10,15' into a sorted list of 1-based indices.
    Raises ValueError on bad input.
    """
    indices: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_str, hi_str = part.split("-", 1)
            lo, hi = int(lo_str), int(hi_str)
            if lo > hi:
                lo, hi = hi, lo
            for n in range(lo, hi + 1):
                indices.add(n)
        else:
            indices.add(int(part))

    valid = sorted(i for i in indices if 1 <= i <= max_index)
    oob = sorted(i for i in indices if not (1 <= i <= max_index))
    if oob:
        print(f"   ⚠  Ignoring out-of-range number(s): {', '.join(map(str, oob))}")
    return valid


# ── Main ──────────────────────────────────────────────────────────────────────


def mass_leave_servers(token: str) -> None:
    print("\nFetching your servers…")
    try:
        all_guilds = get_guilds(token)
    except ValueError as e:
        print(e)
        return
    except requests.RequestException as e:
        print(f"\n✗  Network/API error fetching servers: {e}")
        return
    except RuntimeError as e:
        print(f"\n✗  Runtime error: {e}")
        return

    if not all_guilds:
        print("No servers found.")
        return

    leavable = [g for g in all_guilds if not g.get("owner")]
    owned = [g for g in all_guilds if g.get("owner")]

    print(
        f"\nFound {len(all_guilds)} server(s)  "
        f"({len(leavable)} leavable, {len(owned)} owned by you)\n"
    )

    if not leavable:
        print("You own all your servers — nothing to leave.")
        return

    col_w = max(len(g["name"]) for g in leavable) + 2
    for i, g in enumerate(leavable, 1):
        print(f"  [{i:>3}]  {g['name']:<{col_w}}")

    if owned:
        print("\n  (Owned — skipped:)")
        for g in owned:
            print(f"      - {g['name']}")

    print("\nEnter servers to leave:")
    print("  • Numbers / ranges  →  1,3,5-10,15")
    print("  • All leavable      →  all")
    print("  • Cancel            →  q\n")

    raw = input("Selection > ").strip()

    if raw.lower() == "q":
        print("Cancelled.")
        return

    selected: list[dict] = []

    if raw.lower() == "all":
        selected = leavable[:]
    else:
        try:
            indices = parse_selection(raw, len(leavable))
            selected = [leavable[i - 1] for i in indices]
        except ValueError:
            print("Invalid input — use numbers, ranges (e.g. 2-5), or 'all'.")
            return

    if not selected:
        print("Nothing selected.")
        return

    print(f"\nAbout to leave {len(selected)} server(s):\n")
    for g in selected:
        print(f"  –  {g['name']}")

    confirm = input("\nType 'yes' to confirm: ").strip()
    if confirm.lower() != "yes":
        print("Cancelled.")
        return

    print()
    success = failed = 0
    for g in selected:
        try:
            status, text = leave_guild(token, g["id"])
            if status == 204:
                print(f"  ✓  Left:   {g['name']}")
                success += 1
            else:
                print(f"  ✗  Failed: {g['name']}  (HTTP {status} - {text})")
                failed += 1
        except Exception as e:
            print(f"  ✗  Failed: {g['name']}  (Error: {e})")
            failed += 1

        time.sleep(REQUEST_DELAY)

    print(f"\nDone — left {success}, failed {failed}.")


def mass_remove_friends(token: str) -> None:
    print("\nFetching your friends…")
    try:
        all_friends = get_friends(token)
    except ValueError as e:
        print(e)
        return
    except requests.RequestException as e:
        print(f"\n✗  Network/API error fetching friends: {e}")
        return
    except RuntimeError as e:
        print(f"\n✗  Runtime error: {e}")
        return

    if not all_friends:
        print("No friends found.")
        return

    print(f"\nFound {len(all_friends)} friend(s)\n")

    col_w = max(len(f["user"]["username"]) for f in all_friends) + 2
    for i, f in enumerate(all_friends, 1):
        # Discord usernames have a global name optionally
        display = f["user"].get("global_name") or f["user"]["username"]
        print(f"  [{i:>3}]  {display:<{col_w}} (@{f['user']['username']})")

    print("\nEnter friends to remove:")
    print("  • Numbers / ranges  →  1,3,5-10,15")
    print("  • All friends       →  all")
    print("  • Cancel            →  q\n")

    raw = input("Selection > ").strip()

    if raw.lower() == "q":
        print("Cancelled.")
        return

    selected: list[dict] = []

    if raw.lower() == "all":
        selected = all_friends[:]
    else:
        try:
            indices = parse_selection(raw, len(all_friends))
            selected = [all_friends[i - 1] for i in indices]
        except ValueError:
            print("Invalid input — use numbers, ranges (e.g. 2-5), or 'all'.")
            return

    if not selected:
        print("Nothing selected.")
        return

    print(f"\nAbout to remove {len(selected)} friend(s):\n")
    for f in selected:
        display = f["user"].get("global_name") or f["user"]["username"]
        print(f"  –  {display} (@{f['user']['username']})")

    confirm = input("\nType 'yes' to confirm: ").strip()
    if confirm.lower() != "yes":
        print("Cancelled.")
        return

    print()
    success = failed = 0
    for f in selected:
        user_id = f["id"]
        display = f["user"].get("global_name") or f["user"]["username"]

        try:
            status, text = remove_friend(token, user_id)
            if status == 204:
                print(f"  ✓  Removed: {display}")
                success += 1
            else:
                print(f"  ✗  Failed:  {display}  (HTTP {status} - {text})")
                failed += 1
        except Exception as e:
            print(f"  ✗  Failed:  {display}  (Error: {e})")
            failed += 1

        time.sleep(REQUEST_DELAY)

    print(f"\nDone — removed {success}, failed {failed}.")


def mass_mark_read(token: str) -> None:
    print("\nFetching your DM channels…")
    try:
        channels = get_dms(token)
    except ValueError as e:
        print(e)
        return
    except requests.RequestException as e:
        print(f"\n✗  Network/API error fetching DMs: {e}")
        return
    except RuntimeError as e:
        print(f"\n✗  Runtime error: {e}")
        return

    if not channels:
        print("No DM channels found.")
        return

    valid_channels = [c for c in channels if c.get("last_message_id")]

    print(f"\nFound {len(valid_channels)} DM(s) to process.\n")

    confirm = input("Type 'yes' to mark all these DMs as read: ").strip()
    if confirm.lower() != "yes":
        print("Cancelled.")
        return

    print()
    success = failed = 0
    for c in valid_channels:
        channel_id = c["id"]
        message_id = c["last_message_id"]

        name = "Unknown DM"
        if c.get("name"):
            name = c["name"]
        elif c.get("recipients") and len(c["recipients"]) > 0:
            if len(c["recipients"]) == 1:
                name = (
                    c["recipients"][0].get("global_name")
                    or c["recipients"][0].get("username")
                    or "User"
                )
            else:
                name = "Group Chat"

        try:
            status, text = mark_channel_read(token, channel_id, message_id)
            if status in (200, 204):
                print(f"  ✓  Marked Read: {name}")
                success += 1
            else:
                print(f"  ✗  Failed:      {name}  (HTTP {status} - {text})")
                failed += 1
        except Exception as e:
            print(f"  ✗  Failed:      {name}  (Error: {e})")
            failed += 1

        time.sleep(REQUEST_DELAY)

    print(f"\nDone — marked read {success}, failed {failed}.")


def mass_mark_guilds_read(token: str) -> None:
    print("\nFetching your servers…")
    try:
        all_guilds = get_guilds(token)
    except ValueError as e:
        print(e)
        return
    except requests.RequestException as e:
        print(f"\n✗  Network/API error fetching servers: {e}")
        return
    except RuntimeError as e:
        print(f"\n✗  Runtime error: {e}")
        return

    if not all_guilds:
        print("No servers found.")
        return

    print(f"\nFound {len(all_guilds)} server(s) to process.\n")

    confirm = input("Type 'yes' to mark all these servers as read: ").strip()
    if confirm.lower() != "yes":
        print("Cancelled.")
        return

    print()
    success = failed = 0
    for g in all_guilds:
        guild_id = g["id"]
        name = g["name"]

        try:
            status, text = mark_guild_read(token, guild_id)
            if status in (200, 204):
                print(f"  ✓  Marked Read: {name}")
                success += 1
            else:
                print(f"  ✗  Failed:      {name}  (HTTP {status} - {text})")
                failed += 1
        except Exception as e:
            print(f"  ✗  Failed:      {name}  (Error: {e})")
            failed += 1

        time.sleep(REQUEST_DELAY)

    print(f"\nDone — marked read {success}, failed {failed}.")


def main() -> None:
    print("\n╔══════════════════════════════════════════╗")
    print("║   Discord Mass Account Cleanup Tool      ║")
    print("╚══════════════════════════════════════════╝\n")
    print("⚠  This uses your Discord user token, which is technically")
    print("   self-botting and against Discord's ToS. Low risk for")
    print("   a one-off cleanup, but use at your own discretion.\n")

    while True:
        token = ""
        load_dotenv()
        env_token = os.getenv("DISCORD_TOKEN")

        if env_token:
            print("Using token from .env file.")
            token = env_token.strip()

        if not token:
            print("── How to get your token ──────────────────────────────────")
            print("  1. Open https://discord.com in your browser and log in.")
            print("  2. Press F12 to open Developer Tools.")
            print("  3. Go to Application tab → Local Storage → https://discord.com")
            print("  4. Press Ctrl+Shift+M (or Cmd+Shift+M) to toggle Mobile View.")
            print(
                "  5. Type 'token' in the filter box and copy the value without quotes."
            )
            print("───────────────────────────────────────────────────────────\n")
            try:
                token = pwinput.pwinput("Paste token: ", mask="*").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                return

        if not token:
            print("No token entered. Exiting.")
            return

        if not check_token(token):
            print("\n✗  Invalid token — please double-check and try again.\n")
            if "DISCORD_TOKEN" in os.environ:
                del os.environ["DISCORD_TOKEN"]
            continue

        while True:
            print("\nMain Menu:")
            print("  [1] Mass Leave Servers")
            print("  [2] Mass Remove Friends")
            print("  [3] Mass Mark DMs as Read")
            print("  [4] Mass Mark Servers as Read")
            print("  [t] Change Token / Switch Account")
            print("  [q] Quit\n")

            choice = input("Select an option > ").strip().lower()

            if choice == "1":
                mass_leave_servers(token)
            elif choice == "2":
                mass_remove_friends(token)
            elif choice == "3":
                mass_mark_read(token)
            elif choice == "4":
                mass_mark_guilds_read(token)
            elif choice == "t":
                if "DISCORD_TOKEN" in os.environ:
                    del os.environ["DISCORD_TOKEN"]
                print("\nLogging out...")
                break
            elif choice == "q":
                print("Exiting...")
                return
            else:
                print("Invalid choice. Please select 1, 2, 3, 4, t, or q.")


if __name__ == "__main__":
    main()
