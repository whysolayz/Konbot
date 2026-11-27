"""Point system: chat + voice points, $profile, leaderboard, $point config, badges."""
import re
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.config import BLURPLE, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import FieldsModal, OwnedView, parse_int

DEFAULTS = {
    "chat_enabled": True, "chat_msgs": 50, "chat_points": 1, "chat_everywhere": True,
    "vc_enabled": True, "vc_mins": 5, "vc_points": 1, "vc_need_others": True,
    "vc_need_muted": True,      # voice points ONLY while the member is muted / deafened (mic off)
    "cooldown": 5,
    "channel_rules": {},        # "<channel_id>": {"msgs": 50, "points": 1}
    "disabled_channels": [],    # chat points are NOT counted here (channel or whole category)
    "enabled_channels": [],     # used when "Everywhere" is OFF: chat points ARE counted here (default rule)
    "vc_disabled_channels": [], # voice points are NOT counted here (voice channel or whole category)
}
LIST_KEYS = ("disabled_channels", "enabled_channels", "vc_disabled_channels")
TICK = 30
TEXT_TYPES = [discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum,
              discord.ChannelType.voice, discord.ChannelType.stage_voice, discord.ChannelType.category]
VOICE_TYPES = [discord.ChannelType.voice, discord.ChannelType.stage_voice, discord.ChannelType.category]
MAX_SHOWN = 25


BADGE_MAX = 100          # a custom emoji alone is ~30 characters, so the old limit of 30 cut it in half
_SHORTCODE = re.compile(r"(?<![<\w]):(\w{2,32}):(?!\d)")      # :name: (but not the inside of <:name:id>)


def clean_badge(bot, raw: str) -> str:
    """Badge text exactly as typed: custom emojis stay whole (<:name:id> / <a:name:id>), :name: turns into the emoji
    when the bot can see it, and nothing is ever cut in the middle of an emoji."""
    name = " ".join((raw or "").split())

    def sub(m):
        e = discord.utils.get(bot.emojis, name=m.group(1))
        return str(e) if e else m.group(0)
    name = _SHORTCODE.sub(sub, name)
    if not name:
        raise VCError("Give the badge a name or an emoji.")
    if len(name) > BADGE_MAX:
        raise VCError(f"That badge is too long (max **{BADGE_MAX}** characters).")
    return name


def show_badge(name: str) -> str:
    return name if "<" in name else f"**{name}**"      # emojis render best without bold markers


_CUSTOM_EMOJI = re.compile(r"<(a?):(\w+):(\d+)>")


def badge_option(name: str, idx: int) -> discord.SelectOption:
    """A badge as a dropdown option: a custom emoji becomes the option's icon, the rest is the label."""
    m = _CUSTOM_EMOJI.search(name)
    icon = None
    label = name
    if m:
        icon = discord.PartialEmoji(name=m.group(2), id=int(m.group(3)), animated=bool(m.group(1)))
        label = _CUSTOM_EMOJI.sub("", name).strip() or m.group(2)
    return discord.SelectOption(label=label[:100] or "badge", value=str(idx), emoji=icon)


