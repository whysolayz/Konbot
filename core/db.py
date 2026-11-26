"""SQLite data system: points, profiles, shop, purchases, audit log, per-guild settings.
Everything is stored in kon.db (WAL mode). Single connection, used from the event loop."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  points INTEGER NOT NULL DEFAULT 0,
  chat_total INTEGER NOT NULL DEFAULT 0,
  chat_progress INTEGER NOT NULL DEFAULT 0,
  vc_total INTEGER NOT NULL DEFAULT 0,
  vc_progress INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(guild_id, user_id));
CREATE INDEX IF NOT EXISTS idx_users_points ON users(guild_id, points DESC);
CREATE TABLE IF NOT EXISTS channel_progress(
  guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
  progress INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(guild_id, user_id, channel_id));
CREATE TABLE IF NOT EXISTS badges(
  guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, badge TEXT NOT NULL,
  PRIMARY KEY(guild_id, user_id, badge));
CREATE TABLE IF NOT EXISTS shop_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL,
  price INTEGER NOT NULL, stock INTEGER NOT NULL DEFAULT -1, emoji TEXT,
  UNIQUE(guild_id, role_id));
CREATE TABLE IF NOT EXISTS purchases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role_id INTEGER NOT NULL,
  price INTEGER NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS point_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  delta INTEGER NOT NULL, reason TEXT, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings(
  guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY(guild_id, key));
CREATE TABLE IF NOT EXISTS left_members(
  guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT '', roles TEXT NOT NULL DEFAULT '[]', left_ts REAL NOT NULL,
  PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS aura_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, ts REAL NOT NULL, value INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS aura_log_user ON aura_log(guild_id, user_id, id);
CREATE TABLE IF NOT EXISTS transfer_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  guild_id INTEGER NOT NULL, ts REAL NOT NULL, actor_id INTEGER NOT NULL,
  source_id INTEGER NOT NULL, source_name TEXT NOT NULL DEFAULT '', target_id INTEGER NOT NULL,
  roles TEXT NOT NULL DEFAULT '[]', left_member INTEGER NOT NULL DEFAULT 0, undone INTEGER NOT NULL DEFAULT 0);
"""


