import getpass
import logging
import sys
import time
from collections.abc import Callable
from typing import Any

from discord_cleanup.api.client import DEFAULT_API_CLIENT, DiscordApiClient
from discord_cleanup.api.exceptions import AuthenticationError, DiscordCleanupError
from discord_cleanup.gateway.notifications import DEFAULT_GATEWAY_READER, GatewayNotificationsReader
from discord_cleanup.models.domain import OperationPreview, OperationResult
from discord_cleanup.security.credentials import DEFAULT_CREDENTIAL_STORE

logger = logging.getLogger(__name__)


def parse_selection(user_input: str, total_items: int) -> list[int]:
    """Parse comma/space-separated numbers and hyphenated ranges into 0-based indices.

    Examples:
        "1, 3, 5"   -> [0, 2, 4]
        "1-3, 5"    -> [0, 1, 2, 4]
        "all"       -> [0, 1, ..., total_items - 1]
    """
    cleaned = user_input.strip().lower()
    if not cleaned:
        return []

    if cleaned == "all":
        return list(range(total_items))

    selected: set[int] = set()
    # Normalize commas, semicolons, and spaces into delimiters
    tokens = [tok.strip() for tok in cleaned.replace(";", ",").replace(" ", ",").split(",") if tok.strip()]

    for token in tokens:
        if "-" in token:
            parts = token.split("-")
            if len(parts) == 2:
                try:
                    start = int(parts[0]) - 1
                    end = int(parts[1]) - 1
                    if start <= end and start >= 0 and end < total_items:
                        selected.update(range(start, end + 1))
                except ValueError:
                    continue
        else:
            try:
                idx = int(token) - 1
                if 0 <= idx < total_items:
                    selected.add(idx)
            except ValueError:
                continue

    return sorted(selected)


def read_masked_chars(
    read_char: Callable[[], str],
    write_fn: Callable[[str], Any] = sys.stdout.write,
    flush_fn: Callable[[], Any] = sys.stdout.flush,
    prompt: str = "Paste token: ",
    mask: str = "*",
) -> str:
    """Read characters key-by-key with masking, supporting Backspace, Ctrl+C, and Enter."""
    write_fn(prompt)
    flush_fn()
    entered: list[str] = []
    while True:
        char = read_char()
        key = ord(char)
        if key in (0, 224):  # Special key prefix (e.g. arrow keys)
            try:
                read_char()
            except Exception:
                pass
            continue
        if key == 13:  # Enter
            write_fn("\n")
            return "".join(entered)
        if key == 3:  # Ctrl+C
            raise KeyboardInterrupt()
        if key in (8, 127):  # Backspace
            if entered:
                write_fn("\b \b")
                flush_fn()
                entered.pop()
        elif 0 <= key <= 31:  # Control characters
            pass
        else:
            write_fn(mask)
            flush_fn()
            entered.append(char)


def get_masked_input(prompt: str = "Paste token: ", mask: str = "*") -> str:
    """Read a masked token input cross-platform without echoing credentials."""
    if sys.platform == "win32":
        try:
            import msvcrt

            return read_masked_chars(
                read_char=msvcrt.getwch,
                prompt=prompt,
                mask=mask,
            )
        except ImportError:
            return getpass.getpass(prompt)
    else:
        return getpass.getpass(prompt)


def prompt_preview_and_confirmation(preview: OperationPreview) -> bool:
    """Display an explicit preview of targets and prompt for typed 'yes' confirmation."""
    print(f"\n--- Operation Preview: {preview.action_name} ---")
    print(f"Total Targets: {preview.target_count}")
    if preview.target_descriptions:
        print("Sample targets:")
        for desc in preview.target_descriptions[:8]:
            print(f"  - {desc}")
        if len(preview.target_descriptions) > 8:
            print(f"  ... and {len(preview.target_descriptions) - 8} more.")
    print("-" * 45)

    confirm = input(f"Type 'yes' to proceed with {preview.action_name}: ").strip()
    return confirm.lower() == "yes"


