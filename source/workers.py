import time
from PyQt5.QtCore import QThread, pyqtSignal
import discord_mass_cleanup as dmc

class LoginWorker(QThread):
    result_signal = pyqtSignal(bool, str, str, str, bytes, bool) # success, message, raw_username, token, avatar_bytes, save
    def __init__(self, token, save=True):
        super().__init__()
        self.token = token
        self.save = save
    def run(self):
        if not self.token:
            self.result_signal.emit(False, "No token provided", "", self.token, b"", self.save)
            return
        try:
            r = dmc._make_api_request("GET", "/users/@me", self.token, max_retries=1)
            if r.status_code == 401:
                self.result_signal.emit(False, "INVALID TOKEN", "", self.token, b"", self.save)
                return
            r.raise_for_status()
            try:
                user = r.json()
            except ValueError:
                self.result_signal.emit(False, "Invalid response from Discord", "", self.token, b"", self.save)
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
                    import requests
                    av_r = requests.get(avatar_url, timeout=10)
                    if av_r.status_code == 200:
                        avatar_bytes = av_r.content
                except Exception:
                    pass

            self.result_signal.emit(True, display, username, self.token, avatar_bytes, self.save)
        except Exception as e:
            self.result_signal.emit(False, str(e), "", self.token, b"", self.save)

class FetchServersWorker(QThread):
    result_signal = pyqtSignal(list, str)
    def __init__(self, token):
        super().__init__()
        self.token = token
    def run(self):
        if not self.token:
            self.result_signal.emit([], "No token provided")
            return
        try:
            guilds = dmc.get_guilds(self.token)
            self.result_signal.emit(guilds, "")
        except Exception as e:
            self.result_signal.emit([], str(e))



class FetchFriendsWorker(QThread):
    result_signal = pyqtSignal(list, str)
    def __init__(self, token):
        super().__init__()
        self.token = token
    def run(self):
        if not self.token:
            self.result_signal.emit([], "No token provided")
            return
        try:
            friends = dmc.get_friends(self.token)
            self.result_signal.emit(friends, "")
        except Exception as e:
            self.result_signal.emit([], str(e))

class RemoveFriendsWorker(QThread):
    progress_signal = pyqtSignal(int, str) # current_count, log_msg
    finished_signal = pyqtSignal(int, int) # success, failed
    def __init__(self, token, friends_to_remove):
        super().__init__()
        self.token = token
        self.friends_to_remove = friends_to_remove
    def run(self):
        success = 0
        failed = 0
        for i, f in enumerate(self.friends_to_remove):
            display = f["user"].get("global_name") or f["user"].get("username", "Unknown")
            try:
                status, text = dmc.remove_friend(self.token, f["id"])
                if status == 204:
                    success += 1
                    self.progress_signal.emit(i+1, f"[+] REMOVED: {display}")
                else:
                    failed += 1
                    self.progress_signal.emit(i+1, f"[-] FAILED: {display} ({text})")
            except Exception as e:
                failed += 1
                self.progress_signal.emit(i+1, f"[-] FAILED: {display} ({e})")
            time.sleep(dmc.REQUEST_DELAY)
        self.finished_signal.emit(success, failed)

class BlockUsersWorker(QThread):
    progress_signal = pyqtSignal(int, str)
    finished_signal = pyqtSignal(int, int)
    def __init__(self, token, users_to_block):
        super().__init__()
        self.token = token
        self.users_to_block = users_to_block
    def run(self):
        success = 0
        failed = 0
        for i, u in enumerate(self.users_to_block):
            display = u["user"].get("global_name") or u["user"].get("username", "Unknown")
            try:
                status, text = dmc.block_user(self.token, u["id"])
                if status == 204:
                    success += 1
                    self.progress_signal.emit(i+1, f"[+] BLOCKED: {display}")
                else:
                    failed += 1
                    self.progress_signal.emit(i+1, f"[-] FAILED: {display} ({text})")
            except Exception as e:
                failed += 1
                self.progress_signal.emit(i+1, f"[-] FAILED: {display} ({e})")
            time.sleep(dmc.REQUEST_DELAY)
        self.finished_signal.emit(success, failed)

