"""Giveaways - one panel (`$giveaway`) to build everything, saved templates, requirements and role / points prizes.

$giveaway                    the builder panel: title, prize, winners, time, emojis, colour, image, buttons, requirements
$giveaway end [id]           end a running giveaway right now (reply to it, or give its message id / #number)
$giveaway reroll [id] [n]    pick new winner(s) of an ended giveaway
$giveaway cancel [id]        cancel a running giveaway (no winners)
$giveaway list               running + recently ended giveaways
$giveaway templates          saved templates (`$giveaway load <name>` opens the builder with one)

PRIZE      text + optional ROLE reward (permanent or temporary - it is removed again by itself) + optional Kon points
REQUIRE    role(s) · N messages (channels / categories can be excluded) · invite N people · "say X to @person"
           Members can join any time, but only people who completed EVERYTHING when it ends can win ("no task = no reward").
EMOJIS     every emoji and every button (label / emoji / colour) is editable. Type an emoji, `:name:` or just an emoji ID -
           animated emojis work too (no Nitro needed).
TEMPLATES  "Save template" keeps the whole giveaway (look, emojis, buttons, prize, requirements) - "Load template" brings it back.

Everything is saved in kon.db (settings key "giveaway"), so it is part of every backup / `$backupload`.
Buttons use fixed custom ids ("kon_gw:...") and are handled in on_interaction, so they keep working after a restart.
"""
import asyncio
import copy
import datetime as dt
import logging
import time
from typing import Optional

import discord
from discord.ext import commands, tasks

from core import emoji as emojis
from core import giveaway_logic as gl
from core import media
from core.config import BRAND, PREFIX
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import OwnedView

log = logging.getLogger("kon.giveaway")

KEY = "giveaway"
CID = "kon_gw:"
GREEN, RED, GREY = 0x57F287, 0xED4245, 0x4F545C
NONE = discord.AllowedMentions.none()
PEOPLE = discord.AllowedMentions(users=True, roles=False, everyone=False)
STYLES = {"primary": discord.ButtonStyle.primary, "success": discord.ButtonStyle.success,
          "secondary": discord.ButtonStyle.secondary, "danger": discord.ButtonStyle.danger}
FLUSH_EVERY = 10          # seconds: how often counted messages / entries are written to the database
EDIT_EVERY = 8            # seconds: a giveaway message is redrawn at most this often (entry counter)
CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.voice, discord.ChannelType.category,
                 discord.ChannelType.news, discord.ChannelType.forum, discord.ChannelType.stage_voice]


def kon_admin_gw():
    """Manage Server / Administrator, the bot owner or a $konextra owner of this server."""
    async def predicate(ctx: commands.Context):
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        p = ctx.author.guild_permissions
        if p.administrator or p.manage_guild or await ctx.bot.is_owner_in(ctx.author, ctx.guild.id):
            return True
        raise commands.MissingPermissions(["manage_guild"])
    return commands.check(predicate)


def pre(emoji: str, text: str) -> str:
    return f"{emoji} {text}" if emoji else text


async def say(interaction: discord.Interaction, text: str = None, **kw):
    kw.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(text, **kw)
    else:
        await interaction.response.send_message(text, **kw)


class Noop(discord.ui.Button):
    """Only drawn - the real work happens in Giveaway.on_interaction (custom_id), so it also works after a restart."""

    async def callback(self, interaction: discord.Interaction):
        return


class GModal(discord.ui.Modal):
    """fields: dicts(label, default, max, required, long, placeholder). The callback gets the cleaned values; afterwards the
    builder message is redrawn. Errors (VCError) are shown to the person only."""

    def __init__(self, view, title: str, fields: list, on_submit):
        super().__init__(title=title[:45], timeout=300)
        self.view_, self._cb, self._inputs = view, on_submit, []
        for f in fields:
            mx = f.get("max", 100)
            ti = discord.ui.TextInput(
                label=f["label"][:45], default=((f.get("default") or "")[:mx] or None), max_length=mx,
                required=f.get("required", False), placeholder=(f.get("placeholder") or None),
                style=discord.TextStyle.paragraph if f.get("long") else discord.TextStyle.short)
            self._inputs.append(ti)
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            await self._cb(interaction, [i.value.strip() for i in self._inputs])
        except VCError as e:
            return await interaction.followup.send(err(str(e)), ephemeral=True)
        await self.view_.refresh()

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("giveaway modal failed", exc_info=error)
        await say(interaction, err("That didn't work - check the values and try again."))


class LeaveView(discord.ui.View):
    """Shown (only to the member) when they press Join again: progress + a Leave button."""

    def __init__(self, cog: "Giveaway", gid: int, mid: str, uid: int):
        super().__init__(timeout=90)
        self.cog, self.gid, self.mid, self.uid = cog, gid, mid, uid
        b = discord.ui.Button(label="Leave giveaway", emoji=I.e("logout"), style=discord.ButtonStyle.danger)
        b.callback = self.leave
        self.add_item(b)

    async def leave(self, interaction: discord.Interaction):
        rec = self.cog.data(self.gid)["active"].get(self.mid)
        if rec is not None and self.uid in rec["entries"]:
            rec["entries"].remove(self.uid)
            self.cog.mark(self.gid)
            self.cog.touch(self.gid, rec)
        await interaction.response.edit_message(content=ok("You left the giveaway. Press **Join** again any time before it ends."),
                                                embed=None, view=None)