def run_servers_cleanup(client: DiscordApiClient) -> OperationResult:
    print("\nFetching servers...")
    try:
        guilds = client.get_guilds()
    except DiscordCleanupError as exc:
        print(f"Failed to fetch servers: {exc}")
        return OperationResult(errors=[str(exc)])

    leavable = [g for g in guilds if not g.owner]
    owned = [g for g in guilds if g.owner]

    print(f"Found {len(guilds)} server(s) ({len(leavable)} leavable, {len(owned)} owned).")
    if not leavable:
        print("No leavable servers found.")
        return OperationResult()

    print("\nLeavable servers:")
    for idx, guild in enumerate(leavable, 1):
        print(f"  [{idx}] {guild.name} (ID: {guild.id})")

    choice = input("\nEnter server numbers (e.g. 1, 3-5, all, or 'q' to cancel): ").strip()
    if choice.lower() == "q":
        print("Cancelled.")
        return OperationResult(cancelled=True)

    indices = parse_selection(choice, len(leavable))
    if not indices:
        print("No servers selected.")
        return OperationResult()

    selected = [leavable[i] for i in indices]
    preview = OperationPreview(
        action_name="Leave Servers",
        target_count=len(selected),
        target_descriptions=[f"{g.name} ({g.id})" for g in selected],
    )

    if not prompt_preview_and_confirmation(preview):
        print("Cancelled by user.")
        return OperationResult(cancelled=True)

    result = OperationResult()
    for idx, guild in enumerate(selected, 1):
        print(f"[{idx}/{len(selected)}] Leaving: {guild.name}...", end="", flush=True)
        status, err = client.leave_guild(guild.id)
        if status in (200, 204):
            result.success_count += 1
            print(" [OK]")
        else:
            result.failure_count += 1
            result.errors.append(f"{guild.name}: {err}")
            print(f" [FAILED: {err}]")
            if "IP Ban" in err:
                print("Upstream rate limit ban detected. Halting operation.")
                break

    print(f"\nCompleted: {result.success_count} left, {result.failure_count} failed.")
    return result


def run_friends_cleanup(client: DiscordApiClient) -> OperationResult:
    print("\nFetching friends...")
    try:
        friends = client.get_friends()
    except DiscordCleanupError as exc:
        print(f"Failed to fetch friends: {exc}")
        return OperationResult(errors=[str(exc)])

    if not friends:
        print("No friends found.")
        return OperationResult()

    print(f"\nFound {len(friends)} friend(s):")
    for idx, rel in enumerate(friends, 1):
        print(f"  [{idx}] {rel.user.display_name} ({rel.user.tag})")

    choice = input("\nEnter friend numbers to remove (e.g. 1, 3-5, all, or 'q' to cancel): ").strip()
    if choice.lower() == "q":
        print("Cancelled.")
        return OperationResult(cancelled=True)

    indices = parse_selection(choice, len(friends))
    if not indices:
        print("No friends selected.")
        return OperationResult()

    selected = [friends[i] for i in indices]
    preview = OperationPreview(
        action_name="Remove Friends",
        target_count=len(selected),
        target_descriptions=[f"{r.user.display_name} ({r.user.tag})" for r in selected],
    )

    if not prompt_preview_and_confirmation(preview):
        print("Cancelled by user.")
        return OperationResult(cancelled=True)

    result = OperationResult()
    for idx, rel in enumerate(selected, 1):
        print(f"[{idx}/{len(selected)}] Removing: {rel.user.display_name}...", end="", flush=True)
        status, err = client.remove_friend(rel.user.id)
        if status in (200, 204):
            result.success_count += 1
            print(" [OK]")
        else:
            result.failure_count += 1
            result.errors.append(f"{rel.user.display_name}: {err}")
            print(f" [FAILED: {err}]")
            if "IP Ban" in err:
                print("Upstream rate limit ban detected. Halting operation.")
                break

    print(f"\nCompleted: {result.success_count} removed, {result.failure_count} failed.")
    return result


def run_blocked_cleanup(client: DiscordApiClient) -> OperationResult:
    print("\nFetching blocked users...")
    try:
        blocked = client.get_blocked_users()
    except DiscordCleanupError as exc:
        print(f"Failed to fetch blocked users: {exc}")
        return OperationResult(errors=[str(exc)])

    if not blocked:
        print("No blocked users found.")
        return OperationResult()

    print(f"\nFound {len(blocked)} blocked user(s):")
    for idx, rel in enumerate(blocked, 1):
        print(f"  [{idx}] {rel.user.display_name} ({rel.user.tag})")

    choice = input("\nEnter numbers to unblock (e.g. 1, 3-5, all, or 'q' to cancel): ").strip()
    if choice.lower() == "q":
        print("Cancelled.")
        return OperationResult(cancelled=True)

    indices = parse_selection(choice, len(blocked))
    if not indices:
        print("No users selected.")
        return OperationResult()

    selected = [blocked[i] for i in indices]
    preview = OperationPreview(
        action_name="Unblock Users",
        target_count=len(selected),
        target_descriptions=[f"{r.user.display_name} ({r.user.tag})" for r in selected],
    )

    if not prompt_preview_and_confirmation(preview):
        print("Cancelled by user.")
        return OperationResult(cancelled=True)

    result = OperationResult()
    for idx, rel in enumerate(selected, 1):
        print(f"[{idx}/{len(selected)}] Unblocking: {rel.user.display_name}...", end="", flush=True)
        status, err = client.unblock_user(rel.user.id)
        if status in (200, 204):
            result.success_count += 1
            print(" [OK]")
        else:
            result.failure_count += 1
            result.errors.append(f"{rel.user.display_name}: {err}")
            print(f" [FAILED: {err}]")
            if "IP Ban" in err:
                print("Upstream rate limit ban detected. Halting operation.")
                break

    print(f"\nCompleted: {result.success_count} unblocked, {result.failure_count} failed.")
    return result


