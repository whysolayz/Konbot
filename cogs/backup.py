"""Webhook backup - TWO zips in one small message.

  kon_code.zip   code, assets, .env (token + webhook stay in unless BACKUP_REDACT=1), export/ (readable config)
  kon_data.zip   data/ only: database, VC / AFK / self-role data  (upload it back with `$backupload`)

Sent when the bot starts, every 30 seconds, and when it stops. Each backup is a new message; the previous one is
deleted (BACKUP_MODE=keep keeps them all).

`$backupload` (owner) - attach data file(s) to the command, or REPLY to a message that has them, and everything is merged
into the running bot. Works with: kon_data.zip (or an old full backup zip), a raw kon.db, db_dump.json, vc_data.json,
afk_data.json, blacktea.json. Nothing is overwritten blindly: new rows are added, existing ones keep the bigger value.
The reply shows what was loaded (players, shop, every feature's settings: tickets, giveaways, points ...), checks the
file checksums and tells you when a file is only code (kon_code.zip has no data - nothing to load).

Everything the bot saves is in kon.db (every setting of every feature) + vc_data.json + afk_data.json + blacktea.json, all inside
kon_data.zip. Before each backup the bot first writes everything that is still only in memory (counted giveaway messages ...)
to the database, and every backup checks the database (integrity + row counts) and records it in DATA_MANIFEST.json.
"""
import asyncio
import copy
import hashlib
import io
import json
import logging
import os
import platform
import re
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks

from core import checks, export
from core.config import PREFIX, AFK_FILE, BACKUP_MODE, BACKUP_REDACT, BACKUP_WEBHOOK, BASE, BLURPLE, DATA_DIR, BRAND
from core.errors import VCError
from core.icons import I, ok

log = logging.getLogger("kon.backup")

SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".idea", ".vscode",
             ".local", ".cache", ".pip", ".config", "site-packages", "pip-cache"}   # host/pip folders are not part of the bot
SKIP_FILES = {"kon.db", "kon.db-wal", "kon.db-shm", "leafy.db", "leafy.db-wal", "leafy.db-shm"}       # the live db is added as a consistent snapshot
SECRET_LINE = re.compile(r"^(\s*(?:export\s+)?[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|WEBHOOK)[A-Za-z0-9_]*\s*=).*$",
                         re.I | re.M)
ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z0-9_]+)\s*=", re.M)
WEBHOOK_LIMIT = 10 * 1024 * 1024
INTERVAL = 30       # seconds - periodic refresh
REASONS = {"start": "Bot started", "interval": "Automatic (every 30s)", "change": "Change detected",
           "manual": "Manual refresh", "stop": "Bot stopped"}


# readable names of the settings keys (kon.db -> settings table) - used by the $backupload report
FEATURES = {
    "points": "Points", "shop": "Shop", "customvc": "Custom VC rules", "noprefix": "No-prefix", "aliases": "Aliases",
    "rolecmds": "Role commands", "autoreact": "Autoreact", "aura": "Aura", "cmd_disable": "Command control",
    "tickets": "Tickets", "giveaway": "Giveaways", "tictactoe": "Tic Tac Toe", "timetap": "TimeTap",
    "timetap_stats": "TimeTap stats", "handfight": "Handfight", "handfight_stats": "Handfight stats",
    "webhook": "Webhook sender", "imagehost": "Image links", "imagehost_host": "Image host channel",
}


def feature_name(key: str) -> str:
    return FEATURES.get(key) or key.replace("_", " ").title()


def describe_setting(key: str, data) -> str:
    """One short phrase about what a settings row holds (shown in the $backupload report)."""
    if not isinstance(data, dict):
        return ""
    if key == "tickets":
        panels = data.get("panels") if isinstance(data.get("panels"), dict) else {"main": data}
        cats = sum(len(p.get("categories") or []) for p in panels.values() if isinstance(p, dict))
        return f"{len(panels)} panel{'s' if len(panels) != 1 else ''}, {cats} categories, {len(data.get('tickets') or {})} open"
    if key == "giveaway":
        return (f"{len(data.get('templates') or {})} templates, {len(data.get('active') or {})} running, "
                f"{len(data.get('ended') or {})} ended")
    if key == "shop":
        return "shop settings"
    return ""


def db_report(snapshot: str) -> dict:
    """Checks a database snapshot (worker thread): integrity + row counts + how many settings rows of every feature."""
    c = sqlite3.connect(snapshot)
    try:
        rep = {"integrity": c.execute("PRAGMA integrity_check").fetchone()[0], "tables": {}, "settings": {}}
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            rep["tables"][name] = c.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for key, n in c.execute("SELECT key, COUNT(*) FROM settings GROUP BY key"):
            rep["settings"][key] = n
        return rep
    finally:
        c.close()


def redact_env(raw: bytes) -> bytes:
    return SECRET_LINE.sub(r"\1REMOVED", raw.decode("utf-8", "replace")).encode()


def iter_files(base: Path):
    """(relative posix name, Path) of every file that belongs in the backup."""
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(files):
            if f in SKIP_FILES or f.endswith((".pyc", ".tmp", ".zip")) or f == "BACKUP_MANIFEST.json":
                continue                # .zip = older backups lying around (would nest / bloat); the manifest is rebuilt each time
            p = Path(root) / f
            yield p.relative_to(base).as_posix(), p


