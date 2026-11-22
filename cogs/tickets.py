"""Tickets - a Components V2 panel with a category dropdown, private ticket channels, detailed logs and an HTML transcript.

$ticket                 (admins) the control panel: create / edit categories, panel message, welcome message, banner,
                        log channels, support roles, settings, post the panel
$ticket close [reason]  close the ticket you are in (staff or the opener)
$ticket claim           claim / unclaim the ticket you are in (staff)
$ticket add @member     let someone into this ticket (staff)
$ticket remove @member  take someone out of this ticket (staff)
$ticket rename <name>   rename this ticket channel (staff)
$ticket blacklist [@m]  (admins) block / unblock someone from opening tickets, no member = list

Everything is saved in kon.db (settings table key "tickets"), so it is part of every backup.
Every button / dropdown has a fixed custom_id ("kon_tk:...") and is handled in on_interaction, so the panel and the
ticket buttons keep working after a restart without re-posting anything.
"""
import asyncio
import copy
import datetime as dt
import io
import logging
import re
import time
import uuid
import zipfile
from typing import Optional

import discord
from discord.ext import commands

from core import media
from core.config import BLURPLE, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, err, ok
from core.transcript import build_html, esc, fmt_dt
from core.ui import OwnedView, clean_emoji, to_partial

log = logging.getLogger("kon.tickets")

KEY = "tickets"
CID = "kon_tk:"                      # custom_id prefix of every ticket component
GREEN = 0x57F287
RED = 0xED4245
GOLD = 0xF5C542
NONE = discord.AllowedMentions.none()
PEOPLE = discord.AllowedMentions(users=True, roles=True, everyone=False)
MAX_CATEGORIES = 25
MAX_HISTORY = 10000                  # messages in one transcript
CLOSE_DELAY = 5

DEFAULTS = {
    "panel_channel": None, "panel_message": None,
    "panel": {"title": "Support Center", "color": BRAND, "placeholder": "Choose a category...", "banner": None,
              "description": ("Need a hand? Pick the category that fits your problem from the menu below and a private "
                              "channel opens just for you and our staff.\n\n"
                              "• One ticket per problem - keep it clear and add screenshots if they help\n"
                              "• Be patient, staff replies as soon as they can\n"
                              "• Tickets opened for fun get you blacklisted")},
    "welcome": {"title": "Welcome to your ticket",
                "description": ("Hey {user}, thanks for reaching out about **{category}**!\n"
                                "Tell us what is going on in as much detail as you can - a staff member will be with you shortly.")},
    "support_roles": [], "admin_roles": [], "open_category": None,
    "logs": {"opened": None, "closed": None, "transcript": None},
    "categories": [], "max_open": 1, "name_format": "ticket-{user}", "opener_can_close": True,
    "counter": 0, "blacklist": [], "tickets": {}, "stats": {"opened": 0, "closed": 0},
}


def fmt_dur(seconds: float) -> str:
    s = max(0, int(seconds))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{sec}s" if (sec or not (d or h or m)) else ""]
    return " ".join(p for p in parts if p)


def stamp(ts: float, style: str = "F") -> str:
    return f"<t:{int(ts)}:{style}>"


def parse_color(text: str) -> Optional[int]:
    t = (text or "").strip().lower().lstrip("#")
    if t.startswith("0x"):
        t = t[2:]
    if not t:
        return None
    if not re.fullmatch(r"[0-9a-f]{6}", t):
        raise VCError("Colour must be a hex code like `#FFB832`.")
    return int(t, 16)


def safe_name(text: str) -> str:
    t = re.sub(r"[^a-z0-9_\-]", "", (text or "").lower().replace(" ", "-"))
    return re.sub(r"-{2,}", "-", t).strip("-_")


# ------------------------------------------------------------------ small ui helpers
class Noop(discord.ui.Button):
    """A button that only exists to be drawn - the real work is done in Tickets.on_interaction (custom_id)."""

    async def callback(self, interaction: discord.Interaction):
        return


class NoopSelect(discord.ui.Select):
    async def callback(self, interaction: discord.Interaction):
        return


async def say(interaction: discord.Interaction, text: str, **kw):
    kw.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(text, **kw)
    else:
        await interaction.response.send_message(text, **kw)


class TKModal(discord.ui.Modal):
    """fields: list of dicts(label, default, max, required, long, placeholder)."""

    def __init__(self, title: str, fields: list, on_submit):
        super().__init__(title=title[:45], timeout=300)
        self._cb = on_submit
        self._inputs = []
        for f in fields:
            mx = f.get("max", 100)
            ti = discord.ui.TextInput(
                label=f["label"][:45], default=((f.get("default") or "")[:mx] or None), max_length=mx,
                required=f.get("required", False), placeholder=(f.get("placeholder") or None),
                style=discord.TextStyle.paragraph if f.get("long") else discord.TextStyle.short)
            self._inputs.append(ti)
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self._cb(interaction, [i.value.strip() for i in self._inputs])
        except VCError as e:
            await say(interaction, err(str(e)))

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("ticket modal failed", exc_info=error)
        await say(interaction, err("That didn't work - check the values and try again."))


