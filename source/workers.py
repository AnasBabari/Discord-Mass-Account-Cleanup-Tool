import time
from PyQt5.QtCore import QThread, pyqtSignal
import discord_mass_cleanup as dmc

class LoginWorker(QThread):
    result_signal = pyqtSignal(bool, str, str) # success, message, token
    def __init__(self, token):
        super().__init__()
        self.token = token
    def run(self):
        if not self.token:
            self.result_signal.emit(False, "No token provided", self.token)
            return
        try:
            r = dmc._make_api_request("GET", "/users/@me", self.token, max_retries=1)
            if r.status_code == 401:
                self.result_signal.emit(False, "INVALID TOKEN", self.token)
                return
            r.raise_for_status()
            try:
                user = r.json()
            except ValueError:
                self.result_signal.emit(False, "Invalid response from Discord", self.token)
                return
            display = user.get("global_name") or user.get("username")
            username = user.get("username")
            self.result_signal.emit(True, f"{display} (@{username})", self.token)
        except Exception as e:
            self.result_signal.emit(False, str(e), self.token)

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

class FetchJoinedAtWorker(QThread):
    joined_at_signal = pyqtSignal(str, str)
    progress_signal = pyqtSignal(int, int)  # current, total
    finished_signal = pyqtSignal()
    def __init__(self, token, guilds):
        super().__init__()
        self.token = token
        self.guilds = guilds
    def run(self):
        total = len(self.guilds)
        for i, g in enumerate(self.guilds):
            try:
                r = dmc._make_api_request("GET", f"/users/@me/guilds/{g['id']}/member", self.token)
                if r.status_code == 200:
                    joined_at = r.json().get("joined_at", "")
                    self.joined_at_signal.emit(g['id'], joined_at)
                else:
                    self.joined_at_signal.emit(g['id'], "")
            except Exception:
                self.joined_at_signal.emit(g['id'], "")
            self.progress_signal.emit(i + 1, total)
            time.sleep(0.25)  # Read-only GET — safe with shorter delay
        self.finished_signal.emit()

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
            channel_ids = dmc._get_read_states(self.token)
            if not channel_ids:
                self.finished_signal.emit(0, 0, "No active channels found.")
                return
            self.progress_signal.emit(f"[*] Acknowledging {len(channel_ids)} channels...")
            
            current_time_ms = int(time.time() * 1000)
            future_ms = current_time_ms + 3600000
            massive_message_id = str((future_ms - 1420070400000) << 22)

            read_states_payload = [{"channel_id": c, "message_id": massive_message_id, "read_state_type": 0} for c in channel_ids]
            chunk_size = 100
            chunks = [read_states_payload[i:i + chunk_size] for i in range(0, len(read_states_payload), chunk_size)]
            
            success_count = 0
            fail_count = 0
            
            for chunk in chunks:
                try:
                    r = dmc._make_api_request("POST", "/read-states/ack-bulk", self.token, json={"read_states": chunk}, quiet=True)
                    if r.status_code in (200, 204):
                        success_count += len(chunk)
                    else:
                        fail_count += len(chunk)
                except Exception as e:
                    fail_count += len(chunk)
                    self.progress_signal.emit(f"[-] Chunk failed: {e}")
                time.sleep(dmc.REQUEST_DELAY)
                
            self.finished_signal.emit(success_count, fail_count, "")
        except Exception as e:
            self.finished_signal.emit(0, 0, str(e))