def build_zip(base: Path, db_snapshot: Optional[str], meta: dict, cfg_export: Optional[dict] = None):
    """Runs in a worker thread -> dict(code, data, digest, files{name:(size,sha1)}, raw_total, env_keys, env_removed).
    code zip = everything except data/; data zip = data/ (+ a consistent database snapshot)."""
    digest = hashlib.sha256()
    code_buf, data_buf = io.BytesIO(), io.BytesIO()
    files: dict[str, tuple[int, str]] = {}
    env_keys, env_removed = [], []
    with zipfile.ZipFile(code_buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as cz, \
            zipfile.ZipFile(data_buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as dz:
        def add(name: str, data: bytes):
            h = hashlib.sha1(data).hexdigest()
            files[name] = (len(data), h)
            digest.update(name.encode() + b"\0" + h.encode())
            (dz if name.startswith("data/") else cz).writestr(name, data)

        for name, p in iter_files(base):
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if p.name == ".env":
                text = data.decode("utf-8", "replace")
                env_keys = ENV_KEY.findall(text)
                if BACKUP_REDACT:                       # default: token + webhook stay in the code zip
                    env_removed = [k for k in env_keys if re.search("TOKEN|SECRET|PASSWORD|WEBHOOK", k, re.I)]
                    data = redact_env(data)
            add(name, data)
        if db_snapshot:
            add("data/kon.db", Path(db_snapshot).read_bytes())
        if cfg_export:
            add("export/SERVER_CONFIG.md", cfg_export["md"])
            add("export/SERVER_CONFIG.json", cfg_export["json"])
            add("export/afk.json", cfg_export["afk"])
            if cfg_export.get("links_md"):
                add("export/LINKS.md", cfg_export["links_md"])
            add("export/custom_vc_channels.json", cfg_export["vc_json"])
            for gid, files_ in cfg_export.get("per_server", {}).items():
                add(f"export/servers/{gid}/config.md", files_["md"])
                add(f"export/servers/{gid}/config.json", files_["json"])
                add(f"export/servers/{gid}/data.json", files_["data"])
            if db_snapshot:
                for name, data in export.db_extras(db_snapshot, cfg_export["names"]).items():
                    add(name, data)
        report = db_report(db_snapshot) if db_snapshot else None
        listing = [{"path": n, "size": s, "sha1": h} for n, (s, h) in sorted(files.items())]
        code_files = [f for f in listing if not f["path"].startswith("data/")]
        data_files = [f for f in listing if f["path"].startswith("data/")]
        cz.writestr("BACKUP_MANIFEST.json", json.dumps({
            **meta, "kind": "code", "files": code_files,
            "restore": ["unzip", "put your DISCORD_TOKEN (and BACKUP_WEBHOOK) back into .env",
                        "pip install -r requirements.txt", "python main.py",
                        "then send kon_data.zip with $backupload to bring the data back"]}, indent=2))
        dz.writestr("DATA_MANIFEST.json", json.dumps({**meta, "kind": "data", "files": data_files, "database": report,
                                                      "restore": ["send this zip with $backupload"]}, indent=2))
    return {"code": code_buf.getvalue(), "data": data_buf.getvalue(), "digest": digest.hexdigest(), "files": files, "report": report,
            "env_keys": env_keys, "env_removed": env_removed, "raw_total": sum(s for s, _ in files.values())}


# ------------------------------------------------------------------ $backupload: merge data zips into the live bot
UPLOAD_LIMIT = 64 * 1024 * 1024
DATA_ENTRY = re.compile(r"(?:^|/)data/((?:kon\.db|leafy\.db|vc_data\.json|afk_data\.json|blacktea\.json|selfrole/[^/]+))$")


def _cols(c: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in c.execute(f"PRAGMA {schema}.table_info({table})")]


def merge_db(live: sqlite3.Connection, src_path: str, replace: bool = False, detail: Optional[dict] = None) -> dict:
    """Merge a backup database into the live one. Returns {table: rows added/changed}.
    `detail` (optional) is filled with: features {settings key: rows changed}, notes {key: text}, guilds {ids in the backup}."""
    added: dict[str, int] = {}
    chk = sqlite3.connect(src_path)
    try:
        if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise VCError("That database file is damaged.")
    finally:
        chk.close()
    live.commit()
    live.execute("ATTACH DATABASE ? AS src", (src_path,))
    try:
        tables = {r[0] for r in live.execute("SELECT name FROM src.sqlite_master WHERE type='table'")}
        if detail is not None:
            for t in ("users", "settings", "shop_items"):
                if t in tables:
                    detail.setdefault("guilds", set()).update(r[0] for r in live.execute(f"SELECT DISTINCT guild_id FROM src.{t}"))
        with live:
            def n(table):
                return live.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]

            if "users" in tables:
                before = n("users")
                cols = [c for c in _cols(live, "src", "users") if c in _cols(live, "main", "users")]
                keep = [c for c in cols if c not in ("guild_id", "user_id")]
                live.execute(
                    f"INSERT INTO main.users({','.join(cols)}) SELECT {','.join(cols)} FROM src.users WHERE true "
                    f"ON CONFLICT(guild_id,user_id) DO UPDATE SET "
                    + ",".join(f"{c}=MAX({c},excluded.{c})" for c in keep))
                added["players"] = n("users") - before
            if "channel_progress" in tables:
                live.execute(
                    "INSERT INTO main.channel_progress(guild_id,user_id,channel_id,progress,total) "
                    "SELECT guild_id,user_id,channel_id,progress,total FROM src.channel_progress WHERE true "
                    "ON CONFLICT(guild_id,user_id,channel_id) DO UPDATE SET "
                    "progress=MAX(progress,excluded.progress), total=MAX(total,excluded.total)")
            if "badges" in tables:
                before = n("badges")
                live.execute("INSERT OR IGNORE INTO main.badges SELECT guild_id,user_id,badge FROM src.badges")
                added["badges"] = n("badges") - before
            if "shop_items" in tables:
                before = n("shop_items")
                has_pos = "position" in _cols(live, "src", "shop_items")
                live.execute(
                    "INSERT OR IGNORE INTO main.shop_items(guild_id,role_id,price,stock,emoji,position) "
                    f"SELECT guild_id,role_id,price,stock,emoji,{'position' if has_pos else 'NULL'} FROM src.shop_items")
                live.execute(
                    "UPDATE shop_items SET position=(SELECT COUNT(*) FROM shop_items s2 WHERE "
                    "s2.guild_id=shop_items.guild_id AND s2.id<=shop_items.id) WHERE position IS NULL")
                if replace:                          # a restore: the backup's price / stock / emoji / order win for items that exist
                    live.execute(
                        "UPDATE main.shop_items SET "
                        "price=(SELECT s.price FROM src.shop_items s WHERE s.guild_id=main.shop_items.guild_id AND s.role_id=main.shop_items.role_id), "
                        "stock=(SELECT s.stock FROM src.shop_items s WHERE s.guild_id=main.shop_items.guild_id AND s.role_id=main.shop_items.role_id), "
                        "emoji=(SELECT s.emoji FROM src.shop_items s WHERE s.guild_id=main.shop_items.guild_id AND s.role_id=main.shop_items.role_id), "
                        + ("position=COALESCE((SELECT s.position FROM src.shop_items s WHERE s.guild_id=main.shop_items.guild_id "
                           "AND s.role_id=main.shop_items.role_id), position) " if has_pos else "position=position ")
                        + "WHERE EXISTS (SELECT 1 FROM src.shop_items s WHERE s.guild_id=main.shop_items.guild_id AND s.role_id=main.shop_items.role_id)")
                added["shop items"] = n("shop_items") - before
            if "purchases" in tables:
                before = n("purchases")
                live.execute(
                    "INSERT INTO main.purchases(guild_id,user_id,role_id,price,ts) "
                    "SELECT s.guild_id,s.user_id,s.role_id,s.price,s.ts FROM src.purchases s WHERE NOT EXISTS("
                    "SELECT 1 FROM main.purchases p WHERE p.guild_id=s.guild_id AND p.user_id=s.user_id "
                    "AND p.role_id=s.role_id AND p.ts=s.ts)")
                added["purchases"] = n("purchases") - before
            if "point_log" in tables:
                live.execute(
                    "INSERT INTO main.point_log(guild_id,user_id,delta,reason,ts) "
                    "SELECT s.guild_id,s.user_id,s.delta,s.reason,s.ts FROM src.point_log s WHERE NOT EXISTS("
                    "SELECT 1 FROM main.point_log p WHERE p.guild_id=s.guild_id AND p.user_id=s.user_id "
                    "AND p.delta=s.delta AND p.ts=s.ts)")
            if "settings" in tables:
                touched = 0
                for gid, key, val in live.execute("SELECT guild_id,key,value FROM src.settings").fetchall():
                    try:
                        incoming = json.loads(val)
                    except ValueError:
                        continue
                    row = live.execute("SELECT value FROM main.settings WHERE guild_id=? AND key=?", (gid, key)).fetchone()
                    if row is None:
                        live.execute("INSERT INTO main.settings(guild_id,key,value) VALUES(?,?,?)", (gid, key, val))
                        touched += 1
                        if detail is not None:
                            detail.setdefault("features", {})[key] = detail.get("features", {}).get(key, 0) + 1
                            note = describe_setting(key, incoming)
                            if note:
                                detail.setdefault("notes", {}).setdefault(key, []).append(note)
                        continue
                    try:
                        current = json.loads(row[0])
                    except ValueError:
                        current = {}
                    merged = deep_override(copy.deepcopy(current), incoming) if replace else deep_fill(copy.deepcopy(current), incoming)
                    if merged != current:
                        live.execute("UPDATE main.settings SET value=? WHERE guild_id=? AND key=?",
                                     (json.dumps(merged), gid, key))
                        touched += 1
                        if detail is not None:
                            detail.setdefault("features", {})[key] = detail.get("features", {}).get(key, 0) + 1
                            note = describe_setting(key, merged)
                            if note:
                                detail.setdefault("notes", {}).setdefault(key, []).append(note)
                added["settings"] = touched
    finally:
        live.execute("DETACH DATABASE src")
    return added


def dump_to_db(dump: dict, path: str, live: sqlite3.Connection):
    """export/db_dump.json (or servers/<id>/database.json) -> a temporary sqlite file with the live schema, so merge_db can read it."""
    c = sqlite3.connect(path)
    try:
        for table in ("users", "channel_progress", "badges", "shop_items", "purchases", "settings", "point_log"):
            rows = dump.get(table)
            if not isinstance(rows, list) or not rows:
                continue
            row = live.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not row or not row[0]:
                continue
            c.execute(row[0])
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
            for r in rows:
                if not isinstance(r, dict):
                    continue
                vals = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items() if k in cols}
                if vals:
                    keys = list(vals)
                    c.execute(f"INSERT OR IGNORE INTO {table}({','.join(keys)}) VALUES({','.join('?' * len(keys))})",
                              list(vals.values()))
        c.commit()
    finally:
        c.close()


