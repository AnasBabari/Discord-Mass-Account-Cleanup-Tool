import time
import threading
from PyQt5.QtCore import QThread, pyqtSignal
import discord_mass_cleanup as dmc


class CancellableTokenWorker(QThread):
    """Base worker with cooperative cancellation and prompt token scrubbing."""

    def __init__(self, token):
        super().__init__()
        self.token = token
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancel_event.set()
        self.token = ""

    def is_cancelled(self):
        return self._cancel_event.is_set()

    def scrub_token(self):
        self.token = ""


class LoginWorker(CancellableTokenWorker):
    # Signals: success, message, raw_username, token, avatar_bytes, save
    result_signal = pyqtSignal(bool, str, str, str, bytes, bool)

    def __init__(self, token, save=True):
        super().__init__(token)
        self.save = save

    def run(self):
        token = self.token
        if not token or self.is_cancelled():
            self.result_signal.emit(False, "No token provided", "", "", b"", self.save)
            return
        try:
            r = dmc._make_api_request("GET", "/users/@me", token, max_retries=2, cancel_event=self._cancel_event)
            if r.status_code == 401:
                self.result_signal.emit(False, "INVALID TOKEN", "", "", b"", self.save)
                return
            r.raise_for_status()
            try:
                user = r.json()
            except ValueError:
                self.result_signal.emit(False, "Invalid response from Discord", "", "", b"", self.save)
                return
            display = user.get("global_name") or user.get("username")
            username = user.get("username")
            user_id = user.get("id")
            avatar_hash = user.get("avatar")

            avatar_bytes = b""
            if user_id:
                if avatar_hash:
                    avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=64"
                else:
                    try:
                        index = (int(user_id) >> 22) % 6
                    except Exception:
                        index = 0
                    avatar_url = f"https://cdn.discordapp.com/embed/avatars/{index}.png"

                try:
                    client = dmc.HTTP_TRANSPORT
                    if dmc.HAS_CURL_CFFI and client is dmc.curl_requests:
                        av_r = client.get(avatar_url, timeout=10, impersonate="chrome110")
                    else:
                        av_r = client.get(avatar_url, timeout=10)
                    if av_r.status_code == 200:
                        avatar_bytes = av_r.content
                except Exception:
                    pass

            if self.is_cancelled():
                return
            self.result_signal.emit(True, display, username, token, avatar_bytes, self.save)
        except Exception as e:
            if not self.is_cancelled():
                self.result_signal.emit(False, f"NETWORK ERROR: {e}", "", "", b"", self.save)
        finally:
            self.scrub_token()


class FetchServersWorker(CancellableTokenWorker):
    result_signal = pyqtSignal(list, str)

    def __init__(self, token):
        super().__init__(token)

    def run(self):
        if not self.token or self.is_cancelled():
            self.result_signal.emit([], "No token provided")
            return
        try:
            guilds = dmc.get_guilds(self.token, cancel_event=self._cancel_event)
            self.result_signal.emit(guilds, "")
        except Exception as e:
            self.result_signal.emit([], str(e))
        finally:
            self.scrub_token()


class FetchFriendsWorker(CancellableTokenWorker):
    result_signal = pyqtSignal(list, str)

    def __init__(self, token):
        super().__init__(token)

    def run(self):
        if not self.token or self.is_cancelled():
            self.result_signal.emit([], "No token provided")
            return
        try:
            friends = dmc.get_friends(self.token, cancel_event=self._cancel_event)
            self.result_signal.emit(friends, "")
        except Exception as e:
            self.result_signal.emit([], str(e))
        finally:
            self.scrub_token()


class FetchBlockedWorker(CancellableTokenWorker):
    result_signal = pyqtSignal(list, str)

    def __init__(self, token):
        super().__init__(token)

    def run(self):
        if not self.token or self.is_cancelled():
            self.result_signal.emit([], "No token provided")
            return
        try:
            blocked = dmc.get_blocked_users(self.token, cancel_event=self._cancel_event)
            self.result_signal.emit(blocked, "")
        except Exception as e:
            self.result_signal.emit([], str(e))
        finally:
            self.scrub_token()


class BatchActionWorker(CancellableTokenWorker):
    """Unified runner for batch bulk mutation actions (friends, servers, blocks)."""
    progress_signal = pyqtSignal(int, str)  # current_count, log_msg
    finished_signal = pyqtSignal(int, int)  # success, failed

    def __init__(self, token, items, action_name="PROCESSED"):
        super().__init__(token)
        self.items = items
        self.action_name = action_name

    def get_item_id(self, item):
        return item.get("id")

    def get_item_display(self, item):
        user = item.get("user")
        if isinstance(user, dict):
            return user.get("global_name") or user.get("username") or "Unknown"
        return item.get("name") or str(item.get("id", "Unknown"))

    def execute_action(self, token, item_id, cancel_event):
        raise NotImplementedError

    def run(self):
        success = 0
        failed = 0
        for i, item in enumerate(self.items):
            if self.is_cancelled():
                break
            display = self.get_item_display(item)
            item_id = self.get_item_id(item)
            try:
                status, text = self.execute_action(self.token, item_id, cancel_event=self._cancel_event)
                if status == 204:
                    success += 1
                    self.progress_signal.emit(i + 1, f"[+] {self.action_name}: {display}")
                elif status == 401:
                    failed += 1
                    self.progress_signal.emit(i + 1, f"[-] FAILED: {display} (401 Unauthorized - Invalid Token)")
                    break
                else:
                    failed += 1
                    self.progress_signal.emit(i + 1, f"[-] FAILED: {display} ({text})")
            except dmc.RequestCancelled:
                break
            except Exception as e:
                failed += 1
                self.progress_signal.emit(i + 1, f"[-] FAILED: {display} ({e})")
                if "Cloudflare IP Ban" in str(e):
                    break
        self.finished_signal.emit(success, failed)
        self.scrub_token()