class BadgeRemoveView(OwnedView):
    """$badge remove @user -> pick a badge in the menu and it is removed right away."""
    PER = 25

    def __init__(self, cog, invoker: discord.Member, member: discord.Member):
        super().__init__(invoker.id, timeout=180)
        self.cog, self.guild, self.member = cog, invoker.guild, member
        self.page = 0
        self.badges: list = []
        self.render()

    def embed(self, note: str = "") -> discord.Embed:
        e = discord.Embed(color=BRAND, title=f"{I.s('trophy')} Remove a badge",
                          description=f"Pick the badge to remove from {self.member.mention}.\n\n" +
                                      (", ".join(self.badges) if self.badges else "*No badges left.*"))
        if note:
            e.add_field(name="Done", value=note[:1000], inline=False)
        return e

    def render(self):
        self.clear_items()
        self.badges = self.cog.db.badges(self.guild.id, self.member.id)
        pages = max(1, -(-len(self.badges) // self.PER))
        self.page = max(0, min(self.page, pages - 1))
        if not self.badges:
            return
        start = self.page * self.PER
        chunk = self.badges[start:start + self.PER]
        sel = discord.ui.Select(placeholder="Select a badge to remove...", row=0,
                                options=[badge_option(b, start + i) for i, b in enumerate(chunk)])
        sel.callback = self.picked
        self.add_item(sel)
        if pages > 1:
            for label, step in (("Previous", -1), ("Next", 1)):
                b = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=1,
                                      disabled=(self.page + step < 0 or self.page + step >= pages))
                b.callback = (lambda i, st=step: self.turn(i, st))
                self.add_item(b)

    async def turn(self, interaction: discord.Interaction, step: int):
        self.page += step
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def picked(self, interaction: discord.Interaction):
        idx = int(interaction.data["values"][0])
        if idx >= len(self.badges):
            self.render()
            return await interaction.response.edit_message(embed=self.embed("That list changed - pick again."), view=self)
        name = self.badges[idx]
        gone = self.cog.db.remove_badge(self.guild.id, self.member.id, name)
        self.render()
        note = f"Removed {show_badge(name)}." if gone else "They no longer have that badge."
        if not self.badges:
            self.stop()
            return await interaction.response.edit_message(embed=self.embed(note), view=None)
        await interaction.response.edit_message(embed=self.embed(note), view=self)


def onoff(v: bool) -> str:
    return "ON" if v else "OFF"


def chan_ids(ch) -> set:
    """ids that can switch a channel off/on: the channel, its thread parent and its category."""
    ids = {ch.id}
    parent = getattr(ch, "parent", None)
    for c in (ch, parent):
        if c is None:
            continue
        if getattr(c, "id", None):
            ids.add(c.id)
        if getattr(c, "category_id", None):
            ids.add(c.category_id)
    if getattr(ch, "parent_id", None):
        ids.add(ch.parent_id)
    return ids


def fmt_channels(guild: discord.Guild, ids: list, limit: int = 15) -> str:
    if not ids:
        return "*none*"
    out = []
    for cid in ids[:limit]:
        ch = guild.get_channel(cid)
        if ch is None:
            out.append(f"~~deleted `{cid}`~~")
        elif isinstance(ch, discord.CategoryChannel):
            out.append(f"**{ch.name}** (category)")
        else:
            out.append(ch.mention)
    more = f" +{len(ids) - limit} more" if len(ids) > limit else ""
    return " · ".join(out) + more


class PointConfigView(OwnedView):
    """Two pages: Main (rules + switches) and Channels (where points count / don't count)."""

    def __init__(self, cog: "Points", invoker: discord.Member):
        super().__init__(invoker.id, timeout=600)
        self.cog, self.guild = cog, invoker.guild
        self.page = "main"
        self.render()

    @property
    def cfg(self):
        return self.cog.cfg(self.guild.id)

    # ----- embeds -----
    def embed(self) -> discord.Embed:
        return self.embed_main() if self.page == "main" else self.embed_channels()

    def embed_main(self) -> discord.Embed:
        c = self.cfg
        off = len(c["disabled_channels"]) + len(c["vc_disabled_channels"])
        return discord.Embed(title=f"{I.s('settings')} Point Configuration", color=BRAND, description=(
            f"{I.s('message')} **Chat {onoff(c['chat_enabled'])}** · {c['chat_msgs']} msgs = {c['chat_points']} pt · "
            f"cooldown {c['cooldown']}s · everywhere {onoff(c['chat_everywhere'])}\n"
            f"{I.s('mic')} **Voice {onoff(c['vc_enabled'])}** · {c['vc_mins']} min = {c['vc_points']} pt · "
            f"needs 2+ people {onoff(c['vc_need_others'])} · only when mic off/deaf {onoff(c['vc_need_muted'])}\n"
            f"{I.s('lock')} Points off in **{off}** channel(s) · custom rules **{len(c['channel_rules'])}**"))

    def embed_channels(self) -> discord.Embed:
        c, g = self.cfg, self.guild
        everywhere = c["chat_everywhere"]
        e = discord.Embed(title=f"{I.s('lock')} Point Channels", color=BLURPLE, description=(
            f"**Mode:** " + ("Count **everywhere** except the disabled channels" if everywhere
                             else "Count **only** in the enabled channels + channels with a rule")
            + "\nA category covers every channel inside it."))
        e.add_field(name=f"{I.s('cross')} Chat points OFF in", inline=False, value=fmt_channels(g, c["disabled_channels"]))
        if not everywhere:
            e.add_field(name=f"{I.s('check')} Chat points ON in", inline=False, value=fmt_channels(g, c["enabled_channels"]))
        e.add_field(name=f"{I.s('mute')} Voice points OFF in", inline=False, value=fmt_channels(g, c["vc_disabled_channels"]))
        rules = [f"<#{cid}> · **{r['msgs']}** msgs = **{r['points']}** pt" for cid, r in c["channel_rules"].items()]
        e.add_field(name=f"{I.s('tag')} Custom rules", inline=False,
                    value="\n".join(rules[:10]) + (f"\n+{len(rules) - 10} more" if len(rules) > 10 else "") if rules else "*none*")
        e.set_footer(text="Un-select a channel to turn its points back on.")
        return e

    # ----- components -----
    def _btn(self, label, emoji, cb, row, style=discord.ButtonStyle.secondary):
        b = discord.ui.Button(label=label, emoji=I.e(emoji), style=style, row=row)
        b.callback = cb
        self.add_item(b)

    def _select(self, key: str, placeholder: str, types, row: int):
        ids = self.cfg[key][:MAX_SHOWN]
        shown = [ch for ch in (self.guild.get_channel(i) for i in ids) if ch is not None]
        sel = discord.ui.ChannelSelect(placeholder=placeholder, channel_types=types, min_values=0,
                                       max_values=MAX_SHOWN, default_values=shown, row=row)

        async def picked(i: discord.Interaction):
            old = self.cfg[key]
            shown_ids = {ch.id for ch in shown}
            kept = [x for x in old if x not in shown_ids]            # channels that weren't in the menu stay
            self.cfg[key] = kept + [ch.id for ch in sel.values]
            if key == "disabled_channels":                              # a channel can't be both
                self.cfg["enabled_channels"] = [x for x in self.cfg["enabled_channels"] if x not in self.cfg[key]]
            elif key == "enabled_channels":
                self.cfg["disabled_channels"] = [x for x in self.cfg["disabled_channels"] if x not in self.cfg[key]]
            self.save()
            await self.push(i)
        sel.callback = picked
        self.add_item(sel)

    def render(self):
        self.clear_items()
        c = self.cfg
        if self.page == "main":
            self._btn("Chat Rule", "message", self.chat_rule, 0)
            self._btn("VC Rule", "mic", self.vc_rule, 0)
            self._btn("Cooldown", "clock", self.cooldown, 0)
            self._btn(f"Chat: {onoff(c['chat_enabled'])}", "message", self.t_chat, 1,
                      discord.ButtonStyle.success if c["chat_enabled"] else discord.ButtonStyle.danger)
            self._btn(f"VC: {onoff(c['vc_enabled'])}", "mic", self.t_vc, 1,
                      discord.ButtonStyle.success if c["vc_enabled"] else discord.ButtonStyle.danger)
            self._btn(f"VC needs others: {onoff(c['vc_need_others'])}", "users", self.t_others, 2)
            self._btn(f"Only mic off/deaf: {onoff(c['vc_need_muted'])}", "mute", self.t_muted, 2)
            self._btn("Channels", "lock", self.go_channels, 3, discord.ButtonStyle.primary)
            self._btn("Close", "cross", self.close, 3)
        else:
            self._select("disabled_channels", "Chat points OFF in... (pick channels / categories)", TEXT_TYPES, 0)
            row = 1
            if not c["chat_everywhere"]:
                self._select("enabled_channels", "Chat points ON in... (pick channels / categories)", TEXT_TYPES, row)
                row += 1
            self._select("vc_disabled_channels", "Voice points OFF in... (voice channels / categories)", VOICE_TYPES, row)
            self._btn("Mode: everywhere" if c["chat_everywhere"] else "Mode: only enabled", "link", self.t_everywhere, 3,
                      discord.ButtonStyle.success if c["chat_everywhere"] else discord.ButtonStyle.primary)
            self._btn("Add Rule", "plus", self.add_rule, 3)
            self._btn("Remove Rule", "minus", self.remove_rule, 3)
            self._btn("Back", "left", self.go_main, 3)

    async def push(self, interaction: discord.Interaction):
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def refresh_message(self):
        if self.message:
            self.render()
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass

    def save(self):
        self.cog.save_cfg(self.guild.id, self.cfg)

    async def go_channels(self, i):
        self.page = "channels"
        await self.push(i)

    async def go_main(self, i):
        self.page = "main"
        await self.push(i)

    # ----- modals -----
    async def chat_rule(self, interaction):
        c = self.cfg

        async def cb(i, v):
            c["chat_msgs"] = parse_int(v[0], 1, 100000, "Messages")
            c["chat_points"] = parse_int(v[1], 1, 1000, "Points")
            self.save()
            await self.push(i)
        await interaction.response.send_modal(FieldsModal("Chat Rule", [
            ("Messages needed", str(c["chat_msgs"]), 6, True), ("Points awarded", str(c["chat_points"]), 4, True)], cb))

    async def vc_rule(self, interaction):
        c = self.cfg

        async def cb(i, v):
            c["vc_mins"] = parse_int(v[0], 1, 10000, "Minutes")
            c["vc_points"] = parse_int(v[1], 1, 1000, "Points")
            self.save()
            await self.push(i)
        await interaction.response.send_modal(FieldsModal("Voice Rule", [
            ("Minutes in VC needed", str(c["vc_mins"]), 5, True), ("Points awarded", str(c["vc_points"]), 4, True)], cb))

    async def cooldown(self, interaction):
        c = self.cfg

        async def cb(i, v):
            c["cooldown"] = parse_int(v[0], 0, 600, "Cooldown")
            self.save()
            await self.push(i)
        await interaction.response.send_modal(FieldsModal("Message Cooldown", [
            ("Seconds between counted messages", str(c["cooldown"]), 3, True)], cb))

    # ----- channel rules -----
    async def add_rule(self, interaction):
        view = discord.ui.View(timeout=120)
        sel = discord.ui.ChannelSelect(
            placeholder="Select a channel...",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum])

        async def picked(i: discord.Interaction):
            ch = sel.values[0]
            cid = str(ch.id)

            async def cb(i2, v):
                self.cfg["channel_rules"][cid] = {"msgs": parse_int(v[0], 1, 100000, "Messages"),
                                                  "points": parse_int(v[1], 1, 1000, "Points")}
                self.save()
                await i2.response.send_message(ok(f"Rule saved for <#{cid}>."), ephemeral=True)
                await self.refresh_message()
            cur = self.cfg["channel_rules"].get(cid, {"msgs": self.cfg["chat_msgs"], "points": self.cfg["chat_points"]})
            await i.response.send_modal(FieldsModal(f"Rule: #{ch.name}"[:45], [
                ("Messages needed", str(cur["msgs"]), 6, True), ("Points awarded", str(cur["points"]), 4, True)], cb))
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Pick a channel:", view=view, ephemeral=True)

    async def remove_rule(self, interaction):
        rules = self.cfg["channel_rules"]
        if not rules:
            return await interaction.response.send_message(err("No channel rules yet."), ephemeral=True)
        opts = []
        for cid, r in list(rules.items())[:25]:
            ch = self.guild.get_channel(int(cid))
            opts.append(discord.SelectOption(label=f"#{ch.name}" if ch else f"deleted channel {cid}", value=cid,
                                             description=f"{r['msgs']} msgs = {r['points']} pt(s)"))
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder="Select a rule to remove...", options=opts)

        async def picked(i: discord.Interaction):
            self.cfg["channel_rules"].pop(sel.values[0], None)
            self.save()
            await i.response.edit_message(content=ok("Rule removed."), view=None)
            await self.refresh_message()
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Remove which rule?", view=view, ephemeral=True)

    # ----- toggles -----
    async def t_chat(self, i):
        self.cfg["chat_enabled"] = not self.cfg["chat_enabled"]; self.save(); await self.push(i)

    async def t_vc(self, i):
        self.cfg["vc_enabled"] = not self.cfg["vc_enabled"]; self.save(); await self.push(i)

    async def t_everywhere(self, i):
        self.cfg["chat_everywhere"] = not self.cfg["chat_everywhere"]; self.save(); await self.push(i)

    async def t_others(self, i):
        self.cfg["vc_need_others"] = not self.cfg["vc_need_others"]; self.save(); await self.push(i)

    async def t_muted(self, i):
        self.cfg["vc_need_muted"] = not self.cfg["vc_need_muted"]; self.save(); await self.push(i)

    async def close(self, interaction):
        self.stop()
        await interaction.response.edit_message(view=None)


