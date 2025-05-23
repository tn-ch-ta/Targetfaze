# session_manager.py

import json
import os
import base58
from solders.keypair import Keypair

SESSION_FILE = "session_data.json"

class UserSession:
    def __init__(self, private_key=None, sol_amount=0.01, sniping=False):
        self.private_key = private_key
        self.sol_amount = sol_amount
        self.sniping = sniping
        self.keypair = None

        if private_key:
            self.set_private_key(private_key)

    def set_private_key(self, base58_key: str):
        try:
            raw = base58.b58decode(base58_key)
            self.keypair = Keypair.from_bytes(raw[:64])
            self.private_key = base58_key
            return True
        except Exception as e:
            print(f"[KEY ERROR] Invalid base58 key: {e}")
            return False

    def get_public_key(self):
        if self.keypair:
            return str(self.keypair.pubkey())
        return "Not Set"

    def masked_wallet(self):
        pub = self.get_public_key()
        return pub[:4] + "..." + pub[-4:] if pub != "Not Set" else pub

    def to_dict(self):
        return {
            "private_key": self.private_key,
            "sol_amount": self.sol_amount,
            "sniping": self.sniping
        }

    @staticmethod
    def from_dict(data):
        return UserSession(
            private_key=data.get("private_key"),
            sol_amount=data.get("sol_amount", 0.01),
            sniping=data.get("sniping", False)
        )


# -- SESSION STORE --

class SessionStore:
    def __init__(self, path=SESSION_FILE):
        self.path = path
        self.sessions = {}  # user_id: UserSession
        self.load()

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    raw = json.load(f)
                    for uid, sess_data in raw.items():
                        self.sessions[int(uid)] = UserSession.from_dict(sess_data)
            except Exception as e:
                print(f"[LOAD ERROR] Could not load session file: {e}")

    def save(self):
        try:
            data = {uid: sess.to_dict() for uid, sess in self.sessions.items()}
            with open(self.path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[SAVE ERROR] Could not save session file: {e}")

    def get(self, uid):
        if uid not in self.sessions:
            self.sessions[uid] = UserSession()
        return self.sessions[uid]

    def update(self, uid, session: UserSession):
        self.sessions[uid] = session
        self.save()