class DB:
    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.prune_logs()
        self.prune_left()

    def _migrate(self):
        """shop_items.position = order of the roles in the shop ($pointshop config -> Position)."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(shop_items)")]
        with self.conn:
            if "position" not in cols:
                self.conn.execute("ALTER TABLE shop_items ADD COLUMN position INTEGER")
            self.conn.execute(
                "UPDATE shop_items SET position=(SELECT COUNT(*) FROM shop_items s2 "
                "WHERE s2.guild_id=shop_items.guild_id AND s2.id<=shop_items.id) WHERE position IS NULL")

    # ---------- basics ----------
    def _ensure(self, gid, uid):
        self.conn.execute("INSERT OR IGNORE INTO users(guild_id,user_id) VALUES(?,?)", (gid, uid))

    def _log(self, gid, uid, delta, reason):
        self.conn.execute("INSERT INTO point_log(guild_id,user_id,delta,reason,ts) VALUES(?,?,?,?,?)",
                          (gid, uid, delta, reason, time.time()))

    def prune_logs(self, days=90):
        with self.conn:
            self.conn.execute("DELETE FROM point_log WHERE ts<?", (time.time() - days * 86400,))

    # ---------- members who left ($transfer <user id> @new) ----------
    def save_left(self, gid, uid, name, role_ids):
        """Remember the roles a member had when they left, so $transfer can hand them over later."""
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO left_members(guild_id,user_id,name,roles,left_ts) VALUES(?,?,?,?,?)",
                (gid, uid, name[:100], json.dumps([int(r) for r in role_ids]), time.time()))

    def get_left(self, gid, uid):
        r = self.conn.execute("SELECT * FROM left_members WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["roles"] = json.loads(d["roles"] or "[]")
        return d

    def list_left(self, gid, limit=15) -> list:
        rows = self.conn.execute("SELECT * FROM left_members WHERE guild_id=? ORDER BY left_ts DESC LIMIT ?",
                                 (gid, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["roles"] = json.loads(d["roles"] or "[]")
            out.append(d)
        return out

    # ---------- aura memory ($aura shows the last readings of a member) ----------
    AURA_KEEP = 24

    def log_aura(self, gid, uid, value: int):
        with self.conn:
            self.conn.execute("INSERT INTO aura_log(guild_id,user_id,ts,value) VALUES(?,?,?,?)", (gid, uid, time.time(), int(value)))
            self.conn.execute(
                "DELETE FROM aura_log WHERE guild_id=? AND user_id=? AND id NOT IN "
                "(SELECT id FROM aura_log WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?)",
                (gid, uid, gid, uid, self.AURA_KEEP))

    def aura_history(self, gid, uid, limit=12) -> list:
        """values, NEWEST first"""
        rows = self.conn.execute("SELECT value FROM aura_log WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
                                 (gid, uid, limit)).fetchall()
        return [r["value"] for r in rows]

    def aura_rank(self, gid, uid) -> tuple:
        """(this member's best reading, how many members have readings) - for 'best in the server'"""
        best = self.conn.execute("SELECT MAX(value) m FROM aura_log WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()["m"]
        n = self.conn.execute("SELECT COUNT(DISTINCT user_id) c FROM aura_log WHERE guild_id=?", (gid,)).fetchone()["c"]
        return best, n

    def find_left(self, gid, text: str, limit=5) -> list:
        """Leavers whose saved name contains `text` (so `$transfer nickname @new` works without the ID)."""
        like = "%" + text.replace("%", "").replace("_", "").lower() + "%"
        rows = self.conn.execute("SELECT * FROM left_members WHERE guild_id=? AND lower(name) LIKE ? "
                                 "ORDER BY left_ts DESC LIMIT ?", (gid, like, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["roles"] = json.loads(d["roles"] or "[]")
            out.append(d)
        return out

    def log_transfer(self, gid, actor, source_id, source_name, target_id, role_ids, left: bool) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO transfer_log(guild_id,ts,actor_id,source_id,source_name,target_id,roles,left_member) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (gid, time.time(), actor, source_id, source_name[:100], target_id,
                 json.dumps([int(r) for r in role_ids]), 1 if left else 0))
            return cur.lastrowid

    def mark_undone(self, log_id: int):
        with self.conn:
            self.conn.execute("UPDATE transfer_log SET undone=1 WHERE id=?", (log_id,))

    def transfer_history(self, gid, limit=10) -> list:
        rows = self.conn.execute("SELECT * FROM transfer_log WHERE guild_id=? ORDER BY ts DESC LIMIT ?",
                                 (gid, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["roles"] = json.loads(d["roles"] or "[]")
            out.append(d)
        return out

    def forget_left(self, gid, uid):
        """A member who comes back no longer needs a 'left' record."""
        with self.conn:
            self.conn.execute("DELETE FROM left_members WHERE guild_id=? AND user_id=?", (gid, uid))

    def prune_left(self, days=180):
        with self.conn:
            self.conn.execute("DELETE FROM left_members WHERE left_ts<?", (time.time() - days * 86400,))

    def close(self):
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

    def ping(self) -> None:
        self.conn.execute("SELECT 1").fetchone()

    def counts(self) -> dict:
        q = lambda t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return {"users": q("users"), "items": q("shop_items"), "purchases": q("purchases"), "settings": q("settings")}

    def snapshot(self, dest: str):
        """Consistent copy of the live database (used by $backup)."""
        out = sqlite3.connect(dest)
        try:
            self.conn.commit()
            self.conn.backup(out)
        finally:
            out.close()

    # ---------- users / points ----------
    def user(self, gid, uid) -> dict:
        r = self.conn.execute("SELECT * FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()
        if r:
            return dict(r)
        return {"guild_id": gid, "user_id": uid, "points": 0, "chat_total": 0, "chat_progress": 0,
                "vc_total": 0, "vc_progress": 0}

    def add_points(self, gid, uid, delta: int, reason="") -> int:
        with self.conn:
            self._ensure(gid, uid)
            cur = self.conn.execute("SELECT points FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()[0]
            new = max(0, cur + delta)
            self.conn.execute("UPDATE users SET points=? WHERE guild_id=? AND user_id=?", (new, gid, uid))
            self._log(gid, uid, new - cur, reason)
        return new

    def set_points(self, gid, uid, value: int) -> int:
        with self.conn:
            self._ensure(gid, uid)
            cur = self.conn.execute("SELECT points FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()[0]
            value = max(0, value)
            self.conn.execute("UPDATE users SET points=? WHERE guild_id=? AND user_id=?", (value, gid, uid))
            self._log(gid, uid, value - cur, "set")
        return value

    def reset_user(self, gid, uid):
        with self.conn:
            for t in ("users", "channel_progress", "badges"):
                self.conn.execute(f"DELETE FROM {t} WHERE guild_id=? AND user_id=?", (gid, uid))

    def bump_chat(self, gid, uid, cid, per: int, pts: int, use_channel: bool):
        """Count one message. Returns (points_awarded, progress)."""
        with self.conn:
            self._ensure(gid, uid)
            if use_channel:
                self.conn.execute(
                    "INSERT OR IGNORE INTO channel_progress(guild_id,user_id,channel_id) VALUES(?,?,?)", (gid, uid, cid))
                prog = self.conn.execute(
                    "SELECT progress FROM channel_progress WHERE guild_id=? AND user_id=? AND channel_id=?",
                    (gid, uid, cid)).fetchone()[0] + 1
            else:
                prog = self.conn.execute(
                    "SELECT chat_progress FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()[0] + 1
            awarded = 0
            if prog >= per:
                awarded = (prog // per) * pts
                prog %= per
            if use_channel:
                self.conn.execute(
                    "UPDATE channel_progress SET progress=?, total=total+1 WHERE guild_id=? AND user_id=? AND channel_id=?",
                    (prog, gid, uid, cid))
                self.conn.execute("UPDATE users SET chat_total=chat_total+1, points=points+? WHERE guild_id=? AND user_id=?",
                                  (awarded, gid, uid))
            else:
                self.conn.execute(
                    "UPDATE users SET chat_total=chat_total+1, chat_progress=?, points=points+? WHERE guild_id=? AND user_id=?",
                    (prog, awarded, gid, uid))
            if awarded:
                self._log(gid, uid, awarded, "chat")
        return awarded, prog

    def bump_vc_many(self, rows):
        """rows: (gid, uid, seconds, per_seconds, points)"""
        with self.conn:
            for gid, uid, secs, per, pts in rows:
                self._ensure(gid, uid)
                prog = self.conn.execute("SELECT vc_progress FROM users WHERE guild_id=? AND user_id=?",
                                         (gid, uid)).fetchone()[0] + secs
                awarded = 0
                if prog >= per:
                    awarded = (prog // per) * pts
                    prog %= per
                self.conn.execute(
                    "UPDATE users SET vc_total=vc_total+?, vc_progress=?, points=points+? WHERE guild_id=? AND user_id=?",
                    (secs, prog, awarded, gid, uid))
                if awarded:
                    self._log(gid, uid, awarded, "vc")

    def leaderboard(self, gid, limit=10):
        return self.conn.execute(
            "SELECT * FROM users WHERE guild_id=? AND (points>0 OR chat_total>0) ORDER BY points DESC, chat_total DESC LIMIT ?",
            (gid, limit)).fetchall()

    def leaderboard_page(self, gid, limit=5, offset=0):
        return self.conn.execute(
            "SELECT * FROM users WHERE guild_id=? AND (points>0 OR chat_total>0) ORDER BY points DESC, chat_total DESC LIMIT ? OFFSET ?",
            (gid, limit, offset)).fetchall()

    def leaderboard_count(self, gid) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM users WHERE guild_id=? AND (points>0 OR chat_total>0)", (gid,)).fetchone()[0]

    # ---------- leaderboard by stat (messages / points / voice) ----------
    LB_COLS = {"messages": "chat_total", "points": "points", "voice": "vc_total"}

    def lb_rows(self, gid, stat="messages", limit=10, offset=0):
        col = self.LB_COLS.get(stat, "chat_total")
        return self.conn.execute(
            f"SELECT * FROM users WHERE guild_id=? AND {col}>0 ORDER BY {col} DESC, points DESC, user_id LIMIT ? OFFSET ?",
            (gid, limit, offset)).fetchall()

    def lb_count(self, gid, stat="messages") -> int:
        col = self.LB_COLS.get(stat, "chat_total")
        return self.conn.execute(f"SELECT COUNT(*) FROM users WHERE guild_id=? AND {col}>0", (gid,)).fetchone()[0]

    def lb_position(self, gid, uid, stat="messages"):
        """1-based place of a member in that leaderboard, or None when they have no value yet."""
        col = self.LB_COLS.get(stat, "chat_total")
        me = self.conn.execute(f"SELECT {col} v, points p FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()
        if me is None or me["v"] <= 0:
            return None
        return self.conn.execute(
            f"SELECT COUNT(*)+1 FROM users WHERE guild_id=? AND {col}>0 AND ({col}>? OR ({col}=? AND (points>? OR (points=? AND user_id<?))))",
            (gid, me["v"], me["v"], me["p"], me["p"], uid)).fetchone()[0]

    def rank(self, gid, uid) -> int:
        return self.conn.execute(
            "SELECT COUNT(*)+1 FROM users WHERE guild_id=? AND points > COALESCE((SELECT points FROM users WHERE guild_id=? AND user_id=?),0)",
            (gid, gid, uid)).fetchone()[0]

    # ---------- badges ----------
    def badges(self, gid, uid) -> list:
        return [r[0] for r in self.conn.execute(
            "SELECT badge FROM badges WHERE guild_id=? AND user_id=? ORDER BY rowid", (gid, uid))]

    def add_badge(self, gid, uid, badge) -> bool:
        with self.conn:
            cur = self.conn.execute("INSERT OR IGNORE INTO badges VALUES(?,?,?)", (gid, uid, badge))
        return cur.rowcount > 0

    def remove_badge(self, gid, uid, badge) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM badges WHERE guild_id=? AND user_id=? AND badge=? COLLATE NOCASE",
                                    (gid, uid, badge))
        return cur.rowcount > 0

    def clear_badges(self, gid, uid) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM badges WHERE guild_id=? AND user_id=?", (gid, uid))
        return cur.rowcount

    def repair_badges(self) -> int:
        """Old versions cut badges at 30 characters, which broke custom emojis ('<:name:123' without the '>').
        Close them again so they show as emojis."""
        import re
        broken = re.compile(r"<a?:\w+:\d+")
        n = 0
        with self.conn:
            for gid, uid, badge in self.conn.execute("SELECT guild_id, user_id, badge FROM badges").fetchall():
                if broken.fullmatch(badge):
                    self.conn.execute("UPDATE OR IGNORE badges SET badge=? WHERE guild_id=? AND user_id=? AND badge=?",
                                      (badge + ">", gid, uid, badge))
                    self.conn.execute("DELETE FROM badges WHERE guild_id=? AND user_id=? AND badge=?", (gid, uid, badge))
                    n += 1
        return n

    # ---------- shop ----------
    def shop_list(self, gid) -> list:
        return [dict(r) for r in self.conn.execute("SELECT * FROM shop_items WHERE guild_id=? ORDER BY position, id", (gid,))]

    def shop_get(self, gid, item_id):
        r = self.conn.execute("SELECT * FROM shop_items WHERE guild_id=? AND id=?", (gid, item_id)).fetchone()
        return dict(r) if r else None

    def shop_upsert(self, gid, role_id, price, stock, emoji) -> int:
        with self.conn:
            self.conn.execute(
                "INSERT INTO shop_items(guild_id,role_id,price,stock,emoji,position) VALUES(?,?,?,?,?,"
                "(SELECT COALESCE(MAX(position),0)+1 FROM shop_items WHERE guild_id=?)) "
                "ON CONFLICT(guild_id,role_id) DO UPDATE SET price=excluded.price, stock=excluded.stock, emoji=excluded.emoji",
                (gid, role_id, price, stock, emoji, gid))
            return self.conn.execute("SELECT id FROM shop_items WHERE guild_id=? AND role_id=?", (gid, role_id)).fetchone()[0]

    def shop_update(self, gid, item_id, price, stock, emoji):
        with self.conn:
            self.conn.execute("UPDATE shop_items SET price=?, stock=?, emoji=? WHERE guild_id=? AND id=?",
                              (price, stock, emoji, gid, item_id))

    def shop_move(self, gid, item_id, new_pos: int) -> int:
        """Move an item to position new_pos (1 = first). Returns the position it ended up at."""
        ids = [it["id"] for it in self.shop_list(gid)]
        if item_id not in ids:
            return 0
        ids.remove(item_id)
        idx = max(0, min(len(ids), new_pos - 1))
        ids.insert(idx, item_id)
        with self.conn:
            for n, i in enumerate(ids, 1):
                self.conn.execute("UPDATE shop_items SET position=? WHERE guild_id=? AND id=?", (n, gid, i))
        return idx + 1

    def shop_remove(self, gid, item_id):
        with self.conn:
            self.conn.execute("DELETE FROM shop_items WHERE guild_id=? AND id=?", (gid, item_id))

    def buy(self, gid, uid, item_id):
        """Atomic purchase. Returns (status, item, balance). status: ok|missing|soldout|poor"""
        with self.conn:
            it = self.conn.execute("SELECT * FROM shop_items WHERE guild_id=? AND id=?", (gid, item_id)).fetchone()
            if not it:
                return "missing", None, 0
            it = dict(it)
            self._ensure(gid, uid)
            bal = self.conn.execute("SELECT points FROM users WHERE guild_id=? AND user_id=?", (gid, uid)).fetchone()[0]
            if it["stock"] == 0:
                return "soldout", it, bal
            if bal < it["price"]:
                return "poor", it, bal
            self.conn.execute("UPDATE users SET points=points-? WHERE guild_id=? AND user_id=?", (it["price"], gid, uid))
            if it["stock"] > 0:
                self.conn.execute("UPDATE shop_items SET stock=stock-1 WHERE id=?", (item_id,))
            self.conn.execute("INSERT INTO purchases(guild_id,user_id,role_id,price,ts) VALUES(?,?,?,?,?)",
                              (gid, uid, it["role_id"], it["price"], time.time()))
            self._log(gid, uid, -it["price"], f"shop:{it['role_id']}")
            return "ok", it, bal - it["price"]

    def refund(self, gid, uid, item: dict):
        """Undo a purchase (role could not be given)."""
        with self.conn:
            self.conn.execute("UPDATE users SET points=points+? WHERE guild_id=? AND user_id=?", (item["price"], gid, uid))
            if item["stock"] > 0:
                self.conn.execute("UPDATE shop_items SET stock=stock+1 WHERE id=?", (item["id"],))
            self.conn.execute(
                "DELETE FROM purchases WHERE id=(SELECT MAX(id) FROM purchases WHERE guild_id=? AND user_id=? AND role_id=?)",
                (gid, uid, item["role_id"]))
            self._log(gid, uid, item["price"], "shop refund")

    # ---------- settings (json per guild+key) ----------
    def get_settings(self, gid, key) -> dict:
        r = self.conn.execute("SELECT value FROM settings WHERE guild_id=? AND key=?", (gid, key)).fetchone()
        try:
            return json.loads(r[0]) if r else {}
        except Exception:
            return {}

    def set_settings(self, gid, key, value: dict):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings(guild_id,key,value) VALUES(?,?,?)",
                              (gid, key, json.dumps(value)))
