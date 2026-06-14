"""
Discord Mass Account Cleanup Tool
================================
Lets you mass leave servers and mass remove friends.

Requirements:
    pip install requests websocket-client

Usage:
    python discord_mass_cleanup.py
"""

import json
import os
import time

import sys
import threading
import websocket

# Test environment detection to preserve 'responses' library compatibility
IN_PYTEST = "pytest" in sys.modules

try:
    if IN_PYTEST:
        raise ImportError("Using standard requests for tests")
    from curl_cffi import requests
    from curl_cffi.requests.errors import RequestsError as NetworkError
    HAS_CURL_CFFI = True
except ImportError:
    import requests
    from requests.exceptions import RequestException as NetworkError
    HAS_CURL_CFFI = False


BASE_URL = "https://discord.com/api/v10"
REQUEST_DELAY = 0.6  # seconds between requests (be polite to the API)


# ── Shared API Request Helper ─────────────────────────────────────────────────


def _make_api_request(
    method: str, endpoint: str, token: str, max_retries: int = 5, quiet: bool = False, **kwargs
) -> requests.Response:
    """Helper for Discord API requests with consistent rate-limit and timeout handling."""
    headers = {"Authorization": token}
    url = f"{BASE_URL}{endpoint}"

    retries = 0
    while retries < max_retries:
        try:
            if HAS_CURL_CFFI:
                r = requests.request(method, url, headers=headers, timeout=10, impersonate="chrome110", **kwargs)
            else:
                r = requests.request(method, url, headers=headers, timeout=10, **kwargs)
        except Exception as e:
            if "timeout" in str(e).lower() or "timeout" in type(e).__name__.lower():
                if not quiet:
                    print("  ⏳  Request timed out — retrying…")
                retries += 1
                time.sleep(2)
                continue
            raise

        if r.status_code == 429:
            if "<html" in r.text.lower() and "1015" in r.text:
                raise RuntimeError("Cloudflare IP Ban (Error 1015) - You are making requests too fast and Discord blocked your IP for 1 hour.")
            
            try:
                # If we get a Discord 429, parse retry_after
                wait = float(r.json().get("retry_after", 5.0))
            except Exception:
                wait = 5.0
            if not quiet:
                print(f"  ⏳  Rate-limited — waiting {wait:.2f}s…")
            time.sleep(wait)
            retries += 1
            continue

        return r

    raise RuntimeError(f"Max retries ({max_retries}) exceeded for {method} {endpoint}")


# ── API helpers (Servers) ─────────────────────────────────────────────────────


def get_clean_error(r: requests.Response) -> str:
    """Helper to extract a clean error message, avoiding huge HTML dumps."""
    text = r.text
    if "<html" in text.lower():
        if "1015" in text:
            return "Cloudflare IP Ban (Error 1015)"
        return "HTML Error Response (Likely Cloudflare block)"
    try:
        data = r.json()
        if "message" in data:
            return data["message"]
    except ValueError:
        pass
    return text[:100] + "..." if len(text) > 100 else text


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
    """Leave a guild."""
    r = _make_api_request("DELETE", f"/users/@me/guilds/{guild_id}", token)
    return r.status_code, get_clean_error(r)


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
    """Remove a friend by user ID."""
    r = _make_api_request("DELETE", f"/users/@me/relationships/{user_id}", token)
    return r.status_code, get_clean_error(r)


# ── API helpers (Read States) ─────────────────────────────────────────────────