LB_PER_PAGE = 10
LB_LINE = "───⬢⬡⬢──────⬢⬡⬢──────⬢⬡⬢───"          # divider like the Ordinal leaderboard
LB_STATS = {                                       # key: (title, unit, icon name)
    "messages": ("Messages Leaderboard", "Messages", "message"),
    "points": ("Points Leaderboard", "Points", "coins"),
    "voice": ("Voice Leaderboard", "Hours", "headset"),
}
LB_ALIASES = {"msg": "messages", "msgs": "messages", "message": "messages", "chat": "messages", "messages": "messages",
              "point": "points", "pts": "points", "points": "points",
              "vc": "voice", "voice": "voice", "hours": "voice", "hour": "voice"}


class LbStatSelect(discord.ui.Select):
    def __init__(self, current: str):
        super().__init__(placeholder="Leaderboard type", row=1, options=[
            discord.SelectOption(label=t, value=k, emoji=I.e(ic), default=(k == current))
            for k, (t, _, ic) in LB_STATS.items()])

    async def callback(self, interaction: discord.Interaction):
        v: "LeaderboardView" = self.view
        v.stat = self.values[0]
        v.reload()
        for o in self.options:
            o.default = o.value == v.stat
        await interaction.response.edit_message(embed=v.embed(), view=v)