# ------------------------------------------------------------------ the cog
class Giveaway(commands.Cog):
    """Giveaways with a full builder panel, templates, requirements and role prizes."""

    def __init__(self, bot):
        self.bot = bot
        self._cfg: dict[int, dict] = {}               # guild id -> saved data (cleared by $backupload so it is re-read)
        self._gids: set[int] = set()                  # guilds that have giveaway data
        self._dirty: set[int] = set()
        self._refresh: dict[tuple, float] = {}        # (gid, message id) -> last redraw
        self._wanted: set[tuple] = set()              # (gid, message id) waiting for a redraw
        self._ending: set[str] = set()
        self._invites: dict[int, dict] = {}           # gid -> {code: (uses, max_uses, inviter)}
        self._inv_locks: dict[int, asyncio.Lock] = {}
        self._last_flush = 0.0
        self._last_roles = 0.0
        self._last_scan = 0.0

    # ------------------------------------------------------------ storage
    def data(self, gid: int) -> dict:
        d = self._cfg.get(gid)
        if d is None:
            d = self._cfg[gid] = gl.fill_data(self.bot.db.get_settings(gid, KEY))
            self._gids.add(gid)
        return d

    def save(self, gid: int):
        self._dirty.discard(gid)
        if gid in self._cfg:
            self.bot.db.set_settings(gid, KEY, self._cfg[gid])

    def mark(self, gid: int):
        self._dirty.add(gid)

    def flush_pending(self):
        """Write everything that is only in memory (counted messages, entries) to the database. Called before every backup."""
        for gid in list(self._dirty):
            self.save(gid)

    def scan_guilds(self):
        self._last_scan = time.monotonic()
        for (gid,) in self.bot.db.conn.execute("SELECT DISTINCT guild_id FROM settings WHERE key=?", (KEY,)):
            self._gids.add(gid)
        for gid in list(self._gids):
            self.data(gid)                             # (re)load into memory

    async def cog_load(self):
        self.scan_guilds()
        self.loop.start()

    async def cog_unload(self):
        self.loop.cancel()
        self.flush_pending()

    # ------------------------------------------------------------ who may manage
    def is_manager(self, member) -> bool:
        if not isinstance(member, discord.Member):
            return False
        p = member.guild_permissions
        return p.administrator or p.manage_guild or self.bot.is_owner_fast(member.id, member.guild.id)

    # ------------------------------------------------------------ text pieces
    @staticmethod
    def host_text(rec: dict) -> str:
        return f"<@{rec['host']}>" if rec.get("host") else "-"

    def prize_text(self, guild: discord.Guild, cfg: dict) -> str:
        parts = [cfg.get("prize") or "-"]
        pr = cfg.get("prize_role") or {}
        if pr.get("role"):
            role = guild.get_role(int(pr["role"]))
            when = "permanent" if pr.get("mode") != "temporary" else f"for {gl.fmt_dur(pr.get('seconds') or 0)}"
            parts.append(f"{role.mention if role else '`deleted role`'} ({when})")
        if cfg.get("points"):
            parts.append(f"{int(cfg['points']):,} points")
        return " · ".join(parts)

    def req_lines(self, guild: discord.Guild, cfg: dict) -> list:
        req, out = cfg.get("req") or {}, []
        roles = [int(r) for r in req.get("roles") or []]
        if roles:
            word = "all of" if req.get("roles_mode") == "all" else "one of"
            out.append(f"• Have {word}: " + " ".join(f"<@&{r}>" for r in roles))
        m = req.get("messages") or {}
        if m.get("count"):
            out.append(f"• Send **{int(m['count']):,}** messages while the giveaway runs")
            if m.get("exclude"):
                out.append("-# Not counted in: " + " ".join(f"<#{c}>" for c in m["exclude"][:10]))
        if req.get("invites"):
            out.append(f"• Invite **{int(req['invites'])}** {'person' if int(req['invites']) == 1 else 'people'} to the server")
        t = req.get("task") or {}
        if t.get("enabled") and t.get("phrase"):
            line = f"• Say **{t['phrase']}**"
            if t.get("target"):
                line += f" to <@{t['target']}>"
            if t.get("channel"):
                line += f" in <#{t['channel']}>"
            out.append(line)
        return out

    def check_lines(self, guild: discord.Guild, cfg: dict, checks: list) -> list:
        req = cfg.get("req") or {}
        done, todo = gl.emoji_of(cfg, "done"), gl.emoji_of(cfg, "todo")
        out = []
        for c in checks:
            mark = done if c["ok"] else todo
            if c["key"] == "roles":
                roles = " ".join(f"<@&{r}>" for r in req.get("roles") or [])
                word = "all of" if req.get("roles_mode") == "all" else "one of"
                out.append(pre(mark, f"Have {word}: {roles}"))
            elif c["key"] == "messages":
                out.append(pre(mark, f"Messages: **{c['have']:,}/{c['need']:,}**"))
            elif c["key"] == "invites":
                out.append(pre(mark, f"Invites: **{c['have']}/{c['need']}**"))
            elif c["key"] == "task":
                t = req["task"]
                line = f"Say **{t['phrase']}**" + (f" to <@{t['target']}>" if t.get("target") else "")
                out.append(pre(mark, line + (f" in <#{t['channel']}>" if t.get("channel") else "")))
        return out

    # ------------------------------------------------------------ the giveaway message
    def embed_for(self, guild: discord.Guild, rec: dict, image: Optional[str] = None, thumb: Optional[str] = None) -> discord.Embed:
        cfg = rec["cfg"]
        E = lambda k: gl.emoji_of(cfg, k)
        status = rec.get("status", "active")
        entries = len(rec.get("entries") or [])
        n = rec.get("n")
        tag = f"Giveaway #{n} · " if n else "Preview · "
        if status == "active":
            end = int(rec["end"])
            lines = []
            if cfg.get("description"):
                lines += [cfg["description"], ""]
            lines += [pre(E("prize"), f"**Prize:** {self.prize_text(guild, cfg)}"),
                      pre(E("winners"), f"**Winners:** {cfg['winners']}"),
                      pre(E("host"), f"**Hosted by:** {self.host_text(rec)}"),
                      pre(E("ends"), f"**Ends:** <t:{end}:R> (<t:{end}:f>)")]
            reqs = self.req_lines(guild, cfg)
            if reqs:
                lines += ["", pre(E("req"), "**Requirements**")] + reqs
                lines += ["", "-# Only members who finish **every** requirement before the end can win."]
            e = discord.Embed(title=pre(E("title"), f"{cfg['title']} {E('title')}".strip())[:256], description="\n".join(lines)[:4000],
                              color=int(cfg.get("color") or BRAND))
            e.set_footer(text=f"{tag}{entries} {'entry' if entries == 1 else 'entries'} · Ends")
            e.timestamp = dt.datetime.fromtimestamp(end, dt.timezone.utc)
        else:
            cancelled = status == "cancelled"
            winners = rec.get("winners") or []
            when = int(rec.get("ended") or time.time())
            lines = [pre(E("prize"), f"**Prize:** {self.prize_text(guild, cfg)}")]
            if cancelled:
                lines.append(pre(E("ended"), "**This giveaway was cancelled.**"))
            else:
                lines.append(pre(E("winners"), "**Winners:** " + (" ".join(f"<@{u}>" for u in winners) if winners
                                                                  else "*nobody finished all requirements*")))
            lines += [pre(E("host"), f"**Hosted by:** {self.host_text(rec)}"),
                      pre(E("ends"), f"**Ended:** <t:{when}:R> (<t:{when}:f>)")]
            if not cancelled and gl.has_requirements(cfg) and rec.get("valid") is not None:
                lines.append(pre(E("entries"), f"**Entries:** {entries} · finished the requirements: **{rec['valid']}**"))
            e = discord.Embed(title=pre(E("ended"), f"{cfg['title']} · {'CANCELLED' if cancelled else 'ENDED'}")[:256],
                              description="\n".join(lines)[:4000], color=GREY if cancelled else RED)
            e.set_footer(text=f"{tag}{entries} {'entry' if entries == 1 else 'entries'}")
        if image:
            e.set_image(url=image)
        if thumb:
            e.set_thumbnail(url=thumb)
        return e

    async def view_for(self, rec: dict) -> discord.ui.View:
        cfg = rec["cfg"]
        b = cfg["buttons"]
        status = rec.get("status", "active")
        gone = await emojis.missing(self.bot, b["join"].get("emoji"), b["entries"].get("emoji"), b["progress"].get("emoji"))

        def em(text):
            pe = emojis.to_partial_checked(text) if text else None
            return None if (pe is not None and pe.id in gone) else pe
        v = discord.ui.View(timeout=None)
        j = b["join"]
        if status == "active":
            v.add_item(Noop(label=(j.get("label") or "Join Giveaway")[:80], emoji=em(j.get("emoji")),
                            style=STYLES.get(j.get("style"), discord.ButtonStyle.primary), custom_id=CID + "join"))
        else:
            v.add_item(Noop(label=(j.get("ended_label") or "Giveaway Ended")[:80], style=discord.ButtonStyle.secondary,
                            custom_id=CID + "join", disabled=True))
        if b["entries"].get("show", True):
            v.add_item(Noop(label=str(len(rec.get("entries") or [])), emoji=em(b["entries"].get("emoji")),
                            style=discord.ButtonStyle.secondary, custom_id=CID + "entries"))
        if status == "active" and gl.has_requirements(cfg) and b["progress"].get("show", True):
            v.add_item(Noop(label=(b["progress"].get("label") or "My Progress")[:80], emoji=em(b["progress"].get("emoji")),
                            style=discord.ButtonStyle.secondary, custom_id=CID + "progress"))
        if status == "ended":
            v.add_item(Noop(label="Reroll", emoji=I.e("refresh"), style=discord.ButtonStyle.secondary, custom_id=CID + "reroll"))
        return v

    async def render_message(self, guild: discord.Guild, rec: dict):
        """Redraw the giveaway message (entry counter, ended state ...). Quiet when the message is gone."""
        ch = guild.get_channel_or_thread(rec["ch"])
        if ch is None or not rec.get("msg"):
            return
        cfg = rec["cfg"]
        image = await media.fresh(self.bot, cfg.get("image"))
        thumb = await media.fresh(self.bot, cfg.get("thumbnail"))
        try:
            await ch.get_partial_message(rec["msg"]).edit(embed=self.embed_for(guild, rec, image, thumb),
                                                          view=await self.view_for(rec), allowed_mentions=NONE)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            log.warning("giveaway #%s message edit failed: %s", rec.get("n"), e)

    def touch(self, gid: int, rec: dict):
        """Ask for a (rate limited) redraw of the giveaway message."""
        self._wanted.add((gid, str(rec["msg"])))

    # ------------------------------------------------------------ requirements of one member
    def valid_invites(self, guild: discord.Guild, rec: dict, uid: int) -> int:
        return sum(1 for i in (rec.get("inv") or {}).get(str(uid), []) if guild.get_member(int(i)) is not None)

    def checks_for(self, guild: discord.Guild, rec: dict, member: discord.Member) -> list:
        return gl.evaluate(rec["cfg"], rec, member.id, {r.id for r in member.roles}, self.valid_invites(guild, rec, member.id))

    def eligible(self, guild: discord.Guild, rec: dict) -> list:
        out = []
        for uid in rec.get("entries") or []:
            m = guild.get_member(int(uid))
            if m is not None and not m.bot and gl.qualified(self.checks_for(guild, rec, m)):
                out.append(int(uid))
        return out

    # ------------------------------------------------------------ rewards
    def check_role_reward(self, guild: discord.Guild, cfg: dict):
        rid = (cfg.get("prize_role") or {}).get("role")
        if not rid:
            return
        role = guild.get_role(int(rid))
        if role is None:
            raise VCError("The prize role doesn't exist any more - pick it again.")
        me = guild.me
        if not me.guild_permissions.manage_roles:
            raise VCError("I need the **Manage Roles** permission to hand out the prize role.")
        if role.managed or role.is_default() or role >= me.top_role:
            raise VCError(f"I can't give {role.mention} - move **my role above it** (and it can't be a bot / integration role).")

    async def reward(self, guild: discord.Guild, rec: dict, member: discord.Member) -> list:
        """Give the role / points of the prize to one winner. Returns problems as text lines."""
        cfg, problems = rec["cfg"], []
        pr = cfg.get("prize_role") or {}
        if pr.get("role"):
            role = guild.get_role(int(pr["role"]))
            if role is None:
                problems.append("the prize role was deleted")
            else:
                had = role in member.roles
                try:
                    if not had:
                        await member.add_roles(role, reason=f"Giveaway #{rec['n']} prize")
                    if pr.get("mode") == "temporary" and not had:
                        d = self.data(guild.id)
                        d["temp_roles"].append({"u": member.id, "r": role.id, "exp": time.time() + int(pr.get("seconds") or 86400),
                                                "gw": rec["n"], "tries": 0})
                except discord.HTTPException:
                    problems.append(f"I couldn't give {role.mention} to {member.mention} (my role must be above it)")
        if cfg.get("points"):
            self.bot.db.add_points(guild.id, member.id, int(cfg["points"]), f"giveaway #{rec['n']}")
        return problems

    # ------------------------------------------------------------ end / reroll
    async def draw(self, guild: discord.Guild, rec: dict, count: int, exclude=()) -> tuple:
        eligible = self.eligible(guild, rec)
        rec["valid"] = len(eligible)
        chosen = gl.pick_winners(eligible, count, exclude=exclude)
        problems = []
        for uid in chosen:
            m = guild.get_member(uid)
            if m is not None:
                problems += await self.reward(guild, rec, m)
        return chosen, problems

    async def end_giveaway(self, gid: int, mid, *, cancel: bool = False):
        key = str(mid)
        if key in self._ending:
            return
        self._ending.add(key)
        try:
            d = self.data(gid)
            rec = d["active"].get(key)
            guild = self.bot.get_guild(gid)
            if rec is None or guild is None:
                return
            winners, problems = [], []
            if not cancel:
                winners, problems = await self.draw(guild, rec, int(rec["cfg"]["winners"]))
            rec["status"] = "cancelled" if cancel else "ended"
            rec["ended"], rec["winners"] = time.time(), winners
            d["active"].pop(key, None)
            d["ended"][key] = rec
            gl.prune_ended(d["ended"])
            self.save(gid)
            await self.render_message(guild, rec)
            if not cancel:
                await self.announce(guild, rec, winners, problems)
        except Exception:
            log.exception("ending giveaway %s failed", key)
        finally:
            self._ending.discard(key)

    async def announce(self, guild: discord.Guild, rec: dict, winners: list, problems: list, reroll: bool = False):
        ch = guild.get_channel_or_thread(rec["ch"])
        if ch is None:
            return
        cfg = rec["cfg"]
        E = lambda k: gl.emoji_of(cfg, k)
        link = f"https://discord.com/channels/{guild.id}/{rec['ch']}/{rec['msg']}"
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="Jump to giveaway", url=link, emoji=I.e("link")))
        if rec.get("status") == "ended":                     # admins can reroll right from here (works after restarts: id in custom_id)
            v.add_item(Noop(label="Reroll", emoji=I.e("refresh"), style=discord.ButtonStyle.secondary,
                            custom_id=f"{CID}reroll:{rec['msg']}"))
        prize = self.prize_text(guild, cfg)
        if winners:
            mentions = " ".join(f"<@{u}>" for u in winners)
            e = discord.Embed(color=GREEN, title=pre(E("winners"), "New winner!" if reroll else "Congratulations!"),
                              description=f"{mentions}\nYou won **{prize}**" + (f"\n-# Hosted by {self.host_text(rec)}" if rec.get("host") else ""))
            content = mentions
        else:
            e = discord.Embed(color=RED, title=pre(E("ended"), "No valid winners"),
                              description=f"Nobody finished all requirements for **{prize}**."
                                          + ("\nUse the **Reroll** button to try again." if rec.get("entries") else ""))
            content = None
        if problems:
            e.add_field(name="Heads up", value="\n".join(f"{I.s('warning')} {p}" for p in problems)[:1000], inline=False)
        try:
            await ch.send(content=content, embed=e, view=v, allowed_mentions=PEOPLE)
        except discord.HTTPException as ex:
            log.warning("giveaway announce failed: %s", ex)
        for uid in winners:                                     # a DM is a bonus - closed DMs are fine
            m = guild.get_member(uid)
            if m is None:
                continue
            try:
                dm = discord.Embed(color=GREEN, title=pre(E("winners"), f"You won in {guild.name}!"),
                                   description=f"**{prize}**\n[Jump to the giveaway]({link})")
                await m.send(embed=dm)
            except discord.HTTPException:
                pass

    async def reroll(self, guild: discord.Guild, rec: dict, count: Optional[int] = None) -> list:
        count = max(1, min(int(count or rec["cfg"]["winners"]), gl.MAX_WINNERS))
        winners, problems = await self.draw(guild, rec, count, exclude=rec.get("winners") or [])
        rec["winners"] = list(rec.get("winners") or []) + winners
        self.save(guild.id)
        await self.render_message(guild, rec)
        await self.announce(guild, rec, winners, problems, reroll=True)
        return winners

    # ------------------------------------------------------------ posting
    async def post_giveaway(self, guild: discord.Guild, channel, cfg: dict, host_id: int):
        cfg = copy.deepcopy(cfg)
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise VCError(f"I need **View Channel, Send Messages and Embed Links** in {channel.mention}.")
        self.check_role_reward(guild, cfg)
        if cfg["req"].get("invites"):
            if not guild.me.guild_permissions.manage_guild:
                raise VCError("I need the **Manage Server** permission to count invites.")
            snap = await self.snapshot_invites(guild)
            if snap is None:
                raise VCError("I couldn't read the server invites (need **Manage Server**).")
            self._invites[guild.id] = snap
        d = self.data(guild.id)
        now = time.time()
        rec = {"n": d["counter"] + 1, "gid": guild.id, "ch": channel.id, "msg": None, "host": host_id, "start": now,
               "end": int(now + int(cfg["duration"])), "cfg": cfg, "entries": [], "counts": {}, "inv": {}, "task": [],
               "status": "active"}
        image = await media.fresh(self.bot, cfg.get("image"))
        thumb = await media.fresh(self.bot, cfg.get("thumbnail"))
        try:
            msg = await channel.send(embed=self.embed_for(guild, rec, image, thumb), view=await self.view_for(rec), allowed_mentions=NONE)
        except discord.HTTPException as e:
            raise VCError(f"Couldn't post the giveaway: {e.text}")
        d["counter"] = rec["n"]
        rec["msg"] = msg.id
        d["active"][str(msg.id)] = rec
        last = copy.deepcopy(cfg)
        last["channel"] = channel.id
        d["last"] = last
        self.save(guild.id)
        return rec, msg

    # ------------------------------------------------------------ background loop
    @tasks.loop(seconds=5)
    async def loop(self):
        now = time.time()
        if time.monotonic() - self._last_scan > 60 or any(g not in self._cfg for g in self._gids):
            self.scan_guilds()
        for gid in list(self._gids):
            d = self._cfg.get(gid)
            if not d:
                continue
            for mid, rec in list(d["active"].items()):
                if rec["end"] <= now and mid not in self._ending and self.bot.get_guild(gid) is not None:
                    asyncio.create_task(self.end_giveaway(gid, mid))
        for gid, mid in list(self._wanted)[:5]:                          # redraw entry counters, a few per round
            if time.monotonic() - self._refresh.get((gid, mid), 0) < EDIT_EVERY:
                continue
            self._wanted.discard((gid, mid))
            self._refresh[(gid, mid)] = time.monotonic()
            guild, d = self.bot.get_guild(gid), self._cfg.get(gid)
            rec = d["active"].get(mid) if d else None
            if guild is not None and rec is not None:
                await self.render_message(guild, rec)
        if time.monotonic() - self._last_roles > 30:
            self._last_roles = time.monotonic()
            await self.expire_roles()
        if time.monotonic() - self._last_flush > FLUSH_EVERY:
            self._last_flush = time.monotonic()
            self.flush_pending()

    @loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()
        for gid in list(self._gids):                                      # invite tracking only where a giveaway needs it
            d = self._cfg.get(gid)
            g = self.bot.get_guild(gid)
            if g and d and any(int(r["cfg"]["req"].get("invites") or 0) for r in d["active"].values()):
                snap = await self.snapshot_invites(g)
                if snap is not None:
                    self._invites[gid] = snap

    @loop.error
    async def _loop_err(self, e):
        log.error("giveaway loop crashed: %r", e)
        await asyncio.sleep(10)
        if not self.loop.is_running():
            self.loop.restart()

    async def expire_roles(self):
        """Temporary prize roles: take them back when the time is over (survives restarts - the list is in the database)."""
        now = time.time()
        for gid in list(self._gids):
            d = self._cfg.get(gid)
            guild = self.bot.get_guild(gid)
            if not d or guild is None or not d["temp_roles"]:
                continue
            keep, changed = [], False
            for it in d["temp_roles"]:
                if it["exp"] > now:
                    keep.append(it)
                    continue
                changed = True
                member, role = guild.get_member(it["u"]), guild.get_role(it["r"])
                if member is None or role is None or role not in member.roles:
                    continue
                try:
                    await member.remove_roles(role, reason=f"Giveaway #{it.get('gw')} role expired")
                except discord.HTTPException:
                    it["tries"] = it.get("tries", 0) + 1
                    if it["tries"] < 5:
                        keep.append(it)
            if changed:
                d["temp_roles"] = keep
                self.save(gid)

    # ------------------------------------------------------------ invite tracking
    async def snapshot_invites(self, guild: discord.Guild) -> Optional[dict]:
        if not guild.me.guild_permissions.manage_guild:
            return None
        try:
            invs = await guild.invites()
        except discord.HTTPException:
            return None
        return {i.code: (i.uses or 0, i.max_uses or 0, i.inviter.id if i.inviter else None) for i in invs}

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild is not None and invite.guild.id in self._invites:
            self._invites[invite.guild.id][invite.code] = (invite.uses or 0, invite.max_uses or 0,
                                                           invite.inviter.id if invite.inviter else None)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        # kept on purpose: a single-use invite is deleted by Discord when it is used - the next join check still needs it
        pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        gid = member.guild.id
        if gid not in self._invites:
            return
        async with self._inv_locks.setdefault(gid, asyncio.Lock()):
            cur = await self.snapshot_invites(member.guild)
            if cur is None:
                return
            inviter = gl.diff_invites(self._invites.get(gid, {}), cur)
            self._invites[gid] = cur
        if not inviter or member.bot or inviter == member.id:
            return
        d, now, changed = self.data(gid), time.time(), False
        for rec in d["active"].values():
            if int(rec["cfg"]["req"].get("invites") or 0) and rec["start"] <= now < rec["end"]:
                lst = rec.setdefault("inv", {}).setdefault(str(inviter), [])
                if member.id not in lst:
                    lst.append(member.id)
                    changed = True
        if changed:
            self.mark(gid)

    # ------------------------------------------------------------ counting messages + the "say it" task
    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if m.guild is None or m.author.bot:
            return
        d = self._cfg.get(m.guild.id)
        if not d or not d["active"]:
            return
        now = time.time()
        ch = m.channel
        parent = getattr(ch, "parent", None)
        ids = (ch.id, getattr(ch, "parent_id", None), getattr(ch, "category_id", None), getattr(parent, "category_id", None))
        changed = False
        for rec in d["active"].values():
            if not (rec["start"] <= now < rec["end"]):
                continue
            req = rec["cfg"]["req"]
            mc = req.get("messages") or {}
            if mc.get("count") and not gl.channel_excluded(mc.get("exclude") or [], *ids):
                counts = rec.setdefault("counts", {})
                counts[str(m.author.id)] = counts.get(str(m.author.id), 0) + 1
                changed = True
            task = req.get("task") or {}
            if task.get("enabled") and m.author.id not in rec.setdefault("task", []):
                reply_to = None
                if m.reference is not None and isinstance(m.reference.resolved, discord.Message):
                    reply_to = m.reference.resolved.author.id
                if gl.task_matches(task, m.content, ids, [u.id for u in m.mentions], reply_to):
                    rec["task"].append(m.author.id)
                    changed = True
                    try:
                        await m.add_reaction("✅")
                    except discord.HTTPException:
                        pass
        if changed:
            self.mark(m.guild.id)

    # ------------------------------------------------------------ buttons under the giveaway
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component or interaction.guild is None:
            return
        cid = (interaction.data or {}).get("custom_id") or ""
        if not cid.startswith(CID):
            return
        action = cid[len(CID):]
        try:
            if action == "join":
                await self.on_join(interaction)
            elif action == "entries":
                await self.on_entries(interaction)
            elif action == "progress":
                await self.on_progress(interaction)
            elif action == "reroll" or action.startswith("reroll:"):
                await self.on_reroll_button(interaction, action.partition(":")[2])
        except VCError as e:
            await say(interaction, err(str(e)))
        except discord.HTTPException as e:
            log.warning("giveaway interaction failed: %s", e)
            await say(interaction, err("Discord refused that. Try again in a moment."))
        except Exception:
            log.exception("giveaway interaction %s crashed", action)
            await say(interaction, err("That didn't work. Try again in a moment."))

    def find_rec(self, interaction: discord.Interaction, ended: bool = False) -> Optional[dict]:
        d = self.data(interaction.guild.id)
        key = str(interaction.message.id)
        return d["active"].get(key) or (d["ended"].get(key) if ended else None)

    def progress_embed(self, guild: discord.Guild, rec: dict, member: discord.Member, title: str) -> discord.Embed:
        cfg = rec["cfg"]
        checks = self.checks_for(guild, rec, member)
        e = discord.Embed(color=GREEN if gl.qualified(checks) else BRAND, title=title)
        if not checks:
            e.description = "No requirements - you're all set. Good luck!"
        else:
            e.description = "\n".join(self.check_lines(guild, cfg, checks))
            e.set_footer(text="Finish every line before the giveaway ends - unfinished entries can't win.")
        return e

    async def on_join(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        rec = self.find_rec(interaction)
        if rec is None or rec["end"] <= time.time():
            raise VCError("This giveaway is over.")
        if not isinstance(member, discord.Member):
            raise VCError("Join from inside the server.")
        checks = self.checks_for(guild, rec, member)
        gate = next((c for c in checks if c["key"] == "roles"), None)
        if gate is not None and not gate["ok"]:
            req = rec["cfg"]["req"]
            word = "all of" if req.get("roles_mode") == "all" else "one of"
            raise VCError(f"You need {word}: " + " ".join(f"<@&{r}>" for r in req["roles"]) + " to join this giveaway.")
        if member.id in rec["entries"]:
            return await interaction.response.send_message(
                embed=self.progress_embed(guild, rec, member, "You're already in"),
                view=LeaveView(self, guild.id, str(interaction.message.id), member.id), ephemeral=True)
        rec["entries"].append(member.id)
        self.mark(guild.id)
        self.touch(guild.id, rec)
        await interaction.response.send_message(embed=self.progress_embed(guild, rec, member, "You're in! " + gl.emoji_of(rec["cfg"], "title")),
                                                ephemeral=True)

    async def on_progress(self, interaction: discord.Interaction):
        rec = self.find_rec(interaction)
        if rec is None:
            raise VCError("This giveaway is over.")
        member = interaction.user
        joined = member.id in rec["entries"]
        e = self.progress_embed(interaction.guild, rec, member, "Your progress")
        if not joined:
            e.add_field(name="Not joined yet", value="Press **Join** first - then finish the requirements.", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def on_entries(self, interaction: discord.Interaction):
        rec = self.find_rec(interaction, ended=True)
        if rec is None:
            raise VCError("I don't know this giveaway any more.")
        guild = interaction.guild
        total = len(rec["entries"])
        e = discord.Embed(color=BRAND, title=pre(gl.emoji_of(rec["cfg"], "entries"), "Entries"))
        e.description = f"**{total}** {'person' if total == 1 else 'people'} joined."
        if gl.has_requirements(rec["cfg"]) and rec.get("status", "active") == "active":
            n = len(self.eligible(guild, rec))
            e.description += f"\n**{n}** finished all requirements so far."
        if self.is_manager(interaction.user) and total:
            names = []
            for uid in rec["entries"][:40]:
                m = guild.get_member(int(uid))
                good = m is not None and gl.qualified(self.checks_for(guild, rec, m))
                names.append(f"{gl.emoji_of(rec['cfg'], 'done') if good else gl.emoji_of(rec['cfg'], 'todo')} <@{uid}>")
            e.add_field(name="First 40 (admins only)", value=" ".join(names)[:1000], inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)

    async def on_reroll_button(self, interaction: discord.Interaction, mid: str = ""):
        """Reroll button: on the ended giveaway message (id = that message) or under the winner announcement (id in the custom_id)."""
        if not self.is_manager(interaction.user):
            raise VCError("Only admins can reroll.")
        d = self.data(interaction.guild.id)
        rec = d["ended"].get(mid or str(interaction.message.id))
        if rec is None:
            raise VCError("I don't have this giveaway any more.")
        await interaction.response.defer(ephemeral=True)
        winners = await self.reroll(interaction.guild, rec, 1)
        await interaction.followup.send(ok("New winner picked.") if winners else err("Nobody else finished the requirements."), ephemeral=True)

    # ------------------------------------------------------------ commands
    def find(self, gid: int, ident, *, states=("active",)):
        d = self.data(gid)
        pools = [d[s] for s in states]
        for pool in pools:
            if str(ident) in pool:
                return pool[str(ident)]
        for pool in pools:
            for rec in pool.values():
                if rec["n"] == ident:
                    return rec
        return None

    async def pick(self, ctx: commands.Context, ident: Optional[int], states=("active",)) -> dict:
        d = self.data(ctx.guild.id)
        rec = None
        if ident is not None:
            rec = self.find(ctx.guild.id, ident, states=states)
        elif ctx.message.reference is not None and ctx.message.reference.message_id:
            rec = self.find(ctx.guild.id, ctx.message.reference.message_id, states=states)
        else:
            pool = [r for s in states for r in d[s].values()]
            if len(pool) == 1:
                rec = pool[0]
            elif len(pool) > 1:
                pool.sort(key=lambda r: r["n"], reverse=True)
                if states == ("ended",):
                    rec = pool[0]
                else:
                    raise VCError("More than one giveaway is running - reply to it, or give the number: "
                                  + ", ".join(f"`#{r['n']}`" for r in pool[:10]))
        if rec is None:
            raise VCError("I couldn't find that giveaway. Reply to its message, or use its number / message id (`$giveaway list`).")
        return rec

    @commands.group(name="giveaway", aliases=["giveway", "gw", "gcreate"], invoke_without_command=True)
    @commands.guild_only()
    @kon_admin_gw()
    async def giveaway(self, ctx: commands.Context):
        """$giveaway - open the giveaway builder (prize, time, emojis, buttons, requirements, templates)."""
        view = GiveawayBuilder(self, ctx.author, ctx.guild, ctx.channel)
        view.message = await ctx.reply(embeds=await view.embeds(), view=view, mention_author=False)

    @giveaway.command(name="end", aliases=["stop"])
    @commands.guild_only()
    @kon_admin_gw()
    async def giveaway_end(self, ctx: commands.Context, ident: Optional[int] = None):
        """$giveaway end [id] - end a running giveaway now and pick the winners."""
        rec = await self.pick(ctx, ident)
        await self.end_giveaway(ctx.guild.id, rec["msg"])
        await ctx.reply(ok(f"Giveaway **#{rec['n']}** ended."), mention_author=False)

    @giveaway.command(name="reroll")
    @commands.guild_only()
    @kon_admin_gw()
    async def giveaway_reroll(self, ctx: commands.Context, ident: Optional[int] = None, count: int = 0):
        """$giveaway reroll [id] [count] - pick new winner(s) of an ended giveaway."""
        if ident is not None and 0 < ident <= gl.MAX_WINNERS and ctx.message.reference is not None and not count:
            ident, count = None, ident                  # "$giveaway reroll 2" as a reply = 2 new winners
        rec = await self.pick(ctx, ident, states=("ended",))
        winners = await self.reroll(ctx.guild, rec, count or None)
        if not winners:
            raise VCError("Nobody else finished the requirements.")

    @giveaway.command(name="cancel", aliases=["delete"])
    @commands.guild_only()
    @kon_admin_gw()
    async def giveaway_cancel(self, ctx: commands.Context, ident: Optional[int] = None):
        """$giveaway cancel [id] - cancel a running giveaway (no winners)."""
        rec = await self.pick(ctx, ident)
        await self.end_giveaway(ctx.guild.id, rec["msg"], cancel=True)
        await ctx.reply(ok(f"Giveaway **#{rec['n']}** cancelled."), mention_author=False)

    @giveaway.command(name="list", aliases=["ls"])
    @commands.guild_only()
    @kon_admin_gw()
    async def giveaway_list(self, ctx: commands.Context):
        """$giveaway list - running and recently ended giveaways."""
        d = self.data(ctx.guild.id)

        def line(r):
            return f"`#{r['n']}` [{(r['cfg'].get('prize') or '-')[:40]}](https://discord.com/channels/{ctx.guild.id}/{r['ch']}/{r['msg']})"
        act = sorted(d["active"].values(), key=lambda r: r["end"])
        done = sorted(d["ended"].values(), key=lambda r: r.get("ended") or 0, reverse=True)[:8]
        e = discord.Embed(color=BRAND, title=f"{I.s('gift')} Giveaways")
        e.add_field(name=f"Running ({len(act)})", inline=False, value="\n".join(
            f"{line(r)} · ends <t:{int(r['end'])}:R> · {len(r['entries'])} entries" for r in act[:10]) or "*none*")
        e.add_field(name="Recently ended", inline=False, value="\n".join(
            f"{line(r)} · {'cancelled' if r.get('status') == 'cancelled' else 'ended'} <t:{int(r.get('ended') or 0)}:R>"
            for r in done) or "*none*")
        e.set_footer(text=f"{PREFIX}giveaway end / reroll / cancel [id] · reply to a giveaway instead of typing the id")
        await ctx.reply(embed=e, mention_author=False)

    @giveaway.command(name="templates", aliases=["template", "saved"])
    @commands.guild_only()
    @kon_admin_gw()
    async def giveaway_templates(self, ctx: commands.Context):
        """$giveaway templates - saved giveaway templates."""
        t = self.data(ctx.guild.id)["templates"]
        e = discord.Embed(color=BRAND, title=f"{I.s('folder')} Giveaway templates", description=(
            "\n".join(f"• **{v['name']}** · {(v['cfg'].get('prize') or '-')[:40]}" for v in t.values())
            or "*none yet - build a giveaway and press **Save template**.*"))
        e.set_footer(text=f"{PREFIX}giveaway load <name> opens the builder with a template")
        await ctx.reply(embed=e, mention_author=False)

    @giveaway.command(name="load")
    @commands.guild_only()
    @kon_admin_gw()
    async def giveaway_load(self, ctx: commands.Context, *, name: str):
        """$giveaway load <name> - open the builder with a saved template."""
        t = self.data(ctx.guild.id)["templates"].get(name.strip().lower())
        if t is None:
            raise VCError("I don't have a template with that name (`$giveaway templates`).")
        view = GiveawayBuilder(self, ctx.author, ctx.guild, ctx.channel, cfg=t["cfg"], name=t["name"])
        view.message = await ctx.reply(embeds=await view.embeds(), view=view, mention_author=False)


# ------------------------------------------------------------------ the builder panel
class GiveawayBuilder(OwnedView):
    """$giveaway - home -> (basic info | prize | requirements -> task | look & emojis | buttons | templates) -> Start."""

    def __init__(self, cog: Giveaway, invoker: discord.Member, guild: discord.Guild, channel, cfg: Optional[dict] = None,
                 name: Optional[str] = None):
        super().__init__(invoker.id, timeout=1500)
        self.cog, self.guild = cog, guild
        self.cfg = gl.full_config(cfg) if cfg else gl.new_config()
        saved_ch = self.cfg.get("channel")
        self.channel_id = saved_ch if (saved_ch and guild.get_channel(saved_ch)) else channel.id
        self.host_id = invoker.id
        self.page = "home"
        self.loaded = name
        self.note = ""
        self.render()

    # ----- small helpers
    @property
    def req(self) -> dict:
        return self.cfg["req"]

    def chan(self, cid) -> str:
        return f"<#{cid}>" if cid else "*not set*"

    async def norm(self, text: str, default: str = "") -> str:
        """What was typed -> an emoji string. Empty = `default`, 'none' = no emoji."""
        t = (text or "").strip()
        if not t:
            return default
        if t.lower() in ("none", "off", "hide", "-"):
            return ""
        return await emojis.normalize(self.cog.bot, self.guild, t) or ""

    def prec(self) -> dict:
        return {"cfg": self.cfg, "host": self.host_id, "end": int(time.time() + int(self.cfg["duration"])),
                "status": "active", "entries": [], "n": None}

    # ----- embeds
    async def embeds(self) -> list:
        note, self.note = self.note, ""
        page = self.page_embed(note)
        prev = self.cog.embed_for(self.guild, self.prec(), (self.cfg.get("image") or {}).get("url"),
                                  (self.cfg.get("thumbnail") or {}).get("url"))
        prev.set_author(name="Preview - this is how the giveaway will look")
        return [page, prev]

    def emoji_line(self) -> str:
        return " ".join(f"{gl.emoji_of(self.cfg, k) or '·'}" for k in gl.EMOJI_FIELDS)

    def page_embed(self, note: str) -> discord.Embed:
        c, p = self.cfg, self.page
        if p == "prize":
            pr = c["prize_role"]
            role = self.guild.get_role(int(pr["role"])) if pr.get("role") else None
            e = discord.Embed(color=BRAND, title=f"{I.s('gift')} Prize & rewards", description=(
                "Winners can get a **role** (permanent, or temporary - I take it back by myself) and / or **Kon points**. "
                "The prize text itself is set in **Basic info**.\n\n"
                f"**Role:** {role.mention if role else '*none*'}"
                + (f" · **{'Permanent' if pr.get('mode') != 'temporary' else 'Temporary - ' + gl.fmt_dur(pr.get('seconds') or 0)}**" if role else "")
                + f"\n**Points:** {int(c.get('points') or 0):,}"))
        elif p == "req":
            lines = self.cog.req_lines(self.guild, c)
            e = discord.Embed(color=BRAND, title=f"{I.s('shield_check')} Requirements", description=(
                "People can join any time, but **only members who finished every requirement when the giveaway ends can win** - "
                "no task, no reward.\n\n" + ("\n".join(lines) if lines else "*No requirements - anyone can win.*")))
            e.set_footer(text="Messages are counted from the moment the giveaway starts. Invites need the Manage Server permission.")
        elif p == "task":
            t = self.req["task"]
            who = "<@" + str(t["target"]) + ">" if t.get("target") else "*anyone / nobody needed*"
            where = self.chan(t.get("channel")) if t.get("channel") else "*any channel*"
            e = discord.Embed(color=BRAND, title=f"{I.s('message')} \"Say it\" task", description=(
                "Members must write a message with your **text** (for example `happy birthday`). Optionally they must also "
                "**mention or reply to a person**, and / or do it in one **channel**. I put a ✅ under the message when it counts.\n\n"
                f"**Status:** {'ON' if t.get('enabled') else 'OFF'}\n**Text:** {t.get('phrase') or '*not set*'}\n"
                f"**Person:** {who}\n**Only in:** {where}"))
        elif p == "look":
            e = discord.Embed(color=BRAND, title=f"{I.s('palette')} Look & emojis", description=(
                "Every emoji is editable: type an emoji, `:name:` or just the **emoji ID** - animated emojis work too, no Nitro needed. "
                "Empty = default, `none` = hide it.\n\n"
                f"{self.emoji_line()}\n-# title · prize · winners · host · ends · requirements · entries · done · missing · ended\n\n"
                f"**Colour:** `#{int(c.get('color') or BRAND):06X}` · **Image:** {'set' if c.get('image') else 'none'} · "
                f"**Thumbnail:** {'set' if c.get('thumbnail') else 'none'}"))
        elif p == "buttons":
            b = c["buttons"]
            e = discord.Embed(color=BRAND, title=f"{I.s('tap')} Buttons", description=(
                f"**Join:** {b['join'].get('emoji') or '·'} {b['join'].get('label')} · colour `{b['join'].get('style')}`\n"
                f"**Entries counter:** {b['entries'].get('emoji') or '·'} · {'shown' if b['entries'].get('show', True) else 'hidden'}\n"
                f"**Progress:** {b['progress'].get('emoji') or '·'} {b['progress'].get('label')} · "
                f"{'shown' if b['progress'].get('show', True) else 'hidden'} *(only appears when there are requirements)*\n\n"
                "Emojis can be animated - use the emoji ID."))
        elif p == "load":
            d = self.cog.data(self.guild.id)
            names = "\n".join(f"• **{v['name']}** · {(v['cfg'].get('prize') or '-')[:40]}" for v in d["templates"].values())
            e = discord.Embed(color=BRAND, title=f"{I.s('folder')} Templates", description=(
                "Pick a template to load it - the look, emojis, buttons, prize and requirements come back exactly as saved.\n\n"
                + (names or "*No templates yet. Build a giveaway and press **Save template**.*")))
        else:
            d = self.cog.data(self.guild.id)
            e = discord.Embed(color=BRAND, title=f"{I.s('gift')} Giveaway builder", description=(
                "Set everything with the buttons, check the preview below, then press **Start**."))
            e.add_field(name="Basics", inline=False, value=(
                f"Title: **{c['title']}**\nPrize: **{self.cog.prize_text(self.guild, c) if c.get('prize') else '*not set*'}**\n"
                f"Winners: **{c['winners']}** · Time: **{gl.fmt_dur(c['duration'])}**"))
            e.add_field(name="Post in", value=f"{self.chan(self.channel_id)}\nHost: <@{self.host_id}>", inline=True)
            reqs = self.cog.req_lines(self.guild, c)
            e.add_field(name="Requirements", value=("\n".join(reqs)[:1000] if reqs else "*none - anyone can win*"), inline=True)
            e.set_footer(text=(f"Template: {self.loaded} · " if self.loaded else "") + f"{len(d['templates'])} saved templates")
        if note:
            e.add_field(name="Note", value=note[:1000], inline=False)
        return e

    # ----- components
    def btn(self, label, icon, cb, *, style=discord.ButtonStyle.secondary, row=0, disabled=False):
        b = discord.ui.Button(label=label, emoji=I.e(icon) if icon else None, style=style, row=row, disabled=disabled)
        b.callback = cb
        self.add_item(b)
        return b

    def goto(self, page: str):
        async def cb(interaction: discord.Interaction):
            self.page = page
            await self.push(interaction)
        return cb

    def objs(self, ids, getter) -> list:
        return [o for o in (getter(int(i)) for i in ids or []) if o is not None]

    def render(self):
        self.clear_items()
        c, p, g = self.cfg, self.page, self.guild
        if p == "home":
            self.btn("Basic info", "edit", self.basic_btn, style=discord.ButtonStyle.primary, row=0)
            self.btn("Prize", "gift", self.goto("prize"), row=0)
            self.btn("Requirements", "shield_check", self.goto("req"), row=0)
            self.btn("Look & emojis", "palette", self.goto("look"), row=0)
            self.btn("Buttons", "tap", self.goto("buttons"), row=0)
            ch = g.get_channel(self.channel_id)
            s = discord.ui.ChannelSelect(placeholder="Post the giveaway in...", row=1, min_values=1, max_values=1,
                                         channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                                         default_values=[ch] if ch else [])
            s.callback = lambda i, s=s: self.pick_channel(i, s)
            self.add_item(s)
            self.btn("Save template", "upload", self.save_btn, row=2)
            self.btn("Load template", "folder", self.goto("load"), row=2)
            self.btn("Reset", "refresh", self.reset_btn, row=2)
            self.btn("Start giveaway", "sparkles", self.start_btn, style=discord.ButtonStyle.success, row=3)
            self.btn("Close", "cross", self.close_btn, style=discord.ButtonStyle.danger, row=3)
            host = g.get_member(self.host_id)
            u = discord.ui.UserSelect(placeholder="Hosted by (default: you)...", row=4, min_values=1, max_values=1,
                                      default_values=[host] if host else [])
            u.callback = lambda i, u=u: self.pick_host(i, u)
            self.add_item(u)
        elif p == "prize":
            pr = c["prize_role"]
            self.btn("Points reward", "coins", self.points_btn, row=0)
            self.btn("Role: " + ("Temporary" if pr.get("mode") == "temporary" else "Permanent"), "clock", self.mode_btn, row=0)
            self.btn("Role time", "hourglass", self.time_btn, row=0, disabled=pr.get("mode") != "temporary")
            self.btn("Clear role", "cross", self.clear_role_btn, row=0, disabled=not pr.get("role"))
            self.btn("Back", "left", self.goto("home"), row=0)
            role = g.get_role(int(pr["role"])) if pr.get("role") else None
            s = discord.ui.RoleSelect(placeholder="Role the winners get...", row=1, min_values=0, max_values=1,
                                      default_values=[role] if role else [])
            s.callback = lambda i, s=s: self.pick_role(i, s)
            self.add_item(s)
        elif p == "req":
            self.btn("Messages", "message", self.msgs_btn, row=0)
            self.btn("Invites", "user_add", self.invites_btn, row=0)
            self.btn("Say it task", "messages", self.goto("task"), row=0)
            self.btn("Clear all", "cross", self.clear_req_btn, style=discord.ButtonStyle.danger, row=0)
            self.btn("Back", "left", self.goto("home"), row=0)
            roles = self.objs(self.req["roles"], g.get_role)
            s = discord.ui.RoleSelect(placeholder="Members need these roles to join...", row=1, min_values=0, max_values=10,
                                      default_values=roles)
            s.callback = lambda i, s=s: self.pick_req_roles(i, s)
            self.add_item(s)
            ex = self.objs(self.req["messages"]["exclude"], g.get_channel)[:25]
            s = discord.ui.ChannelSelect(placeholder="Messages here are NOT counted (channels / categories)...", row=2,
                                         min_values=0, max_values=25, channel_types=CHANNEL_TYPES, default_values=ex)
            s.callback = lambda i, s=s: self.pick_exclude(i, s)
            self.add_item(s)
            self.btn("Roles needed: " + ("ALL" if self.req.get("roles_mode") == "all" else "ANY ONE"), "swap", self.roles_mode_btn, row=3)
        elif p == "task":
            t = self.req["task"]
            self.btn("Set the text", "edit", self.task_text_btn, style=discord.ButtonStyle.primary, row=0)
            self.btn("Task: ON" if t.get("enabled") else "Task: OFF", "check" if t.get("enabled") else "cross", self.task_toggle_btn,
                     style=discord.ButtonStyle.success if t.get("enabled") else discord.ButtonStyle.secondary, row=0)
            self.btn("Back", "left", self.goto("req"), row=0)
            tgt = g.get_member(int(t["target"])) if t.get("target") else None
            u = discord.ui.UserSelect(placeholder="Person they have to say it to (optional)...", row=1, min_values=0, max_values=1,
                                      default_values=[tgt] if tgt else [])
            u.callback = lambda i, u=u: self.pick_task_target(i, u)
            self.add_item(u)
            ch = g.get_channel(int(t["channel"])) if t.get("channel") else None
            s = discord.ui.ChannelSelect(placeholder="Only counts in this channel (optional)...", row=2, min_values=0, max_values=1,
                                         channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                                         default_values=[ch] if ch else [])
            s.callback = lambda i, s=s: self.pick_task_channel(i, s)
            self.add_item(s)
        elif p == "look":
            self.btn("Emojis 1/2", "sparkles", self.emojis1_btn, style=discord.ButtonStyle.primary, row=0)
            self.btn("Emojis 2/2", "sparkles", self.emojis2_btn, style=discord.ButtonStyle.primary, row=0)
            self.btn("Colour", "palette", self.color_btn, row=0)
            self.btn("Image", "image", self.image_btn, row=0)
            self.btn("Thumbnail", "image", self.thumb_btn, row=0)
            self.btn("Reset emojis", "refresh", self.reset_emojis_btn, row=1)
            self.btn("Back", "left", self.goto("home"), row=1)
        elif p == "buttons":
            self.btn("Join button", "tap", self.join_btn, style=discord.ButtonStyle.primary, row=0)
            self.btn("Entries button", "users", self.entries_btn, row=0)
            self.btn("Progress button", "target", self.progress_btn, row=0)
            self.btn("Back", "left", self.goto("home"), row=0)
        elif p == "load":
            d = self.cog.data(g.id)
            opts = [discord.SelectOption(label=v["name"][:100], value=k, description=(v["cfg"].get("prize") or "-")[:100], emoji=I.e("gift"))
                    for k, v in list(d["templates"].items())[:25]]
            if d.get("last"):
                opts.insert(0, discord.SelectOption(label="Last giveaway I started", value="__last__", emoji=I.e("clock"),
                                                    description=(d["last"].get("prize") or "-")[:100]))
                opts = opts[:25]
            if opts:
                s = discord.ui.Select(placeholder="Load a template...", row=0, options=opts)
                s.callback = lambda i, s=s: self.load_pick(i, s)
                self.add_item(s)
            if d["templates"]:
                s = discord.ui.Select(placeholder="Delete a template...", row=1, options=[
                    discord.SelectOption(label=v["name"][:100], value=k, emoji=I.e("cross")) for k, v in list(d["templates"].items())[:25]])
                s.callback = lambda i, s=s: self.delete_pick(i, s)
                self.add_item(s)
            self.btn("Back", "left", self.goto("home"), row=2)

    async def push(self, interaction: discord.Interaction, note: str = ""):
        self.note = note or self.note
        self.render()
        await interaction.response.edit_message(embeds=await self.embeds(), view=self)

    async def refresh(self):
        """After a modal / upload: redraw the builder message (the interaction itself was answered already)."""
        self.render()
        if self.message:
            try:
                await self.message.edit(embeds=await self.embeds(), view=self)
            except discord.HTTPException:
                pass

    def modal(self, title, fields, cb) -> GModal:
        return GModal(self, title, fields, cb)

    # ----- home
    async def pick_channel(self, interaction, sel):
        self.channel_id = sel.values[0].id
        await self.push(interaction)

    async def pick_host(self, interaction, sel):
        self.host_id = sel.values[0].id
        await self.push(interaction)

    async def close_btn(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="Giveaway builder closed.", embeds=[], view=None)

    async def reset_btn(self, interaction: discord.Interaction):
        self.cfg, self.loaded = gl.new_config(), None
        await self.push(interaction, "Everything is back to the defaults.")

    async def basic_btn(self, interaction: discord.Interaction):
        c = self.cfg

        async def submitted(i, v):
            title, prize, desc, winners, dur = v
            if not prize:
                raise VCError("Give the giveaway a prize.")
            try:
                w = int(winners or "1")
            except ValueError:
                raise VCError("Winners must be a number.")
            if not 1 <= w <= gl.MAX_WINNERS:
                raise VCError(f"Winners must be 1-{gl.MAX_WINNERS}.")
            secs = gl.parse_duration(dur or "1h")
            if secs is None or not gl.MIN_DURATION <= secs <= gl.MAX_DURATION:
                raise VCError("Time must look like `30m`, `2h`, `1d 12h` or `1w` (10 seconds - 90 days).")
            c.update(title=title or "GIVEAWAY", prize=prize, description=desc, winners=w, duration=secs)
        await interaction.response.send_modal(self.modal("Basic info", [
            dict(label="Title", default=c["title"], max=40, placeholder="GIVEAWAY"),
            dict(label="Prize", default=c["prize"], max=100, required=True, placeholder="Discord Nitro"),
            dict(label="Description (optional)", default=c["description"], max=300, long=True),
            dict(label="Winners", default=str(c["winners"]), max=2, required=True),
            dict(label="Time (30m, 2h, 1d 12h, 1w)", default=gl.fmt_dur(c["duration"]).replace(" ", ""), max=20, required=True)], submitted))

    async def save_btn(self, interaction: discord.Interaction):
        async def submitted(i, v):
            name = v[0]
            if not name:
                raise VCError("Give the template a name.")
            d = self.cog.data(self.guild.id)
            if name.lower() not in d["templates"] and len(d["templates"]) >= gl.MAX_TEMPLATES:
                raise VCError(f"You can keep {gl.MAX_TEMPLATES} templates - delete one first (Load template).")
            t = copy.deepcopy(self.cfg)
            t["channel"] = self.channel_id
            d["templates"][name.lower()] = {"name": name, "saved": time.time(), "cfg": t}
            self.cog.save(self.guild.id)
            self.loaded = name
            self.note = f"Template **{name}** saved. Load it any time with **Load template** (or `{PREFIX}giveaway load {name}`)."
        await interaction.response.send_modal(self.modal("Save template", [
            dict(label="Template name", default=self.loaded or (self.cfg.get("prize") or "")[:30], max=40, required=True)], submitted))

    async def start_btn(self, interaction: discord.Interaction):
        c = self.cfg
        if not (c.get("prize") or "").strip():
            return await self.push(interaction, f"{I.s('warning')} Set a prize first (**Basic info**).")
        channel = self.guild.get_channel(self.channel_id)
        if channel is None:
            return await self.push(interaction, f"{I.s('warning')} Pick a channel for the giveaway.")
        await interaction.response.defer()
        try:
            rec, msg = await self.cog.post_giveaway(self.guild, channel, c, self.host_id)
        except VCError as e:
            self.render()
            return await interaction.edit_original_response(embeds=await self._with_note(f"{I.s('cross')} {e}"), view=self)
        self.stop()
        e = discord.Embed(color=GREEN, title=f"{I.s('check')} Giveaway #{rec['n']} is live",
                          description=f"Posted in {channel.mention} · ends <t:{int(rec['end'])}:R>\n[Jump to the giveaway]({msg.jump_url})")
        e.set_footer(text=f"{PREFIX}giveaway end / reroll / cancel · it is saved as 'Last giveaway' in Load template")
        await interaction.edit_original_response(content=None, embeds=[e], view=None)

    async def _with_note(self, note: str) -> list:
        self.note = note
        return await self.embeds()

    # ----- prize
    async def points_btn(self, interaction: discord.Interaction):
        async def submitted(i, v):
            try:
                n = int(v[0] or "0")
            except ValueError:
                raise VCError("Points must be a number.")
            if not 0 <= n <= 10_000_000:
                raise VCError("Points must be 0 - 10,000,000.")
            self.cfg["points"] = n
        await interaction.response.send_modal(self.modal("Points reward", [
            dict(label="Kon points every winner gets (0 = none)", default=str(self.cfg.get("points") or 0), max=8)], submitted))

    async def mode_btn(self, interaction: discord.Interaction):
        pr = self.cfg["prize_role"]
        pr["mode"] = "permanent" if pr.get("mode") == "temporary" else "temporary"
        await self.push(interaction)

    async def time_btn(self, interaction: discord.Interaction):
        pr = self.cfg["prize_role"]

        async def submitted(i, v):
            secs = gl.parse_duration(v[0])
            if secs is None or secs < 60 or secs > 365 * 86400:
                raise VCError("Use something like `12h`, `7d` or `4w` (1 minute - 1 year).")
            pr["seconds"] = secs
        await interaction.response.send_modal(self.modal("Temporary role time", [
            dict(label="How long do winners keep the role?", default=gl.fmt_dur(pr.get("seconds") or 86400).replace(" ", ""), max=20,
                 required=True, placeholder="7d")], submitted))

    async def clear_role_btn(self, interaction: discord.Interaction):
        self.cfg["prize_role"]["role"] = None
        await self.push(interaction)

    async def pick_role(self, interaction, sel):
        pr = self.cfg["prize_role"]
        old = pr.get("role")
        pr["role"] = sel.values[0].id if sel.values else None
        try:
            self.cog.check_role_reward(self.guild, self.cfg)
        except VCError as e:
            pr["role"] = old
            return await self.push(interaction, f"{I.s('warning')} {e}")
        await self.push(interaction)

    # ----- requirements
    async def msgs_btn(self, interaction: discord.Interaction):
        async def submitted(i, v):
            try:
                n = int(v[0] or "0")
            except ValueError:
                raise VCError("Messages must be a number.")
            if not 0 <= n <= 100000:
                raise VCError("Messages must be 0 - 100,000.")
            self.req["messages"]["count"] = n
        await interaction.response.send_modal(self.modal("Message requirement", [
            dict(label="Messages needed (0 = off)", default=str(self.req["messages"]["count"]), max=6, placeholder="250")], submitted))

    async def invites_btn(self, interaction: discord.Interaction):
        async def submitted(i, v):
            try:
                n = int(v[0] or "0")
            except ValueError:
                raise VCError("Invites must be a number.")
            if not 0 <= n <= 1000:
                raise VCError("Invites must be 0 - 1000.")
            if n and not self.guild.me.guild_permissions.manage_guild:
                raise VCError("I need the **Manage Server** permission to count invites - give it to me first.")
            self.req["invites"] = n
        await interaction.response.send_modal(self.modal("Invite requirement", [
            dict(label="People to invite (0 = off)", default=str(self.req["invites"]), max=4, placeholder="2")], submitted))

    async def clear_req_btn(self, interaction: discord.Interaction):
        self.cfg["req"] = copy.deepcopy(gl.DEFAULT_CFG["req"])
        await self.push(interaction, "All requirements removed.")

    async def roles_mode_btn(self, interaction: discord.Interaction):
        self.req["roles_mode"] = "any" if self.req.get("roles_mode") == "all" else "all"
        await self.push(interaction)

    async def pick_req_roles(self, interaction, sel):
        self.req["roles"] = [r.id for r in sel.values]
        await self.push(interaction)

    async def pick_exclude(self, interaction, sel):
        self.req["messages"]["exclude"] = [c.id for c in sel.values]
        await self.push(interaction)

    async def task_text_btn(self, interaction: discord.Interaction):
        t = self.req["task"]

        async def submitted(i, v):
            if not v[0]:
                raise VCError("Write the text people have to say.")
            t["phrase"], t["enabled"] = v[0], True
        await interaction.response.send_modal(self.modal("\"Say it\" task", [
            dict(label="Text they have to say", default=t.get("phrase"), max=100, required=True, placeholder="happy birthday")], submitted))

    async def task_toggle_btn(self, interaction: discord.Interaction):
        t = self.req["task"]
        if not t.get("enabled") and not t.get("phrase"):
            return await interaction.response.send_modal(GModal(self, "\"Say it\" task", [
                dict(label="Text they have to say", max=100, required=True, placeholder="happy birthday")], self._set_phrase))
        t["enabled"] = not t.get("enabled")
        await self.push(interaction)

    async def _set_phrase(self, i, v):
        if not v[0]:
            raise VCError("Write the text people have to say.")
        self.req["task"]["phrase"], self.req["task"]["enabled"] = v[0], True

    async def pick_task_target(self, interaction, sel):
        self.req["task"]["target"] = sel.values[0].id if sel.values else None
        await self.push(interaction)

    async def pick_task_channel(self, interaction, sel):
        self.req["task"]["channel"] = sel.values[0].id if sel.values else None
        await self.push(interaction)

    # ----- look & emojis
    def emoji_modal(self, keys: list, title: str) -> GModal:
        async def submitted(i, v):
            em = self.cfg.setdefault("emojis", {})
            for k, text in zip(keys, v):
                if not text:
                    em.pop(k, None)
                elif text.lower() in ("none", "off", "hide", "-"):
                    em[k] = ""
                else:
                    em[k] = await self.norm(text)
        return self.modal(title, [dict(label=gl.EMOJI_FIELDS[k][0], default=gl.emoji_of(self.cfg, k) or "none", max=80,
                                       placeholder="emoji, :name: or emoji ID") for k in keys], submitted)

    async def emojis1_btn(self, interaction: discord.Interaction):
        await interaction.response.send_modal(self.emoji_modal(["title", "prize", "winners", "host", "ends"], "Emojis 1/2"))

    async def emojis2_btn(self, interaction: discord.Interaction):
        await interaction.response.send_modal(self.emoji_modal(["req", "entries", "done", "todo", "ended"], "Emojis 2/2"))

    async def reset_emojis_btn(self, interaction: discord.Interaction):
        self.cfg["emojis"] = {}
        await self.push(interaction, "Emojis are back to the defaults.")

    async def color_btn(self, interaction: discord.Interaction):
        async def submitted(i, v):
            t = v[0].lstrip("#").lower()
            if t.startswith("0x"):
                t = t[2:]
            if len(t) != 6 or any(ch not in "0123456789abcdef" for ch in t):
                raise VCError("Colour must be a hex code like `#FFB832`.")
            self.cfg["color"] = int(t, 16)
        await interaction.response.send_modal(self.modal("Embed colour", [
            dict(label="Hex colour", default=f"#{int(self.cfg.get('color') or BRAND):06X}", max=9, required=True)], submitted))

    async def _image(self, interaction: discord.Interaction, key: str, title: str):
        async def done(i: discord.Interaction, item):
            await media.forget(self.cog.bot, self.cfg.get(key))
            self.cfg[key] = item
            await i.followup.send(ok(f"{title} updated." if item else f"{title} removed."), ephemeral=True)
            await self.refresh()
        await interaction.response.send_modal(media.ImageModal(self.cog.bot, self.guild, title, done, label=f"Giveaway {key}",
                                                                allow_clear=True, fallback=interaction.channel))

    async def image_btn(self, interaction: discord.Interaction):
        await self._image(interaction, "image", "Giveaway image")

    async def thumb_btn(self, interaction: discord.Interaction):
        await self._image(interaction, "thumbnail", "Giveaway thumbnail")

    # ----- buttons
    async def join_btn(self, interaction: discord.Interaction):
        j = self.cfg["buttons"]["join"]
        d = gl.DEFAULT_CFG["buttons"]["join"]

        async def submitted(i, v):
            label, emoji, style, ended = v
            st = gl.BUTTON_STYLES.get(style.lower() or "blurple")
            if st is None:
                raise VCError("Colour must be `blurple`, `green`, `grey` or `red`.")
            j.update(label=label or d["label"], emoji=await self.norm(emoji, d["emoji"]), style=st, ended_label=ended or d["ended_label"])
        rev = {"primary": "blurple", "success": "green", "secondary": "grey", "danger": "red"}
        await interaction.response.send_modal(self.modal("Join button", [
            dict(label="Button text", default=j.get("label"), max=80),
            dict(label="Button emoji (emoji / :name: / ID / none)", default=j.get("emoji") or "none", max=80),
            dict(label="Colour (blurple / green / grey / red)", default=rev.get(j.get("style"), "blurple"), max=10),
            dict(label="Text after the giveaway ended", default=j.get("ended_label"), max=80)], submitted))

    async def entries_btn(self, interaction: discord.Interaction):
        b = self.cfg["buttons"]["entries"]
        d = gl.DEFAULT_CFG["buttons"]["entries"]

        async def submitted(i, v):
            b.update(emoji=await self.norm(v[0], d["emoji"]), show=v[1].lower() not in ("no", "n", "off", "hide", "false", "0"))
        await interaction.response.send_modal(self.modal("Entries button", [
            dict(label="Emoji (emoji / :name: / ID / none)", default=b.get("emoji") or "none", max=80),
            dict(label="Show the entries counter? (yes / no)", default="yes" if b.get("show", True) else "no", max=3)], submitted))

    async def progress_btn(self, interaction: discord.Interaction):
        b = self.cfg["buttons"]["progress"]
        d = gl.DEFAULT_CFG["buttons"]["progress"]

        async def submitted(i, v):
            b.update(label=v[0] or d["label"], emoji=await self.norm(v[1], d["emoji"]),
                     show=v[2].lower() not in ("no", "n", "off", "hide", "false", "0"))
        await interaction.response.send_modal(self.modal("Progress button", [
            dict(label="Button text", default=b.get("label"), max=80),
            dict(label="Emoji (emoji / :name: / ID / none)", default=b.get("emoji") or "none", max=80),
            dict(label="Show it? (yes / no)", default="yes" if b.get("show", True) else "no", max=3)], submitted))

    # ----- templates
    async def load_pick(self, interaction: discord.Interaction, sel):
        d = self.cog.data(self.guild.id)
        key = sel.values[0]
        src = d.get("last") if key == "__last__" else (d["templates"].get(key) or {}).get("cfg")
        if not src:
            return await self.push(interaction, f"{I.s('warning')} That template is gone.")
        self.cfg = gl.full_config(src)
        self.loaded = None if key == "__last__" else d["templates"][key]["name"]
        ch = self.cfg.get("channel")
        if ch and self.guild.get_channel(ch):
            self.channel_id = ch
        self.page = "home"
        await self.push(interaction, f"Loaded **{self.loaded or 'your last giveaway'}**. Change anything you like, then press **Start**.")

    async def delete_pick(self, interaction: discord.Interaction, sel):
        d = self.cog.data(self.guild.id)
        gone = d["templates"].pop(sel.values[0], None)
        self.cog.save(self.guild.id)
        if gone and self.loaded and self.loaded.lower() == gone["name"].lower():
            self.loaded = None
        await self.push(interaction, f"Template **{(gone or {}).get('name', '')}** deleted.")


async def setup(bot):
    await bot.add_cog(Giveaway(bot))
