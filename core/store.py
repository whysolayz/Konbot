import json
import os
import time
import asyncio


class Store:
    """Tiny JSON database (atomic writes, debounced saves)."""

    def __init__(self, path):
        self.path = str(path)
        self.data = {"guilds": {}, "vcs": {}, "mutes": {}, "sync_hash": "", "backup": {}, "extra_owners": [], "guild_owners": {}, "selfroles": {}, "vc_setup": {}}
        self._pending = False

    def load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    for k, v in d.items():          # every key stays (a key this version doesn't know is kept and saved again)
                        self.data[k] = v
            except Exception as e:
                print("Store load error:", e)

    def save_soon(self):
        if self._pending:
            return
        self._pending = True
        try:
            asyncio.get_running_loop().call_later(1.0, self.flush)
        except RuntimeError:
            self.flush()

    def flush(self):
        self._pending = False
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    # --- guild config ---
    def guild(self, gid: int) -> dict:
        return self.data["guilds"].setdefault(str(gid), {})

    # --- per-server owners ($konextra) ---
    def owners(self, gid: int) -> list:
        """Extra owners of ONE server. An owner in server A has no powers in server B."""
        return self.data.setdefault("guild_owners", {}).setdefault(str(gid), [])

    # --- custom VCs ---
    def vc(self, cid: int):
        return self.data["vcs"].get(str(cid))

    def add_vc(self, cid: int, gid: int, owner_id: int) -> dict:
        rec = {"guild": gid, "owner": owner_id, "banned": [], "block_drags": False, "created": time.time()}
        self.data["vcs"][str(cid)] = rec
        self.save_soon()
        return rec

    def remove_vc(self, cid: int):
        if self.data["vcs"].pop(str(cid), None) is not None:
            self.save_soon()