def run_notifications_cleanup(client: DiscordApiClient, gateway: GatewayNotificationsReader) -> OperationResult:
    print("\nConnecting to Gateway to fetch unread notification channels...")
    try:
        grouped = gateway.fetch_unread_channels(client.token)
    except Exception as exc:
        print(f"Failed to fetch read states: {exc}")
        return OperationResult(errors=[str(exc)])

    if not grouped:
        print("No unread notification channels found.")
        return OperationResult()

    total_unread = sum(len(channels) for channels in grouped.values())
    preview = OperationPreview(
        action_name="Mark All Notifications as Read",
        target_count=total_unread,
        target_descriptions=[f"{srv}: {len(chs)} channel(s)" for srv, chs in list(grouped.items())[:5]],
    )

    if not prompt_preview_and_confirmation(preview):
        print("Cancelled by user.")
        return OperationResult(cancelled=True)

    # Generate future snowflake
    current_ms = int(time.time() * 1000)
    future_ms = current_ms + 3600000
    future_message_id = str((future_ms - 1420070400000) << 22)

    all_chunks: list[tuple[str, list[dict]]] = []
    chunk_size = 100
    for server_name, channel_ids in grouped.items():
        payload = [{"channel_id": cid, "message_id": future_message_id, "read_state_type": 0} for cid in channel_ids]
        for i in range(0, len(payload), chunk_size):
            all_chunks.append((server_name, payload[i:i + chunk_size]))

    result = OperationResult()
    for idx, (srv_name, chunk) in enumerate(all_chunks, 1):
        print(f"[{idx}/{len(all_chunks)}] Acknowledging: {srv_name} ({len(chunk)} channels)...", end="", flush=True)
        status, err = client.ack_read_states_chunk(chunk)
        if status in (200, 204):
            result.success_count += len(chunk)
            print(" [OK]")
        else:
            result.failure_count += len(chunk)
            result.errors.append(f"{srv_name}: {err}")
            print(f" [FAILED: {err}]")
            if "IP Ban" in err:
                print("Upstream rate limit ban detected. Halting operation.")
                break

    print(f"\nCompleted: {result.success_count} notifications marked read, {result.failure_count} failed.")
    return result


def main() -> None:
    print("\n========================================================")
    print("        Discord Mass Account Cleanup Tool (CLI)         ")
    print("========================================================")
    print("Platform Policy Disclaimer:")
    print("  User-token automation violates Discord's Terms of Service.")
    print("  Use only on accounts you control at your own discretion.\n")

    client = DEFAULT_API_CLIENT
    gateway = DEFAULT_GATEWAY_READER
    credentials = DEFAULT_CREDENTIAL_STORE

    token = credentials.get_token()
    if token:
        print("Cached credential detected in OS Keyring. Verifying...")
        try:
            user = client.verify_token(token)
            client.set_token(token)
            print(f"Authenticated as: {user.display_name} ({user.tag})")
        except AuthenticationError:
            print("Cached token is invalid or expired.")
            credentials.delete_token()
            token = None

    while not token:
        try:
            entered_token = get_masked_input("Enter Discord user token: ")
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            return

        if not entered_token.strip():
            print("No token entered. Exiting.")
            return

        entered_token = entered_token.strip()
        try:
            user = client.verify_token(entered_token)
            client.set_token(entered_token)
            token = entered_token
            print(f"Authenticated successfully as: {user.display_name} ({user.tag})")
            save_choice = input("Save token to OS Keyring? [Y/n]: ").strip().lower()
            if save_choice != "n":
                credentials.save_token(entered_token)
        except AuthenticationError:
            print("Authentication failed: Invalid token.")
        except Exception as exc:
            print(f"Authentication error: {exc}")

    while True:
        print("\n--- Main Menu ---")
        print("  [1] Leave Servers")
        print("  [2] Remove Friends")
        print("  [3] Unblock Users")
        print("  [4] Mark Notifications Read")
        print("  [T] Switch Account / Logout")
        print("  [Q] Exit")

        try:
            choice = input("\nSelect an option: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if choice == "1":
            run_servers_cleanup(client)
        elif choice == "2":
            run_friends_cleanup(client)
        elif choice == "3":
            run_blocked_cleanup(client)
        elif choice == "4":
            run_notifications_cleanup(client, gateway)
        elif choice == "t":
            credentials.delete_token()
            client.clear_token()
            print("Logged out. Please restart or enter a new token.")
            return
        elif choice == "q":
            print("Exiting.")
            break
        else:
            print("Invalid option selected.")