class FetchBlockedWorker(QThread):
    result_signal = pyqtSignal(list, str)
    def __init__(self, token):
        super().__init__()
        self.token = token
    def run(self):
        if not self.token:
            self.result_signal.emit([], "No token provided")
            return
        try:
            blocked = dmc.get_blocked_users(self.token)
            self.result_signal.emit(blocked, "")
        except Exception as e:
            self.result_signal.emit([], str(e))

class UnblockUsersWorker(QThread):
    progress_signal = pyqtSignal(int, str)
    finished_signal = pyqtSignal(int, int)
    def __init__(self, token, users_to_unblock):
        super().__init__()
        self.token = token
        self.users_to_unblock = users_to_unblock
    def run(self):
        success = 0
        failed = 0
        for i, u in enumerate(self.users_to_unblock):
            display = u.get("name", "Unknown")
            try:
                status, text = dmc.unblock_user(self.token, u["id"])
                if status == 204:
                    success += 1
                    self.progress_signal.emit(i+1, f"[+] UNBLOCKED: {display}")
                else:
                    failed += 1
                    self.progress_signal.emit(i+1, f"[-] FAILED: {display} ({text})")
            except Exception as e:
                failed += 1
                self.progress_signal.emit(i+1, f"[-] FAILED: {display} ({e})")
            time.sleep(dmc.REQUEST_DELAY)
        self.finished_signal.emit(success, failed)

class LeaveServersWorker(QThread):
    progress_signal = pyqtSignal(int, str)
    finished_signal = pyqtSignal(int, int)
    def __init__(self, token, servers_to_leave):
        super().__init__()
        self.token = token
        self.servers_to_leave = servers_to_leave
    def run(self):
        success = 0
        failed = 0
        for i, g in enumerate(self.servers_to_leave):
            try:
                status, text = dmc.leave_guild(self.token, g["id"])
                if status == 204:
                    success += 1
                    self.progress_signal.emit(i+1, f"[+] LEFT: {g['name']}")
                else:
                    failed += 1
                    self.progress_signal.emit(i+1, f"[-] FAILED: {g['name']} ({text})")
            except Exception as e:
                failed += 1
                self.progress_signal.emit(i+1, f"[-] FAILED: {g['name']} ({e})")
            time.sleep(dmc.REQUEST_DELAY)
        self.finished_signal.emit(success, failed)

class ReadNotifsWorker(QThread):
    progress_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(int, int, str)
    def __init__(self, token):
        super().__init__()
        self.token = token
    def run(self):
        try:
            grouped_channels = dmc._get_read_states(self.token)
            if not grouped_channels:
                self.finished_signal.emit(0, 0, "No unread channels found.")
                return
            
            total_unread = sum(len(c) for c in grouped_channels.values())
            if total_unread == 0:
                self.finished_signal.emit(0, 0, "")
                return
                
            self.progress_signal.emit(f"[*] Found {total_unread} unread channels across {len(grouped_channels)} servers/DMs.")
            
            current_time_ms = int(time.time() * 1000)
            future_ms = current_time_ms + 3600000
            massive_message_id = str((future_ms - 1420070400000) << 22)

            success_count = 0
            fail_count = 0
            
            for server_name, channel_ids in grouped_channels.items():
                if not channel_ids:
                    continue
                
                self.progress_signal.emit(f"[*] Marking {server_name} as read...")
                read_states_payload = [{"channel_id": c, "message_id": massive_message_id, "read_state_type": 0} for c in channel_ids]
                
                chunk_size = 100
                chunks = [read_states_payload[i:i + chunk_size] for i in range(0, len(read_states_payload), chunk_size)]
                
                for chunk in chunks:
                    try:
                        r = dmc._make_api_request("POST", "/read-states/ack-bulk", self.token, json={"read_states": chunk}, quiet=True)
                        if r.status_code in (200, 204):
                            success_count += len(chunk)
                        else:
                            fail_count += len(chunk)
                    except Exception as e:
                        fail_count += len(chunk)
                        self.progress_signal.emit(f"[-] Chunk failed for {server_name}: {e}")
                    time.sleep(dmc.REQUEST_DELAY)
                
            self.finished_signal.emit(success_count, fail_count, "")
        except Exception as e:
            self.finished_signal.emit(0, 0, str(e))
