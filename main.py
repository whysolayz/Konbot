import asyncio
import hashlib
import json
import logging
import signal
import time

import discord
from discord.ext import commands

from core import names, ratelimit
from core.config import AUTO_SYNC, DB_FILE, OWNER_IDS, PREFIX, TOKEN, VC_FILE
from core.db import DB
from core.icons import I
from core.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kon")

EXTENSIONS = ["cogs.errors", "cogs.afk", "cogs.vc", "cogs.drag", "cogs.interface", "cogs.points", "cogs.shop",
              "cogs.backup", "cogs.meta", "cogs.admin", "cogs.prefixes", "cogs.autoreact", "cogs.rolecmds",
              "cogs.selfroles", "cogs.expressions", "cogs.purge", "cogs.aura", "cogs.roletransfer", "cogs.tictactoe", "cogs.blacktea", "cogs.timetap", "cogs.handfight", "cogs.games", "cogs.imagehost", "cogs.cmdcontrol", "cogs.tickets", "cogs.giveaway", "cogs.status_api", "cogs.dashboard_api"]


class Kon(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()      # includes voice_states
        intents.message_content = True            # privileged: enable in the Developer Portal
        intents.members = True                    # privileged: enable in the Developer Portal
        super().__init__(
            command_prefix=commands.when_mentioned_or(PREFIX),
            strip_after_prefix=True,                  # "$ vcstatus" works like "$vcstatus"
            case_insensitive=True,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
        )
        self.store = Store(VC_FILE)
        self.db = DB(DB_FILE)
        self.vc_service = None
        self._noprefix: dict[int, dict] = {}
        self._aliases: dict[int, dict] = {}
        self._rolecmds: dict[int, dict] = {}
        self._closing = False
        self._closed_evt = asyncio.Event()


    # ---------- owner + extra owners ($konextra) ----------
    async def is_true_owner(self, user) -> bool:
        """The real bot owner only (can manage the extra owners)."""
        return user.id in OWNER_IDS or await super().is_owner(user)

    def extra_owners(self, gid: int) -> list:
        """Members added with $konextra in server `gid` (every server has its own list)."""
        return self.store.owners(gid)

    async def is_owner_in(self, user, gid) -> bool:
        """True owner (everywhere) or extra owner of THIS server only."""
        if await self.is_true_owner(user):
            return True
        return gid is not None and user.id in self.store.owners(gid)

    async def is_owner(self, user) -> bool:
        """Server-less check = real bot owner only. Use is_owner_in(user, guild_id) for owner commands."""
        return await self.is_true_owner(user)

    def is_owner_fast(self, user_id: int, gid) -> bool:
        """Same as is_owner_in but without await (the owner id is loaded once at startup)."""
        if user_id in OWNER_IDS or user_id == self.owner_id or user_id in (self.owner_ids or ()):
            return True
        return gid is not None and user_id in self.store.owners(gid)

    def migrate_legacy_owners(self):
        """Old versions kept ONE global extra-owner list. Move each person to the server(s) where the database shows
        them (no-prefix list first, then points); the global list is emptied so nothing leaks between servers."""
        d = self.store.data
        legacy = d.get("extra_owners") or []
        if not legacy:
            return
        for uid in list(legacy):
            gids = set()
            for gid, val in self.db.conn.execute("SELECT guild_id, value FROM settings WHERE key='noprefix'"):
                try:
                    if uid in json.loads(val).get("users", []):
                        gids.add(gid)
                except Exception:
                    pass
            if not gids:
                gids = {r[0] for r in self.db.conn.execute("SELECT guild_id FROM users WHERE user_id=? AND points>0", (uid,))}
            for gid in gids:
                lst = self.store.owners(gid)
                if uid not in lst:
                    lst.append(uid)
            log.info("legacy extra owner %s moved to server(s) %s", uid, sorted(gids) or "none - re-add with $konextra")
        d["extra_owners"] = []
        self.store.save_soon()

    # ---------- no-prefix + custom aliases ----------
    def noprefix_cfg(self, gid: int) -> dict:
        c = self._noprefix.get(gid)
        if c is None:
            c = {"role": None, "users": [], **self.db.get_settings(gid, "noprefix")}
            self._noprefix[gid] = c
        return c

    def save_noprefix(self, gid: int):
        self.db.set_settings(gid, "noprefix", self.noprefix_cfg(gid))

    def is_noprefix(self, member) -> bool:
        if not getattr(member, "guild", None):
            return False
        c = self.noprefix_cfg(member.guild.id)
        if member.id in c["users"]:
            return True
        return bool(c["role"]) and any(r.id == c["role"] for r in getattr(member, "roles", ()))

    def aliases(self, gid: int) -> dict:
        a = self._aliases.get(gid)
        if a is None:
            a = self._aliases[gid] = dict(self.db.get_settings(gid, "aliases"))
        return a

    def save_aliases(self, gid: int):
        self.db.set_settings(gid, "aliases", self.aliases(gid))

    # ---------- role commands ($setrole manager <role>  ->  $setmanager @user gives the role) ----------
    def rolecmds(self, gid: int) -> dict:
        r = self._rolecmds.get(gid)
        if r is None:
            r = self._rolecmds[gid] = dict(self.db.get_settings(gid, "rolecmds"))
        return r

    def save_rolecmds(self, gid: int):
        self.db.set_settings(gid, "rolecmds", self.rolecmds(gid))

    async def get_prefix(self, message, /):
        prefixes = await super().get_prefix(message)
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if message.guild and not message.author.bot and self.is_noprefix(message.author):
            prefixes = [*prefixes, ""]                 # "" last -> "$cmd" still works, plain "cmd" works too
        return prefixes

    async def get_context(self, message, *, cls=commands.Context):
        # slash (hybrid) commands also call get_context - with an Interaction, which has no .author
        if isinstance(message, discord.Message) and message.guild and not message.author.bot:
            prefixes = await self.get_prefix(message)
            pref = discord.utils.find(message.content.startswith, prefixes)
            if pref is not None:
                word, _, tail = message.content[len(pref):].lstrip().partition(" ")
                lw = word.lower()
                target = self.aliases(message.guild.id).get(lw)
                if target:                                  # custom alias -> real command (idempotent)
                    message.content = f"{pref}{target} {tail}".rstrip()
                elif lw.startswith("set") and lw[3:] in self.rolecmds(message.guild.id) and self.get_command(lw) is None:
                    message.content = f"{pref}rolecmd {lw[3:]} {tail}".rstrip()   # $setmanager (only the set<name> form exists)
                elif lw in self.rolecmds(message.guild.id) and lw.startswith("set") and self.get_command(lw) is None:
                    message.content = f"{pref}rolecmd {lw} {tail}".rstrip()       # old saved names that already start with "set"
                elif lw == "vc" and tail.strip():           # "$vc kick @x"  ->  "$vckick @x"
                    sub, _, rest = tail.strip().partition(" ")
                    if self.get_command("vc" + sub.lower()):
                        message.content = f"{pref}vc{sub} {rest}".rstrip()
        return await super().get_context(message, cls=cls)

    def command_hash(self) -> str:
        """Fingerprint of all slash commands, so we only sync when something changed."""
        rows = []
        for c in self.tree.walk_commands():
            rows.append([c.qualified_name, c.description, [(p.name, p.description, str(p.type)) for p in getattr(c, "parameters", [])]])
        return hashlib.sha1(json.dumps(sorted(rows), default=str).encode()).hexdigest()

    async def setup_hook(self):
        self.store.load()
        self.migrate_legacy_owners()
        await I.load(self)                       # upload/load custom icons
        try:
            await super().is_owner(self.user)    # loads the owner id once, so is_owner_fast works
        except Exception as e:
            log.warning("could not load the owner id: %r", e)

        from cogs.vc import VCService            # imported here so icons exist first
        from cogs.interface import PanelView
        self.vc_service = VCService(self)
        self.add_check(ratelimit.command_check)   # global rate limiter

        for ext in EXTENSIONS:
            await self.load_extension(ext)
        clash = names.apply(self)                  # friendly new command names (old names stay as aliases)
        if clash:
            log.warning("new command names not added (already taken): %s", ", ".join(clash))
        self.add_view(PanelView(self.vc_service))  # persistent control-panel buttons
        self.add_view(PanelView(self.vc_service, prefix="leafy"))   # panels posted before the Kon rebrand

        if AUTO_SYNC:
            h = self.command_hash()
            if h != self.store.data.get("sync_hash"):
                n = len(await self.tree.sync())
                self.store.data["sync_hash"] = h
                self.store.save_soon()
                log.info("slash commands synced (%d)", n)

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    async def close(self):
        if self._closing:
            await self._closed_evt.wait()       # 2nd caller (async-with exit) waits for the 1st to finish
            return
        self._closing = True
        backup = self.get_cog("Backup")
        if backup:                                  # last backup + "offline" status
            try:
                await asyncio.wait_for(backup.refresh("offline", "stop"), 25)
            except Exception as e:
                log.warning("final backup failed: %r", e)
        self.store.flush()
        self.db.close()
        await super().close()
        self._closed_evt.set()


async def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN missing - put it in .env")
    async with Kon() as bot:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):   # panel "Stop" sends SIGTERM -> clean shutdown + final backup
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
            except NotImplementedError:               # Windows
                pass
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