class RemoveFriendsWorker(BatchActionWorker):
    def __init__(self, token, friends_to_remove):
        super().__init__(token, friends_to_remove, action_name="REMOVED")
        self.friends_to_remove = friends_to_remove

    def execute_action(self, token, item_id, cancel_event):
        return dmc.remove_friend(token, item_id, cancel_event=cancel_event)


class BlockUsersWorker(BatchActionWorker):
    def __init__(self, token, users_to_block):
        super().__init__(token, users_to_block, action_name="BLOCKED")
        self.users_to_block = users_to_block

    def execute_action(self, token, item_id, cancel_event):
        return dmc.block_user(token, item_id, cancel_event=cancel_event)


class UnblockUsersWorker(BatchActionWorker):
    def __init__(self, token, users_to_unblock):
        super().__init__(token, users_to_unblock, action_name="UNBLOCKED")
        self.users_to_unblock = users_to_unblock

    def execute_action(self, token, item_id, cancel_event):
        return dmc.unblock_user(token, item_id, cancel_event=cancel_event)


class LeaveServersWorker(BatchActionWorker):
    def __init__(self, token, servers_to_leave):
        super().__init__(token, servers_to_leave, action_name="LEFT")
        self.servers_to_leave = servers_to_leave

    def execute_action(self, token, item_id, cancel_event):
        return dmc.leave_guild(token, item_id, cancel_event=cancel_event)


class ReadNotifsWorker(CancellableTokenWorker):
    progress_signal = pyqtSignal(str)
    chunk_progress_signal = pyqtSignal(int, int)  # current_chunk, total_chunks
    finished_signal = pyqtSignal(int, int, str)

    def __init__(self, token):
        super().__init__(token)

    def run(self):
        try:
            grouped_channels = dmc._get_read_states(self.token, cancel_event=self._cancel_event)
            if not grouped_channels:
                self.finished_signal.emit(0, 0, "No unread channels found.")
                return

            total_unread = sum(len(c) for c in grouped_channels.values())
            if total_unread == 0:
                self.finished_signal.emit(0, 0, "")
                return

            self.progress_signal.emit(
                f"[*] Found {total_unread} unread channels across {len(grouped_channels)} servers/DMs."
            )

            current_time_ms = int(time.time() * 1000)
            future_ms = current_time_ms + 3600000
            massive_message_id = str((future_ms - 1420070400000) << 22)

            all_chunks = []
            chunk_size = 100
            for server_name, channel_ids in grouped_channels.items():
                if not channel_ids:
                    continue
                read_states_payload = [
                    {"channel_id": c, "message_id": massive_message_id, "read_state_type": 0}
                    for c in channel_ids
                ]
                chunks = [
                    read_states_payload[i:i + chunk_size]
                    for i in range(0, len(read_states_payload), chunk_size)
                ]
                for chunk in chunks:
                    all_chunks.append((server_name, chunk))

            total_chunks = len(all_chunks)
            success_count = 0
            fail_count = 0

            for idx, (server_name, chunk) in enumerate(all_chunks, 1):
                if self.is_cancelled():
                    self.finished_signal.emit(success_count, fail_count, "Cancelled")
                    return
                self.progress_signal.emit(f"[*] Marking {server_name} as read... ({idx}/{total_chunks})")
                self.chunk_progress_signal.emit(idx, total_chunks)
                try:
                    r = dmc._make_api_request(
                        "POST", "/read-states/ack-bulk", self.token,
                        json={"read_states": chunk}, quiet=True, cancel_event=self._cancel_event
                    )
                    if r.status_code in (200, 204):
                        success_count += len(chunk)
                    else:
                        fail_count += len(chunk)
                except dmc.RequestCancelled:
                    self.finished_signal.emit(success_count, fail_count, "Cancelled")
                    return
                except Exception as e:
                    fail_count += len(chunk)
                    self.progress_signal.emit(f"[-] Chunk failed for {server_name}: {e}")
                    if "Cloudflare IP Ban" in str(e):
                        self.finished_signal.emit(success_count, fail_count, "Aborted due to Cloudflare IP Ban")
                        return

            self.finished_signal.emit(success_count, fail_count, "")
        except Exception as e:
            self.finished_signal.emit(0, 0, str(e))
        finally:
            self.scrub_token()