DATA_EXT = (".zip", ".db", ".sqlite", ".sqlite3", ".json")


class CodeBackup(VCError):
    """The zip is a code backup (kon_code.zip): that is fine, it just has no data to load."""


def deep_override(live, incoming):
    """Like deep_fill, but the BACKUP wins for every setting it has (`$backupload replace`). Lists are joined without duplicates."""
    if isinstance(live, dict) and isinstance(incoming, dict):
        for k, v in incoming.items():
            live[k] = deep_override(live[k], v) if k in live else copy.deepcopy(v)
        return live
    if isinstance(live, list) and isinstance(incoming, list):
        for v in incoming:
            if v not in live:
                live.append(copy.deepcopy(v))
        return live
    return copy.deepcopy(incoming)


def deep_fill(live, incoming):
    """Add what is missing in `live` from `incoming` (live values win; lists are joined without duplicates)."""
    if isinstance(live, dict) and isinstance(incoming, dict):
        for k, v in incoming.items():
            live[k] = deep_fill(live[k], v) if k in live else copy.deepcopy(v)
        return live
    if isinstance(live, list) and isinstance(incoming, list):
        for v in incoming:
            if v not in live:
                live.append(copy.deepcopy(v))
        return live
    return live


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{int(n)} B" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


class Backup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._fp = None
        self._last_push = 0.0
        self._last_digest: Optional[str] = None
        self._prev_files: dict[str, tuple[int, str]] = {}
        self._first = True
        self.started = time.time()
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None

    @property
    def cfg(self) -> dict:
        return self.store.data.setdefault("backup", {})

    async def cog_load(self):
        log.info("backup webhook: %s", "configured" if BACKUP_WEBHOOK else "NOT SET (check .env: BACKUP_WEBHOOK=...)")
        self.session = aiohttp.ClientSession()
        self.watcher.start()

    async def cog_unload(self):
        self.watcher.cancel()
        if self.session:
            await self.session.close()

    # ---------- triggers ----------
    def fingerprint(self):
        items = []
        for name, p in iter_files(BASE):
            try:
                st = p.stat()
            except OSError:
                continue
            items.append((name, st.st_mtime_ns, st.st_size))
        return hash((tuple(items), self.bot.db.conn.total_changes))

    @tasks.loop(seconds=INTERVAL)
    async def watcher(self):
        """Runs every INTERVAL (30) seconds: the first run is the start backup, every later one is periodic."""
        if not BACKUP_WEBHOOK:
            return
        reason = "start" if self._first else "interval"
        await self.refresh("online", reason)

    @watcher.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @watcher.error
    async def _err(self, e):
        log.error("backup watcher crashed: %r", e)
        await asyncio.sleep(30)
        if not self.watcher.is_running():
            self.watcher.restart()

    # ---------- core ----------
    async def refresh(self, status: str = "online", reason: str = "manual") -> bool:
        if not BACKUP_WEBHOOK:
            return False
        async with self._lock:
            try:
                self._first = False
                self.flush_all()                         # data that is only in memory goes into the files / database first
                snap = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
                snap.close()
                counter = int(self.cfg.get("count", 0)) + 1
                meta = {"bot": str(self.bot.user), "created": discord.utils.utcnow().isoformat(),
                        "reason": REASONS.get(reason, reason), "status": status, "backup_number": counter}
                try:
                    self.bot.db.snapshot(snap.name)
                    # Drop settings left behind by the retired Ludo module from backup snapshots.
                    # This only cleans the exported snapshot; it does not alter the live server database.
                    try:
                        stale = sqlite3.connect(snap.name)
                        try:
                            stale.execute("DELETE FROM settings WHERE key IN ('ludo', 'ludo_stats', 'ludo_games')")
                            stale.commit()
                        finally:
                            stale.close()
                    except sqlite3.Error:
                        log.warning("could not prune retired Ludo settings from backup snapshot")
                    cfg_export = self.export_config()
                    info = await asyncio.to_thread(build_zip, BASE, snap.name, meta, cfg_export)
                finally:
                    Path(snap.name).unlink(missing_ok=True)
                self._fp = self.fingerprint()
                rep_ = info.get("report") or {}
                if rep_ and rep_.get("integrity") != "ok":
                    log.error("backup database check FAILED: %s", rep_.get("integrity"))
                    self.last_error = f"database check: {rep_.get('integrity')}"
                await self._push(status, reason, info, counter)
                self._last_push = time.monotonic()
                self.last_ok = time.time()
                if not (rep_ and rep_.get("integrity") != "ok"):
                    self.last_error = None
                if counter == 1 or reason in ("start", "manual"):
                    log.info("backup #%d sent to webhook (%s)", counter, REASONS.get(reason, reason))
                return True
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.error("backup failed: %s", self.last_error, exc_info=True)
                self._last_push = time.monotonic()
                return False

    def flush_all(self):
        """Write everything that lives only in memory: vc_data.json and every cog that keeps unsaved data (giveaway counters ...)."""
        try:
            self.store.flush()
        except Exception:
            log.exception("store flush failed")
        for cog in list(self.bot.cogs.values()):
            fp = getattr(cog, "flush_pending", None)
            if callable(fp):
                try:
                    fp()
                except Exception:
                    log.exception("flush of %s failed", type(cog).__name__)

    def export_config(self) -> Optional[dict]:
        """Readable server config (names resolved from the discord cache). Never allowed to break the backup."""
        try:
            try:
                with open(AFK_FILE, "r", encoding="utf-8") as f:
                    afk = json.load(f)
            except (OSError, ValueError):
                afk = {}
            return export.collect(self.bot, afk)
        except Exception as e:
            log.warning("config export failed (backup continues without it): %r", e, exc_info=True)
            return None

    def embed(self, status, reason, info, counter, skipped: bool) -> discord.Embed:
        online = status == "online"
        now = int(time.time())
        db = self.bot.db.counts()
        e = discord.Embed(title=f"Kon Backup #{counter}", color=BRAND if online else 0xED4245,
                          timestamp=discord.utils.utcnow())
        e.description = (f"{'Online' if online else 'Offline'} · {REASONS.get(reason, reason)} · <t:{now}:R>\n"
                         f"Code `{human(len(info['code']))}` · Data `{human(len(info['data']))}`"
                         + (f"\n{I.s('warning')} Too big to upload, status only." if skipped else ""))
        e.set_footer(text=f"{len(self.bot.guilds)} servers · {db['users']} players · {db['settings']} settings · "
                          f"{len(self.store.data['vcs'])} VCs" + (" · db ✓" if (info.get("report") or {}).get("integrity") == "ok" else ""))
        return e

    def fmt_up(self) -> str:
        s = int(time.time() - self.started)
        d, r = divmod(s, 86400)
        h, r = divmod(r, 3600)
        m, s = divmod(r, 60)
        return " ".join(x for x in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s") if x)

    async def _push(self, status, reason, info, counter):
        code, data = info["code"], info["data"]
        skipped = len(code) > WEBHOOK_LIMIT or len(data) > WEBHOOK_LIMIT
        wh = discord.Webhook.from_url(BACKUP_WEBHOOK, session=self.session)
        avatar = self.bot.user.display_avatar.url
        kw = dict(wait=True, username="Kon Backup", avatar_url=avatar)
        sent = []
        embed = self.embed(status, reason, info, counter, skipped)
        try:
            if skipped:
                sent.append(await wh.send(embed=embed, **kw))
            elif len(code) + len(data) <= WEBHOOK_LIMIT:            # both zips in ONE message
                sent.append(await wh.send(embed=embed, files=[discord.File(io.BytesIO(code), "kon_code.zip"),
                                                              discord.File(io.BytesIO(data), "kon_data.zip")], **kw))
            else:                                                    # too big together: data first, then code
                sent.append(await wh.send(embed=embed, file=discord.File(io.BytesIO(data), "kon_data.zip"), **kw))
                sent.append(await wh.send(file=discord.File(io.BytesIO(code), "kon_code.zip"), **kw))
        except discord.HTTPException as e:
            if e.status != 413:
                raise
            sent = [await wh.send(embed=self.embed(status, reason, info, counter, True), **kw)]
            skipped = True
        old = self.cfg.get("webhook_message")
        old = old if isinstance(old, list) else ([old] if old else [])
        self.cfg["webhook_message"] = [m.id for m in sent]
        if BACKUP_MODE != "keep":                                    # only the latest backup stays in the channel
            for mid in old:
                if mid not in self.cfg["webhook_message"]:
                    try:
                        await wh.delete_message(mid)
                    except discord.HTTPException:
                        pass
        if not skipped:
            self._last_digest = info["digest"]
        self._prev_files = info["files"]
        self.cfg["count"] = counter
        self.store.save_soon()

    # ---------- $backup (owner) ----------
    @commands.group(name="backup", invoke_without_command=True)
    @checks.true_owner_only()
    async def backup(self, ctx: commands.Context):
        """Backup status. `$backup refresh` = back up right now (owner only)."""
        lines = [
            f"{I.s('link')} Webhook: " + ("set" if BACKUP_WEBHOOK else "**not set** (add `BACKUP_WEBHOOK` to `.env`)"),
            f"{I.s('gift_box')} Backups sent: **{self.cfg.get('count', 0)}**",
            f"{I.s('clock')} Last: {f'<t:{int(self.last_ok)}:R>' if self.last_ok else 'not yet'}",
            f"{I.s('bolt')} Every **{INTERVAL}s**, on start and on stop",
            f"{I.s('upload')} Bring data back: reply to `kon_data.zip` (or a db / json file) with `{PREFIX}backupload`",
        ]
        if self.last_error:
            lines.append(f"{I.s('warning')} Last error: `{self.last_error[:200]}`")
        await ctx.reply(embed=discord.Embed(title="Backup", description="\n".join(lines), color=BLURPLE),
                        mention_author=False)

    @backup.command(name="config")
    @checks.true_owner_only()
    async def backup_config(self, ctx: commands.Context):
        """Send the readable config of this server (SERVER_CONFIG.md) and the players list (CSV)."""
        async with ctx.typing():
            exp = self.export_config()
            if exp is None:
                raise VCError("I couldn't build the export. Check the bot log.")
            files = [discord.File(io.BytesIO(exp["md"]), filename="SERVER_CONFIG.md")]
            if ctx.guild:
                extras = await asyncio.to_thread(self._players_csv, ctx.guild.id, exp["names"])
                if extras:
                    files.append(discord.File(io.BytesIO(extras), filename=f"players_{ctx.guild.id}.csv"))
        await ctx.reply(ok("Here you go."), files=files, mention_author=False)

    def _players_csv(self, gid: int, names: dict) -> Optional[bytes]:
        snap = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        snap.close()
        try:
            self.bot.db.snapshot(snap.name)
            return export.db_extras(snap.name, names).get(f"export/players_{gid}.csv")
        finally:
            Path(snap.name).unlink(missing_ok=True)

    @backup.command(name="refresh")
    @checks.true_owner_only()
    async def backup_refresh(self, ctx: commands.Context):
        """Back up right now."""
        if not BACKUP_WEBHOOK:
            raise VCError("Add `BACKUP_WEBHOOK` to `.env` and restart me first.")
        async with ctx.typing():
            good = await self.refresh("online", "manual")
        await ctx.reply(ok("Backup sent.") if good else f"{I.s('cross')} Backup failed: `{self.last_error}`",
                        mention_author=False)

    # ---------- $backupload (owner): merge data zip(s) into the running bot ----------
    @commands.command(name="backupload", aliases=["bload", "bupload"])
    @checks.true_owner_only()
    async def backupload(self, ctx: commands.Context, mode: str = ""):
        """Reply to a data file (kon_data.zip, kon.db, db_dump.json ...) with `$backupload`, or attach it to the command.
        `$backupload replace` = every setting from the backup wins (links, banners, panels ...) - use it to restore a fresh bot."""
        replace = mode.strip().lower() in ("replace", "restore", "overwrite", "full")
        atts = list(ctx.message.attachments)
        ref = ctx.message.reference
        if ref is not None:
            msg = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if msg is None and ref.message_id:
                try:
                    chan = ctx.guild.get_channel_or_thread(ref.channel_id) if ctx.guild and ref.channel_id else None
                    msg = await (chan or ctx.channel).fetch_message(ref.message_id)
                except discord.HTTPException:
                    msg = None
            if msg is not None:
                atts += list(msg.attachments)
        atts = [a for a in atts if a.filename.lower().endswith(DATA_EXT)][:10]
        if not atts:
            raise VCError("Reply to a data file (zip / db / json) with `$backupload`, or attach it to the command.")
        t0 = time.monotonic()
        status = await ctx.reply(embed=discord.Embed(
            color=BLURPLE, description=f"{I.s('hourglass')} Loading **{len(atts)}** file{'s' if len(atts) != 1 else ''}..."),
            mention_author=False)
        self.flush_all()                  # unsaved in-memory data goes into the database FIRST, so the merge can't lose it
        results, changed, problems, guilds, meta = [], 0, 0, set(), {}
        for a in atts:
            res = {"name": a.filename, "icon": I.s("check"), "lines": []}
            detail: dict = {}
            if a.size > UPLOAD_LIMIT:
                res.update(icon=I.s("cross"), lines=["This file is too big (max 64 MB)."])
                problems += 1
            else:
                try:
                    got = await self.merge_file(await a.read(), a.filename, replace, detail)
                except CodeBackup as e:
                    res.update(icon=I.s("info"), lines=[str(e)])
                except VCError as e:
                    res.update(icon=I.s("cross"), lines=[str(e)])
                    problems += 1
                except (zipfile.BadZipFile, OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError) as e:
                    log.warning("backupload failed for %s: %r", a.filename, e, exc_info=True)
                    res.update(icon=I.s("cross"), lines=["I couldn't read this file (damaged or not a Kon data file)."])
                    problems += 1
                else:
                    res["lines"] = self.report_lines(got, detail)
                    if detail.get("verified") and detail["verified"][1]:
                        res["icon"] = I.s("warning")
                    changed += 1 if any(got.values()) else 0
                    guilds |= detail.get("guilds", set())
                    meta = meta or detail.get("meta") or {}
            results.append(res)
        self.reset_caches()
        if problems:
            title, color = "Loaded with problems", 0xFEE75C
        elif changed:
            title, color = "Backup loaded", 0x57F287
        else:
            title, color = "Everything is already in the bot", BRAND
        e = discord.Embed(title=f"{I.s('check') if not problems else I.s('warning')} {title}", color=color)
        head = []
        if meta.get("backup_number"):
            when = ""
            try:
                when = f" · made <t:{int(datetime.fromisoformat(meta['created']).timestamp())}:R>"
            except (TypeError, ValueError, KeyError):
                pass
            head.append(f"Backup **#{meta['backup_number']}** of `{meta.get('bot', 'Kon')}`{when}")
        head.append("**Replace** mode - the settings of the backup win." if replace else
                    "**Merge** mode - what the bot has stays, everything missing is added. (`$backupload replace` = the backup wins)")
        e.description = "\n".join(head)
        for res in results:
            e.add_field(name=f"{res['icon']} {res['name']}"[:256], value="\n".join(res["lines"])[:1024] or "-", inline=False)
        if guilds:
            names = [(self.bot.get_guild(g).name if self.bot.get_guild(g) else f"`{g}`") for g in sorted(guilds)]
            e.add_field(name=f"Servers in the backup ({len(names)})", value=", ".join(names)[:1024], inline=False)
        e.set_footer(text=f"{time.monotonic() - t0:.1f}s · live now, no restart needed")
        try:
            await status.edit(embed=e)
        except discord.HTTPException:
            await ctx.reply(embed=e, mention_author=False)

    @staticmethod
    def report_lines(got: dict, detail: dict) -> list:
        """What one data file changed, in plain words."""
        lines = []
        ver = detail.get("verified")
        if ver:
            lines.append(f"{I.s('check')} Checksums OK ({ver[0]} files)" if not ver[1]
                         else f"{I.s('warning')} **{ver[1]}** file(s) don't match their checksum - the backup may be damaged")
        counts = [f"**{got[k]:,}** {k}" for k in ("players", "badges", "shop items", "purchases") if got.get(k)]
        if counts:
            lines.append("• " + " · ".join(counts))
        feats = detail.get("features") or {}
        if got.get("settings") or feats:
            parts = []
            for key in sorted(feats, key=feature_name):
                note = (detail.get("notes") or {}).get(key)
                parts.append(feature_name(key) + (f" ({note[0]})" if note and len(note) == 1 else ""))
            lines.append(f"• **{got.get('settings', len(feats))}** settings: " + (", ".join(parts) if parts else "merged"))
        for k in ("VC / server settings", "AFK", "BlackTea channels", "banners"):
            if got.get(k):
                lines.append(f"• **{got[k]}** {k}" if k != "VC / server settings" else f"• {k} (self roles, VC panel, extra owners ...)")
        if len(lines) == (1 if ver else 0):
            lines.append("• already up to date - nothing new in it")
        return lines

    async def merge_file(self, raw: bytes, filename: str, replace: bool = False, detail: Optional[dict] = None) -> dict:
        """One data file (zip / db / json) -> merged into the running bot. Returns {what: count}."""
        name = filename.lower()
        if name.endswith(".zip"):
            return await self.merge_zip(raw, replace, detail)
        out: dict[str, int] = {}
        if name.endswith((".db", ".sqlite", ".sqlite3")):
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            try:
                tmp.write(raw)
                tmp.close()
                for k, v in merge_db(self.bot.db.conn, tmp.name, replace, detail).items():
                    out[k] = out.get(k, 0) + v
            finally:
                Path(tmp.name).unlink(missing_ok=True)
            return out
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise VCError("That json isn't a Kon data file.")
        if any(isinstance(data.get(t), list) for t in ("users", "shop_items", "settings", "badges", "purchases")):
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            try:
                tmp.close()
                dump_to_db(data, tmp.name, self.bot.db.conn)
                for k, v in merge_db(self.bot.db.conn, tmp.name, replace, detail).items():
                    out[k] = out.get(k, 0) + v
            finally:
                Path(tmp.name).unlink(missing_ok=True)
        elif any(k in data for k in ("guilds", "vcs", "selfroles", "vc_setup", "guild_owners")):
            if self.merge_store(data, replace):
                out["VC / server settings"] = 1
        elif "afk" in data:
            n = self.merge_afk(data)
            if n:
                out["AFK"] = n
        elif isinstance(data.get("channels"), dict):
            if self.merge_blacktea(data):
                out["BlackTea channels"] = 1
        else:
            raise VCError("I don't know this json file. Use kon_data.zip, kon.db or db_dump.json.")
        return out

    async def merge_zip(self, raw: bytes, replace: bool = False, detail: Optional[dict] = None) -> dict:
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = set(z.namelist())
        found: dict[str, str] = {}
        for name in z.namelist():
            m = DATA_ENTRY.search(name.replace("\\", "/"))
            if m and ".." not in name.split("/") and not name.endswith("/"):
                found["kon.db" if m.group(1) == "leafy.db" else m.group(1)] = name      # old Leafy backups still load
        if not found:
            if "main.py" in names or "BACKUP_MANIFEST.json" in names or any(n.startswith("cogs/") for n in names):
                raise CodeBackup("This is the **code** backup (bot files, assets, `.env`). It has no data inside, so there is "
                                 "nothing to load - your data is in `kon_data.zip`. Use `tools/restore.py` to set up the code.")
            raise VCError("There is no `data/` folder inside this zip.")
        if detail is not None and "DATA_MANIFEST.json" in names:         # checksum of every file against the manifest
            try:
                mf = json.loads(z.read("DATA_MANIFEST.json"))
                good = bad = 0
                for f in mf.get("files", []):
                    if f.get("path") in names:
                        if hashlib.sha1(z.read(f["path"])).hexdigest() == f.get("sha1"):
                            good += 1
                        else:
                            bad += 1
                detail["verified"] = (good, bad)
                detail["meta"] = {k: mf.get(k) for k in ("bot", "created", "backup_number", "reason")}
            except (ValueError, KeyError, zipfile.BadZipFile):
                pass
        out: dict[str, int] = {}
        if "kon.db" in found:
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            try:
                tmp.write(z.read(found["kon.db"]))
                tmp.close()
                for k, v in merge_db(self.bot.db.conn, tmp.name, replace, detail).items():
                    out[k] = out.get(k, 0) + v
            finally:
                Path(tmp.name).unlink(missing_ok=True)
        if "vc_data.json" in found:
            if self.merge_store(json.loads(z.read(found["vc_data.json"])), replace):
                out["VC / server settings"] = 1
        if "afk_data.json" in found:
            n = self.merge_afk(json.loads(z.read(found["afk_data.json"])))
            if n:
                out["AFK"] = n
        if "blacktea.json" in found:
            if self.merge_blacktea(json.loads(z.read(found["blacktea.json"]))):
                out["BlackTea channels"] = 1
        # Banners are saved as LINKS (banner_url / banner_ref inside vc_data.json, merged above). Only an OLD backup
        # still carries the image file: it is turned into a link right away and the file is deleted again, so no
        # GIF / image stays in data/ or in the next backup.
        sr = self.bot.get_cog("SelfRoles")
        for rel, name in found.items():
            if not rel.startswith("selfrole/") or "/" in rel[len("selfrole/"):]:
                continue
            fn = Path(rel).name
            dest = DATA_DIR / "selfrole" / fn
            if fn.lower().endswith((".gif", ".png", ".jpg", ".jpeg", ".webp")) and not dest.exists():
                gid = Path(fn).stem
                cfg = (self.store.data.get("selfroles") or {}).get(gid) or {}
                if cfg.get("banner_url") or cfg.get("banner_ref"):
                    continue                                    # a link exists already - the file is not needed
                data = z.read(name)
                if len(data) <= 12 * 1024 * 1024:
                    dest.parent.mkdir(exist_ok=True)
                    dest.write_bytes(data)
                    guild = self.bot.get_guild(int(gid)) if gid.isdigit() else None
                    if sr is not None and guild is not None:
                        await sr.migrate_legacy_banner(guild)   # upload once -> keep the link -> delete the file
                    out["banners"] = out.get("banners", 0) + 1
        return out

    def merge_blacktea(self, inc: dict) -> bool:
        cog = self.bot.get_cog("BlackTea")
        if cog is None or not isinstance(inc.get("channels"), dict):
            return False
        before = json.dumps(cog.settings, sort_keys=True)
        deep_fill(cog.settings.setdefault("channels", {}), inc["channels"])      # live values win
        if json.dumps(cog.settings, sort_keys=True) == before:
            return False
        from cogs.blacktea import save_settings
        save_settings(cog.settings)
        return True

    def merge_store(self, inc: dict, replace: bool = False) -> bool:
        fill = deep_override if replace else deep_fill
        d = self.store.data
        before = json.dumps(d, sort_keys=True, default=str)
        for key in ("guilds", "selfroles", "vc_setup", "guild_owners"):
            if isinstance(inc.get(key), dict):
                d[key] = fill(d.setdefault(key, {}), inc[key])
        if isinstance(inc.get("extra_owners"), list):
            d["extra_owners"] = deep_fill(d.setdefault("extra_owners", []), inc["extra_owners"])
        now = time.time()
        for k, exp in (inc.get("mutes") or {}).items():          # only mutes that are still running
            if k not in d["mutes"] and isinstance(exp, (int, float)) and exp > now:
                d["mutes"][k] = exp
        for cid, rec in (inc.get("vcs") or {}).items():          # only VCs that still exist
            if cid not in d["vcs"] and self.bot.get_channel(int(cid)) is not None:
                d["vcs"][cid] = rec
        changed = json.dumps(d, sort_keys=True, default=str) != before
        if changed:
            self.store.save_soon()
        return changed

    def merge_afk(self, inc: dict) -> int:
        cog = self.bot.get_cog("AFK")
        if cog is None:
            return 0
        n = 0
        for k, v in (inc.get("afk") or {}).items():
            if int(k) not in cog.afk:
                cog.afk[int(k)] = v
                n += 1
        for k, v in (inc.get("tag") or {}).items():
            cog.tag_enabled.setdefault(int(k), v)
        if n:
            cog.save()
        return n

    def reset_caches(self):
        """Settings were merged in the database: drop every in-memory copy so the bot re-reads them."""
        for owner in (self.bot, *self.bot.cogs.values()):
            for attr in ("_noprefix", "_aliases", "_rolecmds", "_cfg", "_cache", "_rules", "_customvc"):
                cache = getattr(owner, attr, None)
                if isinstance(cache, dict):
                    cache.clear()
        svc = getattr(self.bot, "vc_service", None)
        if svc is not None:
            svc.reset_cache()


async def setup(bot):
    await bot.add_cog(Backup(bot))