class LeaderboardView(OwnedView):
    """Ordinal-style leaderboard: 10 per page, Previous / Find Me / Next, a select for Messages / Points / Voice."""

    def __init__(self, cog, ctx: commands.Context, stat: str = "points"):
        super().__init__(ctx.author.id, timeout=300)
        self.cog, self.guild, self.author = cog, ctx.guild, ctx.author
        self.stat = stat
        self.page = 0
        self.prev.emoji, self.mine.emoji, self.next.emoji = I.e("left"), I.e("target"), I.e("right")
        self.select = LbStatSelect(stat)
        self.add_item(self.select)
        self.reload()

    def reload(self):
        self.total = self.cog.db.lb_count(self.guild.id, self.stat)
        self.pages = max(1, -(-self.total // LB_PER_PAGE))
        self.page = min(self.page, self.pages - 1)
        self.sync()

    @staticmethod
    def value_text(stat: str, r) -> str:
        if stat == "voice":
            return f"{r['vc_total'] / 3600:,.1f}"
        return f"{r['points' if stat == 'points' else 'chat_total']:,}"

    def embed(self) -> discord.Embed:
        title, unit, icon = LB_STATS[self.stat]
        rows = self.cog.db.lb_rows(self.guild.id, self.stat, LB_PER_PAGE, self.page * LB_PER_PAGE)
        lines = [LB_LINE]
        for i, r in enumerate(rows, self.page * LB_PER_PAGE + 1):
            line = f"{i}.<@{r['user_id']}> — {self.value_text(self.stat, r)} {unit}"
            lines.append(f"**{line}**" if r["user_id"] == self.author.id else line)
        if not rows:
            lines.append("Nobody on the board yet.")
        e = discord.Embed(title=f"{I.s('trophy')} ∫ {title}", color=BRAND, description="\n".join(lines))
        e.set_footer(text=f"Page {self.page + 1}/{self.pages} • ∫ Kon")
        return e

    def sync(self):
        self.prev.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1

    async def go(self, interaction: discord.Interaction, page: int):
        self.page = min(max(page, 0), self.pages - 1)
        self.sync()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.go(interaction, self.page - 1)

    @discord.ui.button(label="Find Me", style=discord.ButtonStyle.secondary, row=0)
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        pos = self.cog.db.lb_position(self.guild.id, self.author.id, self.stat)
        if pos is None:
            return await interaction.response.send_message(err("You aren't on this leaderboard yet."), ephemeral=True)
        await self.go(interaction, (pos - 1) // LB_PER_PAGE)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.go(interaction, self.page + 1)


class Points(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self._cfg: dict[int, dict] = {}
        self._last: dict[tuple, float] = {}
        self._tick_at = time.monotonic()

    async def cog_load(self):
        fixed = self.db.repair_badges()           # badges saved half-cut by the old 30-character limit
        if fixed:
            import logging
            logging.getLogger("kon.points").info("repaired %d cut-off emoji badge(s)", fixed)
        self.vc_tick.start()

    async def cog_unload(self):
        self.vc_tick.cancel()

    # ---------- config ----------
    def cfg(self, gid: int) -> dict:
        c = self._cfg.get(gid)
        if c is None:
            stored = self.db.get_settings(gid, "points")
            c = {**DEFAULTS, **stored}
            c["channel_rules"] = dict(stored.get("channel_rules", {}))
            for k in LIST_KEYS:
                c[k] = [int(x) for x in stored.get(k, [])]
            self._cfg[gid] = c
        return c

    def save_cfg(self, gid: int, cfg: dict):
        self._cfg[gid] = cfg
        self.db.set_settings(gid, "points", cfg)

    # ---------- earning ----------
    @commands.Cog.listener()
    async def on_message(self, m: discord.Message):
        if m.author.bot or not m.guild or m.content.startswith(PREFIX):
            return
        if not m.content.strip() and not m.attachments:
            return
        if self.bot.is_noprefix(m.author):           # a no-prefix command must not count as chat
            ctx = await self.bot.get_context(m)
            if ctx.valid:
                return
        cfg = self.cfg(m.guild.id)
        if not cfg["chat_enabled"]:
            return
        cid = getattr(m.channel, "parent_id", None) or m.channel.id
        ids = chan_ids(m.channel)
        if ids & set(cfg["disabled_channels"]):          # points switched off here (channel or its category)
            return
        rule = cfg["channel_rules"].get(str(cid))
        if rule:
            per, pts, use_ch = rule["msgs"], rule["points"], True
        elif cfg["chat_everywhere"] or ids & set(cfg["enabled_channels"]):
            per, pts, use_ch = cfg["chat_msgs"], cfg["chat_points"], False
        else:
            return
        now = time.monotonic()
        key = (m.guild.id, m.author.id)
        if now - self._last.get(key, -1e9) < cfg["cooldown"]:
            return
        if len(self._last) > 50000:
            self._last.clear()
        self._last[key] = now
        self.db.bump_chat(m.guild.id, m.author.id, cid, per, pts, use_ch)

    @tasks.loop(seconds=TICK)
    async def vc_tick(self):
        now = time.monotonic()
        secs = int(min(max(now - self._tick_at, 1), 90))
        self._tick_at = now
        rows = []
        for guild in self.bot.guilds:
            cfg = self.cfg(guild.id)
            if not cfg["vc_enabled"]:
                continue
            afk = guild.afk_channel
            off = set(cfg["vc_disabled_channels"])
            for vc in guild.voice_channels:
                if afk and vc.id == afk.id:
                    continue
                if vc.id in off or (vc.category_id and vc.category_id in off):
                    continue
                humans = [m for m in vc.members if not m.bot]
                if cfg["vc_need_others"] and len(humans) < 2:
                    continue
                for m in humans:
                    vs = m.voice
                    if vs is None or vs.afk:
                        continue
                    if cfg["vc_need_muted"]:
                        # points only while the mic is off or the member is deafened (self or server)
                        if not (vs.self_mute or vs.mute or vs.self_deaf or vs.deaf):
                            continue
                    elif vs.deaf or vs.self_deaf:               # old rule (switch OFF): everyone except deafened
                        continue
                    rows.append((guild.id, m.id, secs, cfg["vc_mins"] * 60, cfg["vc_points"]))
        if rows:
            self.db.bump_vc_many(rows)

    @vc_tick.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()
        self._tick_at = time.monotonic()

    # ---------- profile ----------
    @commands.hybrid_command(name="profile", aliases=['stats', 'pf'], description="View your points, chat and VC progress")
    @app_commands.describe(member="Member to look at (default: you)")
    @commands.guild_only()
    async def profile(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        member = member or ctx.author
        gid = ctx.guild.id
        u = self.db.user(gid, member.id)
        c = self.cfg(gid)
        badges = self.db.badges(gid, member.id)
        embed = discord.Embed(
            description=(
                f"{I.s('heart')} **Points:** {u['points']}\n"
                f"{I.s('message')} **Chat Progress:** {u['chat_progress']}/{c['chat_msgs']} msgs\n"
                f"{I.s('mic')} **VC Progress:** {u['vc_progress'] // 60}/{c['vc_mins']} mins\n\n"
                f"**Badges:** {', '.join(badges) if badges else 'None'}"),
            color=BLURPLE, timestamp=discord.utils.utcnow())
        embed.set_author(name=f"{member.name}'s Status",
                         icon_url=ctx.guild.icon.url if ctx.guild.icon else member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    # ---------- leaderboard ----------
    @commands.hybrid_command(name="leaderboard", aliases=["lb", "top"], description="Leaderboard: points (default), messages or voice hours")
    @app_commands.describe(stat="points (default), messages or voice")
    @app_commands.choices(stat=[app_commands.Choice(name="Messages", value="messages"),
                                app_commands.Choice(name="Points", value="points"),
                                app_commands.Choice(name="Voice hours", value="voice")])
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context, stat: str = "points"):
        stat = LB_ALIASES.get(stat.lower().strip(), None)
        if stat is None:
            raise VCError(f"Use `{PREFIX}lb`, `{PREFIX}lb messages` or `{PREFIX}lb voice`.")
        if self.db.lb_count(ctx.guild.id, stat) == 0:
            raise VCError("Nobody is on that leaderboard yet. Start chatting!")
        view = LeaderboardView(self, ctx, stat)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False,
                                       allowed_mentions=discord.AllowedMentions.none())

    # ---------- $point (admin) ----------
    @commands.group(name="point", hidden=True, invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def point(self, ctx: commands.Context):
        """Admin: config / add / remove / set / reset."""
        await ctx.reply(f"{I.s('settings')} `{PREFIX}point config` · `enable` · `disable` · `add` · `remove` · `set` · `reset`",
                        mention_author=False)

    @point.command(name="config")
    @commands.has_permissions(manage_guild=True)
    async def point_config(self, ctx: commands.Context):
        """Customize message / voice points, cooldown and channel rules."""
        view = PointConfigView(self, ctx.author)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False)

    @point.command(name="disable", aliases=["off"])
    @commands.has_permissions(manage_guild=True)
    async def point_disable(self, ctx, channels: commands.Greedy[discord.abc.GuildChannel]):
        """$point disable [#channels / categories] - stop counting points there (empty = this channel)"""
        channels = channels or [ctx.channel]
        c = self.cfg(ctx.guild.id)
        for ch in channels:
            if ch.id not in c["disabled_channels"]:
                c["disabled_channels"].append(ch.id)
            if isinstance(ch, (discord.VoiceChannel, discord.StageChannel, discord.CategoryChannel)) \
                    and ch.id not in c["vc_disabled_channels"]:
                c["vc_disabled_channels"].append(ch.id)
            c["enabled_channels"] = [x for x in c["enabled_channels"] if x != ch.id]
        self.save_cfg(ctx.guild.id, c)
        await ctx.reply(ok("Points off in " + ", ".join(ch.mention for ch in channels) + "."),
                        mention_author=False)

    @point.command(name="enable", aliases=["on"])
    @commands.has_permissions(manage_guild=True)
    async def point_enable(self, ctx, channels: commands.Greedy[discord.abc.GuildChannel]):
        """$point enable [#channels / categories] - count points there again (empty = this channel)"""
        channels = channels or [ctx.channel]
        c = self.cfg(ctx.guild.id)
        for ch in channels:
            c["disabled_channels"] = [x for x in c["disabled_channels"] if x != ch.id]
            c["vc_disabled_channels"] = [x for x in c["vc_disabled_channels"] if x != ch.id]
            if not c["chat_everywhere"] and ch.id not in c["enabled_channels"]:
                c["enabled_channels"].append(ch.id)
        self.save_cfg(ctx.guild.id, c)
        await ctx.reply(ok("Points on in " + ", ".join(ch.mention for ch in channels) + "."),
                        mention_author=False)

    @point.command(name="add")
    @commands.has_permissions(manage_guild=True)
    async def point_add(self, ctx, member: discord.Member, amount: commands.Range[int, 1, 10_000_000]):
        """$point add @user <amount>"""
        new = self.db.add_points(ctx.guild.id, member.id, amount, f"admin add by {ctx.author.id}")
        await ctx.reply(ok(f"Gave {member.mention} **{amount}** points. Now **{new}**."), mention_author=False)

    @point.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def point_remove(self, ctx, member: str, amount: commands.Range[int, 1, 10_000_000]):
        """$point remove @user|user_id <amount>"""
        raw = member.strip().strip("<@!>")
        target_id = int(raw) if raw.isdigit() else None
        target = ctx.guild.get_member(target_id) if target_id else ctx.guild.get_member_named(member)
        if target is not None:
            target_id = target.id
            label = target.mention
        elif target_id is not None and target_id > 0:
            label = f"`{target_id}`"
        else:
            raise commands.BadArgument("Use a member mention, username, or numeric user ID.")
        new = self.db.add_points(ctx.guild.id, target_id, -amount, f"admin remove by {ctx.author.id}")
        await ctx.reply(ok(f"Took **{amount}** points from {label}. Now **{new}**."), mention_author=False)

    @point.command(name="transfer", aliases=["send"])
    @commands.cooldown(1, 30, commands.BucketType.user)
    @commands.guild_only()
    async def point_transfer(self, ctx, member: discord.Member, amount: commands.Range[int, 1, 10_000_000]):
        """$point transfer @user <amount> - send points from your balance."""
        if member.bot or member.id == ctx.author.id:
            raise commands.BadArgument("Choose another member, not yourself or a bot.")
        row = self.db.conn.execute(
            "SELECT points FROM users WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, ctx.author.id)
        ).fetchone()
        balance = int(row[0]) if row else 0
        if amount > balance:
            return await ctx.reply(err(f"You only have **{balance}** points."), mention_author=False)
        self.db.add_points(ctx.guild.id, ctx.author.id, -amount, f"transfer to {member.id}")
        new = self.db.add_points(ctx.guild.id, member.id, amount, f"transfer from {ctx.author.id}")
        await ctx.reply(ok(f"Sent **{amount}** points to {member.mention}. Their balance is now **{new}**."),
                        mention_author=False)

    @point.command(name="set")
    @commands.has_permissions(manage_guild=True)
    async def point_set(self, ctx, member: discord.Member, amount: commands.Range[int, 0, 100_000_000]):
        """$point set @user <amount>"""
        new = self.db.set_points(ctx.guild.id, member.id, amount)
        await ctx.reply(ok(f"{member.mention} has **{new}** points now."), mention_author=False)

    @point.command(name="reset")
    @commands.has_permissions(manage_guild=True)
    async def point_reset(self, ctx, member: discord.Member):
        """$point reset @user - wipe points, progress and badges."""
        self.db.reset_user(ctx.guild.id, member.id)
        await ctx.reply(ok(f"Reset {member.mention}."), mention_author=False)

    # ---------- badges ----------
    @commands.group(name="badge", invoke_without_command=True)
    @commands.guild_only()
    async def badge(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """$badge [@user] - list badges."""
        member = member or ctx.author
        b = self.db.badges(ctx.guild.id, member.id)
        await ctx.reply(f"{I.s('trophy')} **{member.display_name}** badges: {', '.join(b) if b else 'None'}",
                        mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    @badge.command(name="give")
    @commands.has_permissions(manage_guild=True)
    async def badge_give(self, ctx, member: discord.Member, *, name: str):
        """$badge give @user <name>"""
        name = clean_badge(self.bot, name)
        if not self.db.add_badge(ctx.guild.id, member.id, name):
            raise VCError("They already have it.")
        await ctx.reply(ok(f"Gave {show_badge(name)} to {member.mention}."), mention_author=False)

    @badge.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    async def badge_remove(self, ctx, member: discord.Member, *, name: str = ""):
        """$badge remove @user - remove ALL their badges (or `$badge remove @user <name>` for just one)."""
        quiet = discord.AllowedMentions.none()
        if name.strip():
            name = clean_badge(self.bot, name)
            if not self.db.remove_badge(ctx.guild.id, member.id, name):
                raise VCError("They don't have that badge.")
            return await ctx.reply(ok(f"Removed {show_badge(name)} from {member.mention}."), mention_author=False,
                                   allowed_mentions=quiet)
        n = self.db.clear_badges(ctx.guild.id, member.id)
        if not n:
            raise VCError(f"{member.mention} has no badges.")
        await ctx.reply(ok(f"Removed all **{n}** badge(s) from {member.mention}."), mention_author=False,
                        allowed_mentions=quiet)


async def setup(bot):
    await bot.add_cog(Points(bot))