# ------------------------------------------------------------------ the cog
class Tickets(commands.Cog):
    """Tickets with a V2 panel, logs and transcripts."""

    def __init__(self, bot):
        self.bot = bot
        self._cfg: dict[int, dict] = {}

    # ------------------------------------------------------------ storage
    def cfg(self, gid: int) -> dict:
        c = self._cfg.get(gid)
        if c is None:
            stored = self.bot.db.get_settings(gid, KEY)
            c = copy.deepcopy(DEFAULTS)
            for k, v in stored.items():
                if isinstance(c.get(k), dict) and isinstance(v, dict):
                    c[k].update(v)
                else:
                    c[k] = v
            self._cfg[gid] = c
        return c

    def save(self, gid: int):
        self.bot.db.set_settings(gid, KEY, self.cfg(gid))

    # ------------------------------------------------------------ who is who
    def is_admin(self, member: discord.Member) -> bool:
        p = member.guild_permissions
        if p.administrator or p.manage_guild or self.bot.is_owner_fast(member.id, member.guild.id):
            return True
        ids = set(self.cfg(member.guild.id)["admin_roles"])
        return any(r.id in ids for r in member.roles)

    def is_staff(self, member) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if self.is_admin(member):
            return True
        ids = set(self.cfg(member.guild.id)["support_roles"])
        return any(r.id in ids for r in member.roles)

    def category(self, cfg: dict, cid: str) -> Optional[dict]:
        return next((c for c in cfg["categories"] if c["id"] == cid), None)

    def ticket_of(self, channel_id: int, gid: int) -> Optional[dict]:
        return self.cfg(gid)["tickets"].get(str(channel_id))

    @staticmethod
    def who(guild: discord.Guild, uid: Optional[int]) -> str:
        return f"<@{uid}>" if uid else "Nobody"

    # ------------------------------------------------------------ the V2 panel
    def build_panel(self, cfg: dict, banner_url: Optional[str] = None) -> discord.ui.LayoutView:
        p = cfg["panel"]
        items = []
        if banner_url:
            items += [discord.ui.MediaGallery(discord.MediaGalleryItem(banner_url)), discord.ui.Separator()]
        items.append(discord.ui.TextDisplay(f"## {I.s('ticket')} {p['title']}\n{p['description']}"[:3900]))
        items += [discord.ui.Separator(), discord.ui.TextDisplay("-# Pick a category from the menu below to open a ticket.")]
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*items, accent_colour=discord.Colour(p.get("color") or BRAND)))
        options = [discord.SelectOption(label=c["name"][:100], value=c["id"], description=(c.get("description") or None),
                                        emoji=to_partial(c.get("emoji"))) for c in cfg["categories"][:MAX_CATEGORIES]]
        row = discord.ui.ActionRow()
        row.add_item(NoopSelect(custom_id=CID + "panel", placeholder=(p.get("placeholder") or "Choose a category...")[:150],
                                options=options))
        view.add_item(row)
        return view

    async def publish_panel(self, guild: discord.Guild, channel: Optional[discord.TextChannel] = None) -> discord.Message:
        cfg = self.cfg(guild.id)
        if not cfg["categories"]:
            raise VCError("Create at least one category first.")
        channel = channel or (guild.get_channel(cfg["panel_channel"]) if cfg["panel_channel"] else None)
        if not isinstance(channel, discord.TextChannel):
            raise VCError("Pick a channel for the panel first.")
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            raise VCError(f"I need **View Channel and Send Messages** in {channel.mention}.")
        url = await media.fresh(self.bot, cfg["panel"].get("banner"))
        view = self.build_panel(cfg, url)
        msg = None
        if cfg["panel_message"] and cfg["panel_channel"] == channel.id:
            try:
                msg = await channel.get_partial_message(cfg["panel_message"]).edit(view=view, attachments=[])
            except discord.NotFound:
                msg = None
            except discord.HTTPException as e:
                raise VCError(f"Discord said no: {e.text}")
        if msg is None:
            old_ch = guild.get_channel(cfg["panel_channel"]) if cfg["panel_channel"] else None
            if old_ch is not None and cfg["panel_message"]:
                try:
                    await old_ch.get_partial_message(cfg["panel_message"]).delete()
                except discord.HTTPException:
                    pass
            try:
                msg = await channel.send(view=view, allowed_mentions=NONE)
            except discord.HTTPException as e:
                raise VCError(f"Couldn't post the panel: {e.text}")
        cfg["panel_channel"], cfg["panel_message"] = channel.id, msg.id
        cfg["panel"]["_url"] = url                      # remembered so a reset (below) can redraw the same banner
        self.save(guild.id)
        return msg

    async def refresh_panel(self, guild: discord.Guild):
        """Redraw the posted panel after an edit (quietly - nothing happens when no panel is posted)."""
        cfg = self.cfg(guild.id)
        if not (cfg["panel_message"] and cfg["panel_channel"]) or not cfg["categories"]:
            return
        try:
            await self.publish_panel(guild)
        except (VCError, discord.HTTPException):
            pass

    async def reset_panel(self, guild: discord.Guild):
        """Puts the dropdown back to its placeholder after someone picked a category."""
        cfg = self.cfg(guild.id)
        ch = guild.get_channel(cfg["panel_channel"]) if cfg["panel_channel"] else None
        if ch is None or not cfg["panel_message"] or not cfg["categories"]:
            return
        try:
            await ch.get_partial_message(cfg["panel_message"]).edit(
                view=self.build_panel(cfg, cfg["panel"].get("_url")), attachments=[])
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------ ticket message pieces
    def ticket_buttons(self, claimed: bool = False) -> discord.ui.View:
        v = discord.ui.View(timeout=None)
        v.add_item(Noop(label="Close", emoji=I.e("lock"), style=discord.ButtonStyle.danger, custom_id=CID + "close"))
        v.add_item(Noop(label="Close With Reason", emoji=I.e("edit"), style=discord.ButtonStyle.danger,
                        custom_id=CID + "close_reason"))
        v.add_item(Noop(label="Unclaim" if claimed else "Claim", emoji=I.e("claim"),
                        style=discord.ButtonStyle.secondary if claimed else discord.ButtonStyle.success,
                        custom_id=CID + "claim"))
        return v

    def first_reply_text(self, t: dict) -> str:
        if t.get("first_reply_at"):
            return f"<@{t['first_reply_by']}> · after **{fmt_dur(t['first_reply_at'] - t['created'])}**"
        return "*Waiting for staff...*"

    def opened_embed(self, guild: discord.Guild, t: dict, *, closed: bool = False) -> discord.Embed:
        ch = guild.get_channel(t["channel"])
        e = discord.Embed(color=RED if closed else GREEN,
                          title=f"{I.s('ticket')} Ticket #{t['num']:04d} · {'closed' if closed else 'opened'}")
        e.add_field(name="Ticket", value=(ch.mention if ch else f"`{t.get('name', 'ticket')}`") + f"\n`#{t['num']:04d}`", inline=True)
        e.add_field(name="Category", value=t["category_name"], inline=True)
        e.add_field(name="Opened by", value=f"<@{t['opener']}>\n`{t['opener']}`", inline=True)
        e.add_field(name="Created", value=f"{stamp(t['created'])}\n{stamp(t['created'], 'R')}", inline=True)
        status = "Closed" if closed else (f"Claimed by <@{t['claimed_by']}>" if t.get("claimed_by") else "Open · unclaimed")
        e.add_field(name="Status", value=status, inline=True)
        e.add_field(name="First staff reply", value=self.first_reply_text(t), inline=True)
        if t.get("answer"):
            e.add_field(name=(t.get("question") or "Form answer")[:250], value=t["answer"][:1000], inline=False)
        e.set_thumbnail(url=(guild.get_member(t["opener"]).display_avatar.url if guild.get_member(t["opener"]) else None))
        return e

    async def update_open_log(self, guild: discord.Guild, t: dict, *, closed: bool = False):
        ref = t.get("log")
        if not ref:
            return
        ch = guild.get_channel(ref[0])
        if ch is None:
            return
        try:
            await ch.get_partial_message(ref[1]).edit(embed=self.opened_embed(guild, t, closed=closed))
        except discord.HTTPException:
            pass

    def log_channel(self, guild: discord.Guild, which: str) -> Optional[discord.TextChannel]:
        cid = self.cfg(guild.id)["logs"].get(which)
        ch = guild.get_channel(cid) if cid else None
        return ch if isinstance(ch, discord.TextChannel) else None

    # ------------------------------------------------------------ create
    def channel_name(self, cfg: dict, member: discord.Member, num: int, cat: dict) -> str:
        user = safe_name(member.name)
        fmt = cfg.get("name_format") or "ticket-{user}"
        name = (fmt.replace("{user}", user or f"{num:04d}").replace("{number}", f"{num:04d}")
                .replace("{category}", safe_name(cat["name"]) or "ticket"))
        return (safe_name(name) or f"ticket-{num:04d}")[:90]

    def overwrites(self, guild: discord.Guild, cfg: dict, member: discord.Member, cat: dict) -> dict:
        mp = guild.me.guild_permissions

        def po(**perms):                                 # only grant what the bot itself has (Discord refuses the rest)
            return discord.PermissionOverwrite(**{k: v for k, v in perms.items() if mp.administrator or getattr(mp, k, False)})
        talk = dict(view_channel=True, send_messages=True, read_message_history=True, attach_files=True,
                    embed_links=True, add_reactions=True)
        ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
              guild.me: po(**talk), member: po(**talk)}
        for rid in set(cfg["support_roles"]) | set(cfg["admin_roles"]) | set(cat.get("ping_roles", [])):
            role = guild.get_role(rid)
            if role is not None:
                ow[role] = po(**talk)
        return ow

    async def create_ticket(self, guild: discord.Guild, member: discord.Member, cat: dict, answer: Optional[str]) -> discord.TextChannel:
        cfg = self.cfg(guild.id)
        if member.id in cfg["blacklist"]:
            raise VCError("You can't open tickets here.")
        mine = [t for t in cfg["tickets"].values() if t["opener"] == member.id]
        if len(mine) >= max(1, cfg["max_open"]):
            ch = guild.get_channel(mine[0]["channel"])
            raise VCError(f"You already have {'an open ticket' if len(mine) == 1 else 'open tickets'}: "
                          f"{ch.mention if ch else '(channel missing)'}. Close it before opening a new one.")
        num = cfg["counter"] + 1
        parent = guild.get_channel(cat.get("parent") or cfg.get("open_category") or 0)
        if not isinstance(parent, discord.CategoryChannel):
            parent = None
        name = self.channel_name(cfg, member, num, cat)
        try:
            ch = await guild.create_text_channel(
                name, category=parent, overwrites=self.overwrites(guild, cfg, member, cat),
                topic=f"Ticket #{num:04d} · {cat['name']} · opened by {member} ({member.id})",
                reason=f"Ticket #{num:04d} opened by {member}")
        except discord.Forbidden:
            raise VCError("I need the **Manage Channels** permission to open tickets.")
        except discord.HTTPException as e:
            raise VCError(f"Discord couldn't create the channel: {e.text}")
        cfg["counter"] = num
        cfg["stats"]["opened"] += 1
        t = {"num": num, "channel": ch.id, "name": ch.name, "category": cat["id"], "category_name": cat["name"],
             "question": cat.get("question"), "opener": member.id, "created": time.time(), "claimed_by": None,
             "claimed_at": None, "first_reply_by": None, "first_reply_at": None, "answer": answer, "log": None,
             "closing": False}
        cfg["tickets"][str(ch.id)] = t
        self.save(guild.id)

        # the welcome message in the ticket
        w = cfg["welcome"]

        def fill(s):
            return (s.replace("{user}", member.mention).replace("{category}", cat["name"])
                    .replace("{ticket}", f"#{num:04d}").replace("{server}", guild.name))
        embeds = [discord.Embed(color=BRAND, title=fill(w["title"])[:256], description=fill(w["description"])[:4000])]
        embeds[0].add_field(name="Category", value=cat["name"], inline=True)
        embeds[0].add_field(name="Ticket", value=f"`#{num:04d}`", inline=True)
        embeds[0].add_field(name="Opened", value=stamp(t["created"], "R"), inline=True)
        if answer:
            embeds.append(discord.Embed(color=BLURPLE, title=(cat.get("question") or cat["name"])[:256], description=answer[:4000]))
        roles = [rid for rid in dict.fromkeys(cat.get("ping_roles", []) + cfg["support_roles"]) if guild.get_role(rid)]
        ping = member.mention + "".join(f" <@&{r}>" for r in roles)
        try:
            msg = await ch.send(content=ping, embeds=embeds, view=self.ticket_buttons(), allowed_mentions=PEOPLE)
            t["welcome_msg"] = msg.id
        except discord.HTTPException:
            log.warning("could not post the welcome message in %s", ch)

        # the "opened" log
        lg = self.log_channel(guild, "opened")
        if lg is not None:
            try:
                v = discord.ui.View()
                v.add_item(discord.ui.Button(label="Open ticket", url=ch.jump_url, emoji=I.e("ticket")))
                m = await lg.send(embed=self.opened_embed(guild, t), view=v, allowed_mentions=NONE)
                t["log"] = [lg.id, m.id]
            except discord.HTTPException:
                log.warning("could not write the opened log in %s", lg)
        self.save(guild.id)
        return ch

    # ------------------------------------------------------------ claim
    async def toggle_claim(self, channel: discord.TextChannel, user: discord.Member) -> bool:
        """True = claimed, False = released."""
        cfg = self.cfg(channel.guild.id)
        t = cfg["tickets"].get(str(channel.id))
        if t is None:
            raise VCError("This isn't a ticket channel.")
        if not self.is_staff(user):
            raise VCError("Only staff can claim tickets.")
        cur = t.get("claimed_by")
        if cur and cur != user.id and not self.is_admin(user):
            raise VCError(f"<@{cur}> already claimed this ticket.")
        claimed = cur != user.id
        t["claimed_by"], t["claimed_at"] = (user.id, time.time()) if claimed else (None, None)
        self.save(channel.guild.id)
        text = (f"{I.s('claim')} {user.mention} claimed this ticket and will help you from here." if claimed
                else f"{I.s('refresh')} {user.mention} released this ticket - another staff member can take it.")
        try:
            await channel.send(embed=discord.Embed(color=GREEN if claimed else GOLD, description=text), allowed_mentions=NONE)
        except discord.HTTPException:
            pass
        await self.update_open_log(channel.guild, t)
        return claimed

    # ------------------------------------------------------------ close
    def compute_stats(self, guild: discord.Guild, t: dict, msgs: list) -> dict:
        humans = [m for m in msgs if not m.author.bot]
        opener = [m for m in humans if m.author.id == t["opener"]]
        staff = [m for m in humans if m.author.id != t["opener"] and self.is_staff(guild.get_member(m.author.id))]
        first = t.get("first_reply_at"), t.get("first_reply_by")
        if not first[0] and staff:
            first = staff[0].created_at.timestamp(), staff[0].author.id
        people = list(dict.fromkeys(m.author.id for m in humans))
        return {"total": len(msgs), "opener": len(opener), "staff": len(staff), "first": first, "people": people}

    def detail_fields(self, t: dict, closed_by: int, closed_at: float, reason: Optional[str], s: dict) -> list:
        first_at, first_by = s["first"]
        first = (f"<@{first_by}> · after **{fmt_dur(first_at - t['created'])}**" if first_at else "No staff reply")
        return [
            ("Ticket", f"`#{t['num']:04d}` · `{t.get('name', 'ticket')}`", True),
            ("Category", t["category_name"], True),
            ("Opened by", f"<@{t['opener']}>\n`{t['opener']}`", True),
            ("Closed by", f"<@{closed_by}>\n`{closed_by}`", True),
            ("Claimed by", self.who(None, t.get("claimed_by")), True),
            ("Duration", f"**{fmt_dur(closed_at - t['created'])}**", True),
            ("Created", f"{stamp(t['created'])}\n{stamp(t['created'], 'R')}", True),
            ("Closed", f"{stamp(closed_at)}\n{stamp(closed_at, 'R')}", True),
            ("First staff reply", first, True),
            ("Messages", f"**{s['total']}** total · {s['opener']} from the opener · {s['staff']} from staff", False),
            ("Participants", " ".join(f"<@{u}>" for u in s["people"][:15]) or "-", False),
            ("Reason", (reason or "No reason provided")[:1000], False),
        ]

    async def close_ticket(self, channel: discord.TextChannel, closer: discord.Member, reason: Optional[str] = None):
        guild = channel.guild
        cfg = self.cfg(guild.id)
        t = cfg["tickets"].get(str(channel.id))
        if t is None:
            raise VCError("This isn't a ticket channel.")
        if t.get("closing"):
            raise VCError("This ticket is already being closed.")
        t["closing"] = True
        reason = (reason or "").strip() or None
        started = time.time()
        try:
            await channel.send(embed=discord.Embed(
                color=RED, title=f"{I.s('lock')} Closing this ticket",
                description=f"{closer.mention} closed it. Saving the transcript - this channel is deleted in **{CLOSE_DELAY} seconds**."
                            + (f"\n**Reason:** {reason[:500]}" if reason else "")), allowed_mentions=NONE)
        except discord.HTTPException:
            pass

        try:
            msgs = [m async for m in channel.history(limit=MAX_HISTORY, oldest_first=True)]
            s = self.compute_stats(guild, t, msgs)
            closed_at = time.time()
            first_at, first_by = s["first"]
            name_of = lambda uid: (f"{guild.get_member(uid).display_name} ({uid})" if guild.get_member(uid) else f"Unknown user ({uid})") if uid else "Nobody"
            info = [
                ("Ticket", f"#{t['num']:04d} · {esc(channel.name)}"), ("Category", esc(t["category_name"])),
                ("Opened by", esc(name_of(t["opener"]))), ("Claimed by", esc(name_of(t.get("claimed_by")))),
                ("Created", esc(fmt_dt(dt.datetime.fromtimestamp(t["created"], dt.timezone.utc)))),
                ("First staff reply", (f"{esc(name_of(first_by))} · after {fmt_dur(first_at - t['created'])}" if first_at else "No staff reply")),
                ("Closed by", esc(name_of(closer.id))),
                ("Closed", esc(fmt_dt(dt.datetime.fromtimestamp(closed_at, dt.timezone.utc)))),
                ("Duration", fmt_dur(closed_at - t["created"])),
                ("Messages", f"{s['total']} ({s['opener']} opener · {s['staff']} staff)"),
                ("Reason", esc(reason or "No reason provided")),
            ]
            page = await asyncio.to_thread(build_html, guild, channel, msgs, info,
                                           title=f"Ticket #{t['num']:04d} - {t['category_name']}", is_staff=self.is_staff)
            data, fname = page.encode("utf-8"), f"transcript-{channel.name}.html"
            if len(data) > max(guild.filesize_limit - 65536, 1024 * 1024):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr(fname, data)
                data, fname = buf.getvalue(), fname + ".zip"
        except Exception:
            log.exception("transcript for %s failed", channel)
            t["closing"] = False
            raise VCError("I couldn't build the transcript, so the ticket stays open. Try closing it again in a moment.")

        await asyncio.sleep(max(0.0, CLOSE_DELAY - (time.time() - started)))
        fields = self.detail_fields(t, closer.id, closed_at, reason, s)

        def embed(title: str, color: int, fs) -> discord.Embed:
            e = discord.Embed(color=color, title=title)
            for n, v, inline in fs:
                e.add_field(name=n, value=v, inline=inline)
            return e

        def file():
            return discord.File(io.BytesIO(data), filename=fname)

        closed_ch, trans_ch = self.log_channel(guild, "closed"), self.log_channel(guild, "transcript")
        if closed_ch is not None:
            try:
                await closed_ch.send(embed=embed(f"{I.s('lock')} Ticket #{t['num']:04d} · closed", RED, fields),
                                     file=file() if trans_ch is None else discord.utils.MISSING, allowed_mentions=NONE)
            except discord.HTTPException:
                log.warning("closed log failed in %s", closed_ch)
        if trans_ch is not None:
            try:
                await trans_ch.send(embed=embed(f"{I.s('scroll')} Ticket #{t['num']:04d} · transcript", BLURPLE, fields),
                                    file=file(), allowed_mentions=NONE)
            except discord.HTTPException:
                log.warning("transcript log failed in %s", trans_ch)
        opener = guild.get_member(t["opener"])
        if opener is not None:
            try:
                dm = embed(f"{I.s('ticket')} Your ticket was closed", RED,
                           [f for f in fields if f[0] not in ("Participants", "Opened by", "Closed")])
                dm.description = f"Your ticket in **{guild.name}** has been closed. The transcript is attached."
                await opener.send(embed=dm, file=file())
            except discord.HTTPException:
                pass                                          # DMs closed
        await self.update_open_log(guild, t, closed=True)
        cfg["tickets"].pop(str(channel.id), None)
        cfg["stats"]["closed"] += 1
        self.save(guild.id)
        try:
            await channel.delete(reason=f"Ticket #{t['num']:04d} closed by {closer}")
        except discord.HTTPException:
            try:
                await channel.send(err("The ticket is saved, but I couldn't delete this channel - please delete it manually."))
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------ interactions (panel + ticket buttons)
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type is not discord.InteractionType.component or interaction.guild is None:
            return
        cid = (interaction.data or {}).get("custom_id") or ""
        if not cid.startswith(CID):
            return
        action = cid[len(CID):]
        try:
            if action == "panel":
                await self.on_panel_pick(interaction)
            elif action in ("close", "close_reason"):
                await self.on_close_button(interaction, ask_reason=action == "close_reason")
            elif action == "claim":
                await self.on_claim_button(interaction)
        except VCError as e:
            await say(interaction, err(str(e)))
        except discord.HTTPException as e:
            log.warning("ticket interaction failed: %s", e)
            await say(interaction, err("Discord refused that. Check my permissions and try again."))
        except Exception:
            log.exception("ticket interaction %s crashed", action)
            await say(interaction, err("That didn't work. Try again in a moment."))

    async def on_panel_pick(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        cfg = self.cfg(guild.id)
        values = (interaction.data or {}).get("values") or []
        cat = self.category(cfg, values[0]) if values else None
        asyncio.create_task(self.reset_panel(guild))                 # the dropdown goes back to its placeholder
        if cat is None:
            raise VCError("That category doesn't exist any more - the panel will refresh.")
        if member.id in cfg["blacklist"]:
            raise VCError("You can't open tickets here.")
        mine = [t for t in cfg["tickets"].values() if t["opener"] == member.id]
        if len(mine) >= max(1, cfg["max_open"]):
            ch = guild.get_channel(mine[0]["channel"])
            raise VCError(f"You already have {'an open ticket' if len(mine) == 1 else 'open tickets'}: "
                          f"{ch.mention if ch else '(channel missing)'}. Close it before opening a new one.")
        if cat.get("question"):
            async def submitted(i: discord.Interaction, vals):
                await i.response.defer(ephemeral=True)
                ch = await self.create_ticket(guild, member, cat, vals[0])
                await i.followup.send(embed=self.created_embed(ch), ephemeral=True)
            modal = TKModal(cat["name"], [dict(label=cat["name"], max=1000, required=True, long=True,
                                                placeholder=cat["question"][:100])], submitted)
            return await interaction.response.send_modal(modal)
        await interaction.response.defer(ephemeral=True)
        ch = await self.create_ticket(guild, member, cat, None)
        await interaction.followup.send(embed=self.created_embed(ch), ephemeral=True)

    @staticmethod
    def created_embed(ch: discord.TextChannel) -> discord.Embed:
        return discord.Embed(color=GREEN, title=f"{I.s('ticket')} Ticket opened", description=f"Your ticket is ready: {ch.mention}")

    async def on_close_button(self, interaction: discord.Interaction, ask_reason: bool):
        t = self.ticket_of(interaction.channel_id, interaction.guild.id)
        if t is None:
            raise VCError("This isn't an open ticket.")
        cfg = self.cfg(interaction.guild.id)
        user = interaction.user
        if not (self.is_staff(user) or (user.id == t["opener"] and cfg["opener_can_close"])):
            raise VCError("Only staff can close this ticket.")
        if ask_reason:
            async def submitted(i: discord.Interaction, vals):
                await i.response.defer()
                await self.close_ticket(i.channel, i.user, vals[0])
            return await interaction.response.send_modal(TKModal(
                "Close ticket", [dict(label="Reason", max=500, long=True, required=False,
                                      placeholder="Why is this ticket being closed?")], submitted))
        await interaction.response.defer()
        await self.close_ticket(interaction.channel, user, None)

    async def on_claim_button(self, interaction: discord.Interaction):
        claimed = await self.toggle_claim(interaction.channel, interaction.user)
        try:
            await interaction.response.edit_message(view=self.ticket_buttons(claimed))
        except discord.HTTPException:
            if not interaction.response.is_done():
                await interaction.response.defer()

    # ------------------------------------------------------------ listeners
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        t = self.cfg(message.guild.id)["tickets"].get(str(message.channel.id))
        if t is None or t.get("first_reply_at") or message.author.id == t["opener"]:
            return
        if self.is_staff(message.author):
            t["first_reply_at"], t["first_reply_by"] = message.created_at.timestamp(), message.author.id
            self.save(message.guild.id)
            await self.update_open_log(message.guild, t)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        cfg = self.cfg(channel.guild.id)
        if cfg["tickets"].pop(str(channel.id), None) is not None:        # deleted by hand: no transcript possible
            self.save(channel.guild.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = self.cfg(member.guild.id)
        for t in list(cfg["tickets"].values()):
            if t["opener"] == member.id and not t.get("closing"):
                ch = member.guild.get_channel(t["channel"])
                if ch is not None:
                    try:
                        await ch.send(embed=discord.Embed(color=GOLD, description=f"{I.s('warning')} **{member}** left the server."))
                    except discord.HTTPException:
                        pass

    # ------------------------------------------------------------ commands
    @commands.group(name="ticket", aliases=["tickets", "tk"], invoke_without_command=True)
    @commands.guild_only()
    async def ticket(self, ctx: commands.Context):
        """$ticket - the ticket control panel (admins)."""
        if not self.is_admin(ctx.author):
            raise commands.MissingPermissions(["manage_guild"])
        view = TicketAdmin(self, ctx.author)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False)

    def _need_ticket(self, ctx: commands.Context) -> dict:
        t = self.ticket_of(ctx.channel.id, ctx.guild.id)
        if t is None:
            raise VCError("Use this inside a ticket channel.")
        return t

    @ticket.command(name="close")
    @commands.guild_only()
    async def ticket_close(self, ctx: commands.Context, *, reason: str = ""):
        """$ticket close [reason] - close this ticket (staff or the opener)."""
        t = self._need_ticket(ctx)
        if not (self.is_staff(ctx.author) or (ctx.author.id == t["opener"] and self.cfg(ctx.guild.id)["opener_can_close"])):
            raise VCError("Only staff can close this ticket.")
        await self.close_ticket(ctx.channel, ctx.author, reason)

    @ticket.command(name="claim")
    @commands.guild_only()
    async def ticket_claim(self, ctx: commands.Context):
        """$ticket claim - claim / release this ticket (staff)."""
        self._need_ticket(ctx)
        await self.toggle_claim(ctx.channel, ctx.author)

    @ticket.command(name="add")
    @commands.guild_only()
    async def ticket_add(self, ctx: commands.Context, member: discord.Member):
        """$ticket add @member - let someone into this ticket (staff)."""
        self._need_ticket(ctx)
        if not self.is_staff(ctx.author):
            raise VCError("Only staff can add people.")
        await ctx.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True,
                                          attach_files=True, embed_links=True, reason=f"Added to ticket by {ctx.author}")
        await ctx.reply(ok(f"{member.mention} was added to this ticket."), mention_author=False, allowed_mentions=NONE)

    @ticket.command(name="remove")
    @commands.guild_only()
    async def ticket_remove(self, ctx: commands.Context, member: discord.Member):
        """$ticket remove @member - take someone out of this ticket (staff)."""
        t = self._need_ticket(ctx)
        if not self.is_staff(ctx.author):
            raise VCError("Only staff can remove people.")
        if member.id == t["opener"] or self.is_staff(member):
            raise VCError("You can't remove the ticket owner or staff.")
        await ctx.channel.set_permissions(member, overwrite=None, reason=f"Removed from ticket by {ctx.author}")
        await ctx.reply(ok(f"{member.mention} was removed from this ticket."), mention_author=False, allowed_mentions=NONE)

    @ticket.command(name="rename")
    @commands.guild_only()
    async def ticket_rename(self, ctx: commands.Context, *, name: str):
        """$ticket rename <name> - rename this ticket channel (staff)."""
        t = self._need_ticket(ctx)
        if not self.is_staff(ctx.author):
            raise VCError("Only staff can rename tickets.")
        clean = safe_name(name)
        if not clean:
            raise VCError("Use letters, numbers and dashes.")
        await ctx.channel.edit(name=clean[:90], reason=f"Ticket renamed by {ctx.author}")
        t["name"] = clean[:90]
        self.save(ctx.guild.id)
        await ctx.reply(ok(f"Renamed to **{clean[:90]}**."), mention_author=False)

    @ticket.command(name="blacklist", aliases=["ban"])
    @commands.guild_only()
    async def ticket_blacklist(self, ctx: commands.Context, member: Optional[discord.User] = None):
        """$ticket blacklist [@member] - block / unblock a member from opening tickets (admins)."""
        if not self.is_admin(ctx.author):
            raise commands.MissingPermissions(["manage_guild"])
        cfg = self.cfg(ctx.guild.id)
        if member is None:
            text = "\n".join(f"<@{u}>" for u in cfg["blacklist"]) or "*nobody is blacklisted*"
            return await ctx.reply(embed=discord.Embed(color=BRAND, title=f"{I.s('ticket')} Ticket blacklist", description=text),
                                   mention_author=False, allowed_mentions=NONE)
        if member.id in cfg["blacklist"]:
            cfg["blacklist"].remove(member.id)
            msg = f"{member.mention} can open tickets again."
        else:
            cfg["blacklist"].append(member.id)
            msg = f"{member.mention} can't open tickets any more."
        self.save(ctx.guild.id)
        await ctx.reply(ok(msg), mention_author=False, allowed_mentions=NONE)