def _get_read_states(token: str) -> list[str]:
    """Connects to the Discord WS to extract all channel IDs from read_state, guilds, and private_channels."""
    channel_ids = set()
    has_received_ready = False
    print("  [WS] Connecting to Discord Gateway to fetch your read states...")

    def on_message(ws, message):
        nonlocal has_received_ready
        try:
            data = json.loads(message)
            if data.get("op") == 9:
                print("  [WS] Invalid session! Token might be bad or WS blocked.")
                ws.close()
            elif data.get("t") == "READY":
                has_received_ready = True
                d = data.get("d")
                if isinstance(d, dict):
                    # 1. Grab read state entries
                    read_state = d.get("read_state")
                    if isinstance(read_state, dict) and "entries" in read_state:
                        for entry in read_state["entries"]:
                            if isinstance(entry, dict) and entry.get("id"):
                                channel_ids.add(entry.get("id"))
                    
                    # 2. Grab all channels and threads from guilds
                    guilds = d.get("guilds", [])
                    if isinstance(guilds, list):
                        for guild in guilds:
                            if isinstance(guild, dict):
                                if "channels" in guild and isinstance(guild["channels"], list):
                                    for channel in guild["channels"]:
                                        if isinstance(channel, dict) and channel.get("id"):
                                            channel_ids.add(channel.get("id"))
                                if "threads" in guild and isinstance(guild["threads"], list):
                                    for thread in guild["threads"]:
                                        if isinstance(thread, dict) and thread.get("id"):
                                            channel_ids.add(thread.get("id"))
                                            
                    # 3. Grab all private channels
                    private_channels = d.get("private_channels", [])
                    if isinstance(private_channels, list):
                        for pc in private_channels:
                            if isinstance(pc, dict) and pc.get("id"):
                                channel_ids.add(pc.get("id"))
                                
                print("  [WS] Successfully downloaded read states and channel lists.")
                ws.close()
        except Exception as e:
            print(f"  [WS] Exception in on_message: {e}")
            ws.close()

    def on_open(ws):
        payload = {
            "op": 2,
            "d": {
                "token": token,
                "capabilities": 16381,  # Discord's internal client capabilities bitfield (may need updating if READY stops arriving)
                "properties": {
                    "os": "Windows",
                    "browser": "Chrome",
                    "device": "",
                },
                "presence": {
                    "status": "unknown",
                    "since": 0,
                    "activities": [],
                    "afk": False,
                },
                "compress": False,
                "client_state": {"guild_versions": {}},
            },
        }
        ws.send(json.dumps(payload))

    def on_error(ws, error):
        print(f"  [WS] Error: {error}")
        try:
            ws.close()
        except Exception:
            pass

    ws = websocket.WebSocketApp(
        "wss://gateway.discord.gg/?v=9&encoding=json",
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
    )
    
    # Run in a thread to allow timeout
    wst = threading.Thread(target=ws.run_forever)
    wst.daemon = True
    wst.start()
    wst.join(timeout=10.0)
    
    if wst.is_alive():
        print("  [WS] Timeout waiting for READY event. Aborting connection.")
        ws.close()
        wst.join()
        raise RuntimeError("WebSocket connection timed out.")

    if not has_received_ready:
        raise RuntimeError("Failed to receive READY payload from WebSocket. Connection aborted or failed.")

    return list(channel_ids)


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
    except NetworkError as e:
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
                if "Cloudflare IP Ban" in text:
                    print(
                        "\n  ⚠  FATAL: Cloudflare has temporarily banned your IP. Aborting."
                    )
                    break
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
    except NetworkError as e:
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
                if "Cloudflare IP Ban" in text:
                    print(
                        "\n  ⚠  FATAL: Cloudflare has temporarily banned your IP. Aborting."
                    )
                    break
        except Exception as e:
            print(f"  ✗  Failed:  {display}  (Error: {e})")
            failed += 1

        time.sleep(REQUEST_DELAY)

    print(f"\nDone — removed {success}, failed {failed}.")


def mass_read_notifications(token: str) -> None:
    print("\nFetching your unread notifications…")
    
    try:
        channel_ids = _get_read_states(token)
    except RuntimeError as e:
        print(f"\n  ✗  {e}")
        return

    if not channel_ids:
        print("No channels found to mark as read.")
        return
        
    print(f"\nFound {len(channel_ids)} channel(s) to process.")
    
    confirm = input("Type 'yes' to mark ALL DMs and Servers as read: ").strip()
    if confirm.lower() != "yes":
        print("Cancelled.")
        return
    
    # Generate a Snowflake for the current time + 1 hour to ensure it's in the future
    # Discord epoch is 1420070400000
    current_time_ms = int(time.time() * 1000)
    future_ms = current_time_ms + 3600000
    massive_message_id = str((future_ms - 1420070400000) << 22)

    read_states_payload = []
    for c_id in channel_ids:
        read_states_payload.append({
            "channel_id": c_id,
            "message_id": massive_message_id,
            "read_state_type": 0
        })

    # Chunk the payload into batches of 100 to avoid payload size limits
    chunk_size = 100
    chunks = [read_states_payload[i:i + chunk_size] for i in range(0, len(read_states_payload), chunk_size)]
    
    success_count = 0
    fail_count = 0
    
    for i, chunk in enumerate(chunks):
        payload = {"read_states": chunk}
        print(f"\r  >  Sending bulk acknowledgment... ({i+1}/{len(chunks)} chunks)", end="", flush=True)
        try:
            r = _make_api_request("POST", "/read-states/ack-bulk", token, json=payload, quiet=True)
            if r.status_code in (200, 204):
                success_count += len(chunk)
            else:
                print(f"\n  ✗  Failed chunk (HTTP {r.status_code} - {get_clean_error(r)})")
                fail_count += len(chunk)
        except RuntimeError as e:
            if "Cloudflare IP Ban" in str(e):
                print("\n  ⚠  FATAL: Cloudflare has temporarily banned your IP. Aborting.")
                return
            print(f"  ✗  Runtime error: {e}")
            fail_count += len(chunk)
        except NetworkError as e:
            print(f"  ✗  Network error: {e}")
            fail_count += len(chunk)
        except Exception as e:
            print(f"  ✗  Failed: {e}")
            fail_count += len(chunk)
            
        if len(chunks) > 1:
            time.sleep(REQUEST_DELAY)

    print()  # newline after progress bar
    if fail_count == 0:
        print(f"  ✓  Success! All {success_count} notifications have been marked as read.")
    else:
        print(f"Done — marked read {success_count}, failed {fail_count}.")


def get_masked_input(prompt: str = "Paste token: ", mask: str = "*") -> str:
    """A cross-platform masked input that correctly handles Ctrl+C."""
    import sys
    if sys.platform == "win32":
        import msvcrt
        sys.stdout.write(prompt)
        sys.stdout.flush()
        entered = []
        while True:
            # getwch() handles Unicode characters properly on Windows
            key = ord(msvcrt.getwch())
            if key == 13:  # Enter
                sys.stdout.write("\n")
                return "".join(entered)
            elif key == 3:  # Ctrl+C
                raise KeyboardInterrupt()
            elif key in (8, 127):  # Backspace
                if len(entered) > 0:
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                    entered.pop()
            elif 0 <= key <= 31:
                pass
            else:
                sys.stdout.write(mask)
                sys.stdout.flush()
                entered.append(chr(key))
    else:
        import getpass
        return getpass.getpass(prompt)

def main() -> None:
    print("\n╔══════════════════════════════════════════╗")
    print("║   Discord Mass Account Cleanup Tool      ║")
    print("╚══════════════════════════════════════════╝\n")
    print("⚠  This uses your Discord user token, which is technically")
    print("   self-botting and against Discord's ToS. Low risk for")
    print("   a one-off cleanup, but use at your own discretion.\n")

    while True:
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
            token = get_masked_input("Paste token: ", mask="*").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nCancelled.")
                return

        if not token:
            print("No token entered. Exiting.")
            return

        if not check_token(token):
            print("\n✗  Invalid token — please double-check and try again.\n")
            continue

        while True:
            print("\nMain Menu:")
            print("  [1] Mass Leave Servers")
            print("  [2] Mass Remove Friends")
            print("  [3] Mass Read Notifications")
            print("  [t] Change Token / Switch Account")
            print("  [q] Quit\n")

            choice = input("Select an option > ").strip().lower()

            if choice == "1":
                mass_leave_servers(token)
            elif choice == "2":
                mass_remove_friends(token)
            elif choice == "3":
                mass_read_notifications(token)
            elif choice == "t":
                if "DISCORD_TOKEN" in os.environ:
                    del os.environ["DISCORD_TOKEN"]
                print("\nLogging out...")
                break
            elif choice == "q":
                print("Exiting...")
                return
            else:
                print("Invalid choice. Please select 1, 2, 3, t, or q.")


if __name__ == "__main__":
    main()