# ------------------------------------------------------------------ the control panel
HOME_OPTIONS = (
    ("create", "Create category", "Add a new ticket category", "plus"),
    ("edit", "Edit category", "Name, emoji, form question, where tickets open, ping roles", "edit"),
    ("panel", "Edit panel message", "Title, text, colour and dropdown text of the panel", "message"),
    ("welcome", "Edit ticket message", "The message inside every new ticket", "ticket"),
    ("banner", "Upload banner", "Image / GIF on top of the panel", "image"),
    ("logs", "Log channels", "Opened / closed / transcript logs and the tickets' Discord category", "scroll"),
    ("roles", "Support roles", "Who can see, claim and close tickets", "shield_star"),
    ("settings", "Settings", "Max tickets per member, channel name, opener may close", "settings"),
    ("send", "Send / update panel", "Post the panel in a channel", "upload"),
)


class TicketAdmin(OwnedView):
    """$ticket - everything is edited here: home -> (categories | logs | roles | send)."""

    def __init__(self, cog: Tickets, invoker: discord.Member):
        super().__init__(invoker.id, timeout=900)
        self.cog, self.guild = cog, invoker.guild
        self.page: str = "home"
        self.cat_id: Optional[str] = None
        self.render()

    @property
    def cfg(self) -> dict:
        return self.cog.cfg(self.guild.id)

    def ch(self, cid) -> str:
        c = self.guild.get_channel(cid) if cid else None
        return c.mention if c else "*not set*"

    # ----- embeds -----
    def embed(self, note: str = "") -> discord.Embed:
        c = self.cfg
        if self.page == "cat":
            cat = self.cog.category(c, self.cat_id) or {}
            e = discord.Embed(color=BRAND, title=f"{I.s('edit')} {cat.get('emoji') or ''} {cat.get('name', 'Category')}".strip(),
                              description=f"{I.s('message')} {cat.get('description') or '*no description*'}")
            e.add_field(name="Form question", value=cat.get("question") or "*none - the ticket opens straight away*", inline=False)
            e.add_field(name="Tickets open under", value=self.ch(cat.get("parent") or c.get("open_category")), inline=True)
            roles = " ".join(f"<@&{r}>" for r in cat.get("ping_roles", [])) or "*none*"
            e.add_field(name="Extra roles (pinged + can see)", value=roles, inline=True)
            e.set_footer(text="Use the menus below to change where it opens and which roles are added.")
        elif self.page == "catpick":
            e = discord.Embed(color=BRAND, title=f"{I.s('edit')} Edit category", description="Pick the category you want to change.")
        elif self.page == "logs":
            lg = c["logs"]
            e = discord.Embed(color=BRAND, title=f"{I.s('scroll')} Log channels", description=(
                f"{I.s('inbox')} **Opened log:** {self.ch(lg['opened'])}\n"
                f"{I.s('lock')} **Closed log:** {self.ch(lg['closed'])}\n"
                f"{I.s('scroll')} **Transcripts:** {self.ch(lg['transcript'])}\n"
                f"{I.s('folder')} **Tickets open under:** {self.ch(c['open_category'])}\n\n"
                f"Pick a channel in each menu. Leave a menu empty to turn that log off."))
        elif self.page == "roles":
            sr = " ".join(f"<@&{r}>" for r in c["support_roles"]) or "*none*"
            ar = " ".join(f"<@&{r}>" for r in c["admin_roles"]) or "*none*"
            e = discord.Embed(color=BRAND, title=f"{I.s('shield_star')} Support roles", description=(
                f"**Support roles** - see, answer, claim and close tickets, get pinged:\n{sr}\n\n"
                f"**Admin roles** - same, plus they can open this panel and force-claim:\n{ar}\n\n"
                f"Members with *Manage Server* always have full access."))
        elif self.page == "send":
            e = discord.Embed(color=BRAND, title=f"{I.s('upload')} Send / update panel", description=(
                f"**Current panel:** {self.ch(c['panel_channel'])}\n"
                f"Pick the channel, then press **Post panel**. If the panel is already posted in that channel it is updated in place."))
        else:
            e = self.home_embed()
        if note:
            e.add_field(name="Done", value=note[:1000], inline=False)
        return e

    def home_embed(self) -> discord.Embed:
        c = self.cfg
        cats = ", ".join(f"{x.get('emoji') or ''} {x['name']}".strip() for x in c["categories"]) or "*none yet*"
        lg = c["logs"]
        e = discord.Embed(color=BRAND, title=f"{I.s('ticket')} Ticket control panel", description=(
            "Everything about tickets is edited here. Pick what to change in the menu below."))
        e.add_field(name=f"Categories ({len(c['categories'])})", value=cats[:1000], inline=False)
        e.add_field(name="Panel", value=(self.ch(c["panel_channel"]) if c["panel_message"] else "*not posted yet*") +
                    ("\nBanner: set" if c["panel"].get("banner") else "\nBanner: none"), inline=True)
        e.add_field(name="Logs", value=f"Opened {self.ch(lg['opened'])}\nClosed {self.ch(lg['closed'])}\nTranscript {self.ch(lg['transcript'])}", inline=True)
        e.add_field(name="Staff", value=f"Support roles: **{len(c['support_roles'])}**\nAdmin roles: **{len(c['admin_roles'])}**", inline=True)
        e.add_field(name="Tickets", value=f"Open now: **{len(c['tickets'])}**\nOpened: **{c['stats']['opened']}** · Closed: **{c['stats']['closed']}**", inline=True)
        e.add_field(name="Rules", value=f"Max open per member: **{c['max_open']}**\nChannel name: `{c['name_format']}`", inline=True)
        e.set_footer(text=f"In a ticket: {PREFIX}ticket close · claim · add @user · remove @user · rename")
        return e

    # ----- components -----
    def add_back(self, row: int):
        b = discord.ui.Button(label="Back", emoji=I.e("left"), style=discord.ButtonStyle.secondary, row=row)
        b.callback = self.go_home
        self.add_item(b)

    def render(self):
        self.clear_items()
        c = self.cfg
        if self.page == "home":
            sel = discord.ui.Select(placeholder="What do you want to change?", row=0, options=[
                discord.SelectOption(label=label, value=val, description=desc, emoji=I.e(icon))
                for val, label, desc, icon in HOME_OPTIONS])
            sel.callback = self.home_pick
            self.add_item(sel)
            b = discord.ui.Button(label="Send / update panel", emoji=I.e("upload"), style=discord.ButtonStyle.success, row=1)
            b.callback = self.quick_send
            self.add_item(b)
        elif self.page == "catpick":
            sel = discord.ui.Select(placeholder="Pick a category...", row=0, options=[
                discord.SelectOption(label=x["name"][:100], value=x["id"], description=(x.get("description") or None),
                                     emoji=to_partial(x.get("emoji"))) for x in c["categories"][:25]])
            sel.callback = self.cat_picked
            self.add_item(sel)
            self.add_back(1)
        elif self.page == "cat":
            cat = self.cog.category(c, self.cat_id) or {}
            for label, emoji, style, cb in (("Edit details", "edit", discord.ButtonStyle.primary, self.cat_edit),
                                            ("Delete", "cross", discord.ButtonStyle.danger, self.cat_delete)):
                b = discord.ui.Button(label=label, emoji=I.e(emoji), style=style, row=0)
                b.callback = cb
                self.add_item(b)
            self.add_back(0)
            par = self.guild.get_channel(cat.get("parent") or 0)
            s1 = discord.ui.ChannelSelect(placeholder="Open tickets under this Discord category...", row=1, min_values=0,
                                          max_values=1, channel_types=[discord.ChannelType.category],
                                          default_values=[par] if par else [])
            s1.callback = lambda i, s=s1: self.cat_parent(i, s)
            self.add_item(s1)
            roles = [r for r in (self.guild.get_role(x) for x in cat.get("ping_roles", [])) if r]
            s2 = discord.ui.RoleSelect(placeholder="Extra roles pinged for this category...", row=2, min_values=0,
                                       max_values=10, default_values=roles)
            s2.callback = lambda i, s=s2: self.cat_roles(i, s)
            self.add_item(s2)
        elif self.page == "logs":
            lg = c["logs"]
            for row, (key, ph) in enumerate((("opened", "Opened log channel..."), ("closed", "Closed log channel..."),
                                             ("transcript", "Transcript channel..."))):
                ch = self.guild.get_channel(lg.get(key) or 0)
                s = discord.ui.ChannelSelect(placeholder=ph, row=row, min_values=0, max_values=1,
                                             channel_types=[discord.ChannelType.text], default_values=[ch] if ch else [])
                s.callback = lambda i, s=s, k=key: self.set_log(i, s, k)
                self.add_item(s)
            par = self.guild.get_channel(c["open_category"] or 0)
            s = discord.ui.ChannelSelect(placeholder="Discord category tickets open under...", row=3, min_values=0, max_values=1,
                                         channel_types=[discord.ChannelType.category], default_values=[par] if par else [])
            s.callback = lambda i, s=s: self.set_parent(i, s)
            self.add_item(s)
            self.add_back(4)
        elif self.page == "roles":
            for row, (key, ph) in enumerate((("support_roles", "Support roles..."), ("admin_roles", "Admin roles..."))):
                roles = [r for r in (self.guild.get_role(x) for x in c[key]) if r]
                s = discord.ui.RoleSelect(placeholder=ph, row=row, min_values=0, max_values=10, default_values=roles)
                s.callback = lambda i, s=s, k=key: self.set_roles(i, s, k)
                self.add_item(s)
            self.add_back(2)
        elif self.page == "send":
            ch = self.guild.get_channel(c["panel_channel"] or 0)
            s = discord.ui.ChannelSelect(placeholder="Channel for the panel...", row=0, min_values=1, max_values=1,
                                         channel_types=[discord.ChannelType.text], default_values=[ch] if ch else [])
            s.callback = lambda i, s=s: self.pick_panel_channel(i, s)
            self.add_item(s)
            b = discord.ui.Button(label="Post panel", emoji=I.e("upload"), style=discord.ButtonStyle.success, row=1)
            b.callback = self.quick_send
            self.add_item(b)
            self.add_back(1)

    async def push(self, interaction: discord.Interaction, note: str = ""):
        self.render()
        await interaction.response.edit_message(embed=self.embed(note), view=self)

    async def refresh(self, note: str = ""):
        """After a modal / upload: redraw the panel message itself (the interaction was answered separately)."""
        self.render()
        if self.message:
            try:
                await self.message.edit(embed=self.embed(note), view=self)
            except discord.HTTPException:
                pass

    async def saved(self, interaction: discord.Interaction, text: str, *, panel: bool = True):
        self.cog.save(self.guild.id)
        if panel:
            await self.cog.refresh_panel(self.guild)
        await say(interaction, ok(text))
        await self.refresh()

    # ----- navigation -----
    async def go_home(self, interaction: discord.Interaction):
        self.page = "home"
        await self.push(interaction)

    async def home_pick(self, interaction: discord.Interaction):
        v = interaction.data["values"][0]
        c = self.cfg
        if v == "create":
            if len(c["categories"]) >= MAX_CATEGORIES:
                return await say(interaction, err(f"You can have at most {MAX_CATEGORIES} categories."))
            return await interaction.response.send_modal(self.cat_modal(None))
        if v == "edit":
            if not c["categories"]:
                return await say(interaction, err("Create a category first."))
            self.page = "catpick"
            return await self.push(interaction)
        if v == "panel":
            return await interaction.response.send_modal(self.panel_modal())
        if v == "welcome":
            return await interaction.response.send_modal(self.welcome_modal())
        if v == "banner":
            return await interaction.response.send_modal(media.ImageModal(
                self.cog.bot, self.guild, "Panel banner", self.banner_done, label="Ticket banner", allow_clear=True,
                fallback=interaction.channel))
        if v == "settings":
            return await interaction.response.send_modal(self.settings_modal())
        self.page = v
        await self.push(interaction)

    # ----- categories -----
    def cat_modal(self, cat: Optional[dict]) -> TKModal:
        async def submitted(i: discord.Interaction, v):
            name, emoji, desc, question = v
            if not name:
                raise VCError("Give the category a name.")
            em = clean_emoji(emoji) if emoji else None
            c = self.cfg
            if cat is None:
                new = {"id": uuid.uuid4().hex[:8], "name": name, "emoji": em, "description": desc or None,
                       "question": question or None, "parent": None, "ping_roles": []}
                c["categories"].append(new)
                self.cat_id, self.page = new["id"], "cat"
                return await self.saved(i, f"Category **{name}** created. Set where its tickets open below, then post the panel.")
            cat.update(name=name, emoji=em, description=desc or None, question=question or None)
            await self.saved(i, f"Category **{name}** updated.")
        return TKModal("Ticket category" if cat is None else "Edit category", [
            dict(label="Name", default=(cat or {}).get("name"), max=40, required=True, placeholder="General Support"),
            dict(label="Emoji (optional)", default=(cat or {}).get("emoji"), max=64),
            dict(label="Short description (optional)", default=(cat or {}).get("description"), max=100,
                 placeholder="Shown under the name in the dropdown"),
            dict(label="Form question (empty = no form)", default=(cat or {}).get("question"), max=100,
                 placeholder="Kindly mention your issue!")], submitted)

    async def cat_picked(self, interaction: discord.Interaction):
        self.cat_id, self.page = interaction.data["values"][0], "cat"
        await self.push(interaction)

    async def cat_edit(self, interaction: discord.Interaction):
        cat = self.cog.category(self.cfg, self.cat_id)
        if cat is None:
            return await say(interaction, err("That category is gone."))
        await interaction.response.send_modal(self.cat_modal(cat))

    async def cat_delete(self, interaction: discord.Interaction):
        c = self.cfg
        cat = self.cog.category(c, self.cat_id)
        if cat is not None:
            c["categories"].remove(cat)
            self.cog.save(self.guild.id)
            await self.cog.refresh_panel(self.guild)
        self.page, self.cat_id = "home", None
        await self.push(interaction, f"Category **{(cat or {}).get('name', '')}** deleted. Open tickets of it stay open.")

    async def cat_parent(self, interaction: discord.Interaction, sel: discord.ui.ChannelSelect):
        cat = self.cog.category(self.cfg, self.cat_id)
        if cat is not None:
            cat["parent"] = sel.values[0].id if sel.values else None
            self.cog.save(self.guild.id)
        await self.push(interaction)

    async def cat_roles(self, interaction: discord.Interaction, sel: discord.ui.RoleSelect):
        cat = self.cog.category(self.cfg, self.cat_id)
        if cat is not None:
            cat["ping_roles"] = [r.id for r in sel.values]
            self.cog.save(self.guild.id)
        await self.push(interaction)

    # ----- messages -----
    def panel_modal(self) -> TKModal:
        p = self.cfg["panel"]

        async def submitted(i: discord.Interaction, v):
            title, desc, color, ph = v
            if not title or not desc:
                raise VCError("Title and text can't be empty.")
            col = parse_color(color)
            p.update(title=title, description=desc, placeholder=ph or "Choose a category...")
            if col is not None:
                p["color"] = col
            await self.saved(i, "Panel message updated.")
        return TKModal("Panel message", [
            dict(label="Title", default=p["title"], max=100, required=True),
            dict(label="Text", default=p["description"], max=3000, required=True, long=True),
            dict(label="Colour (hex)", default=f"#{p.get('color', BRAND):06X}", max=9),
            dict(label="Dropdown placeholder", default=p.get("placeholder"), max=100)], submitted)

    def welcome_modal(self) -> TKModal:
        w = self.cfg["welcome"]

        async def submitted(i: discord.Interaction, v):
            if not v[0] or not v[1]:
                raise VCError("Title and text can't be empty.")
            w.update(title=v[0], description=v[1])
            await self.saved(i, "Ticket message updated.", panel=False)
        return TKModal("Message inside tickets", [
            dict(label="Title", default=w["title"], max=200, required=True),
            dict(label="Text ({user} {category} {ticket} {server})", default=w["description"], max=2000, required=True,
                 long=True)], submitted)

    def settings_modal(self) -> TKModal:
        c = self.cfg

        async def submitted(i: discord.Interaction, v):
            try:
                mx = int(v[0])
            except ValueError:
                raise VCError("Max open tickets must be a number.")
            if not 1 <= mx <= 10:
                raise VCError("Max open tickets must be 1-10.")
            fmt = v[1] or "ticket-{user}"
            if not safe_name(fmt.replace("{user}", "u").replace("{number}", "1").replace("{category}", "c")):
                raise VCError("Use letters, numbers and dashes in the channel name.")
            c.update(max_open=mx, name_format=fmt, opener_can_close=v[2].lower() not in ("no", "n", "false", "0", "off"))
            await self.saved(i, "Settings saved.", panel=False)
        return TKModal("Ticket settings", [
            dict(label="Max open tickets per member (1-10)", default=str(c["max_open"]), max=2, required=True),
            dict(label="Channel name ({user} {number} {category})", default=c["name_format"], max=60, required=True),
            dict(label="Opener may close the ticket? (yes / no)", default="yes" if c["opener_can_close"] else "no", max=3)],
            submitted)

    async def banner_done(self, interaction: discord.Interaction, item: Optional[dict]):
        p = self.cfg["panel"]
        await media.forget(self.cog.bot, p.get("banner"))
        p["banner"] = item
        await self.saved(interaction, "Banner updated." if item else "Banner removed.")

    # ----- logs / roles / panel -----
    async def set_log(self, interaction: discord.Interaction, sel: discord.ui.ChannelSelect, key: str):
        chan = self.guild.get_channel(sel.values[0].id) if sel.values else None
        if chan is not None:
            miss = [n for n in ("view_channel", "send_messages", "embed_links", "attach_files")
                    if not getattr(chan.permissions_for(self.guild.me), n)]
            if miss:
                return await say(interaction, err(f"I need **{', '.join(m.replace('_', ' ').title() for m in miss)}** in {chan.mention}."))
        self.cfg["logs"][key] = chan.id if chan else None
        self.cog.save(self.guild.id)
        await self.push(interaction)

    async def set_parent(self, interaction: discord.Interaction, sel: discord.ui.ChannelSelect):
        self.cfg["open_category"] = sel.values[0].id if sel.values else None
        self.cog.save(self.guild.id)
        await self.push(interaction)

    async def set_roles(self, interaction: discord.Interaction, sel: discord.ui.RoleSelect, key: str):
        self.cfg[key] = [r.id for r in sel.values]
        self.cog.save(self.guild.id)
        await self.push(interaction)

    async def pick_panel_channel(self, interaction: discord.Interaction, sel: discord.ui.ChannelSelect):
        c = self.cfg
        new = sel.values[0].id
        if c["panel_channel"] != new:
            c["panel_channel"] = new
            self.cog.save(self.guild.id)
        await self.push(interaction)

    async def quick_send(self, interaction: discord.Interaction):
        c = self.cfg
        if not c["panel_channel"]:
            self.page = "send"
            return await self.push(interaction, "Pick a channel first, then press **Post panel**.")
        await interaction.response.defer()
        try:
            msg = await self.cog.publish_panel(self.guild)
        except VCError as e:
            return await interaction.followup.send(err(str(e)), ephemeral=True)
        self.render()
        await interaction.edit_original_response(embed=self.embed(f"Panel posted in {msg.channel.mention} · [jump]({msg.jump_url})"), view=self)


async def setup(bot):
    await bot.add_cog(Tickets(bot))
