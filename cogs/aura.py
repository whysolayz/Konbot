"""$aura - plain message: "Aura this user holds! @user:" + `[██████░░░░]` `60%` (the bar fills in 3 ticks, message is edited).

    $aura                    your aura
    $aura @member            theirs (also works as a reply, or with an ID)
    $aura <any text>         your aura (extra text is ignored, e.g. "$aura of ultimate lazyness")
    $aura config             (admins) fix the aura of members / roles, random on / off, switch a person's aura off

Priority: fixed aura of the MEMBER > fixed aura of their highest ROLE that has one > random aura.
Random aura = a NEW random amount every single time the command is used: from -100% to +100% (negative aura is possible!).
The bar always fills from the LEFT, for positive and negative aura alike (a negative one shows the minus sign).
$aura history [@member]  graph of the last 24 random readings, best / lowest / average and the latest values.
Every random reading is remembered (last 24 per member) - fixed auras are not logged. The $aura message itself stays clean.
`$aura config` -> "Negative: OFF" brings back positive-only random aura. Fixed aura can be negative too (-100 .. 100).
"""
import asyncio
import io
import random
from typing import Optional

import discord
from discord.ext import commands

from core import chart, flair
from core.config import BLURPLE, BRAND, PREFIX
from core.errors import VCError
from core.icons import I, err, ok
from core.ui import FieldsModal, OwnedView, parse_int

KEY = "aura"
SEGMENTS = 10
TICKS = 3                 # the bar reaches the final value in 3 edits


def bar(p: int) -> str:
    """White blocks (filled) on dotted-black blocks (empty): inside the code chip `[........]` like the original message.
    Positive AND negative aura both fill from the LEFT side (negative used to start from the right = looked backwards).
    A negative aura is told apart by the minus sign and the "Negative aura" title, not by a mirrored bar.
    int(x + 0.5) rounds half UP (Python's round() is banker's rounding: 5% -> 0 blocks, 15% -> 2 blocks)."""
    n = max(0, min(SEGMENTS, int(abs(p) / 100 * SEGMENTS + 0.5)))
    return "█" * n + "░" * (SEGMENTS - n)


def tier(p: int) -> str:
    if p < 0:
        return ("Cursed" if p <= -90 else "Terrible" if p <= -70 else "Bad" if p <= -40 else "Poor" if p <= -15 else "Slightly off")
    return ("Legendary" if p >= 90 else "Strong" if p >= 70 else "Average" if p >= 40 else "Weak" if p >= 15 else "Drained")


class AuraConfigView(OwnedView):
    def __init__(self, cog: "Aura", invoker: discord.Member):
        super().__init__(invoker.id, timeout=600)
        self.cog, self.guild = cog, invoker.guild
        self.render()

    @property
    def cfg(self) -> dict:
        return self.cog.cfg(self.guild.id)

    def embed(self) -> discord.Embed:
        c = self.cfg
        mem = [f"<@{u}> {p}%" for u, p in c["members"].items()]
        rol = [f"<@&{r}> {p}%" for r, p in c["roles"].items()]
        off = [f"<@{u}>" for u in c["disabled"]]
        more = lambda l: " · ".join(l[:12]) + (f" +{len(l) - 12} more" if len(l) > 12 else "") if l else "*none*"
        return discord.Embed(title=f"{I.s('fire')} Aura Config", color=BRAND, description=(
            f"Random: **{'ON' if c['random'] else 'OFF'}** (new amount every time) · "
            f"Negative aura: **{'ON' if c.get('negative', True) else 'OFF'}** (random goes from "
            f"{'-100' if c.get('negative', True) else '0'}% to 100%)\n"
            f"{I.s('user_star')} **Members:** {more(mem)}\n{I.s('crown')} **Roles:** {more(rol)}\n"
            f"{I.s('ban')} **Aura off for:** {more(off)}\n"
            f"-# Member beats role · highest role wins"))

    def render(self):
        self.clear_items()

        def btn(label, emoji, cb, row, style=discord.ButtonStyle.secondary):
            b = discord.ui.Button(label=label, emoji=I.e(emoji), style=style, row=row)
            b.callback = cb
            self.add_item(b)
        btn("Set Member", "user_star", self.set_member, 0, discord.ButtonStyle.primary)
        btn("Set Role", "crown", self.set_role, 0, discord.ButtonStyle.primary)
        btn("Remove", "minus", self.remove, 0)
        btn(f"Random: {'ON' if self.cfg['random'] else 'OFF'}", "refresh", self.t_random, 1,
            discord.ButtonStyle.success if self.cfg["random"] else discord.ButtonStyle.danger)
        btn(f"Negative: {'ON' if self.cfg.get('negative', True) else 'OFF'}", "skull", self.t_negative, 1,
            discord.ButtonStyle.success if self.cfg.get("negative", True) else discord.ButtonStyle.secondary)
        btn("Disable Person", "ban", self.t_person, 1)
        btn("Close", "cross", self.close, 1)

    async def refresh(self):
        if self.message:
            self.render()
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass

    # ----- pick a member / role, then ask for the % -----
    async def _pick(self, interaction: discord.Interaction, kind: str):
        view = discord.ui.View(timeout=120)
        sel = discord.ui.UserSelect(placeholder="Select a member...") if kind == "members" \
            else discord.ui.RoleSelect(placeholder="Select a role...")

        async def picked(i: discord.Interaction):
            target = sel.values[0]
            if kind == "roles" and target.is_default():
                return await i.response.send_message(err("Pick a real role, not @everyone."), ephemeral=True)
            cur = self.cfg[kind].get(str(target.id), "")

            async def cb(i2, v):
                pct = parse_int(v[0], -100, 100, "Aura %")
                self.cfg[kind][str(target.id)] = pct
                self.cog.save(self.guild.id)
                await i2.response.send_message(ok(f"{target.mention} now has **{pct}%** aura."), ephemeral=True,
                                               allowed_mentions=discord.AllowedMentions.none())
                await self.refresh()
            name = getattr(target, "display_name", None) or target.name
            await i.response.send_modal(FieldsModal(f"Aura: {name}"[:45], [("Aura % (-100 to 100)", str(cur), 4, True)], cb))
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message(
            "Pick a member:" if kind == "members" else "Pick a role:", view=view, ephemeral=True)

    async def set_member(self, interaction):
        await self._pick(interaction, "members")

    async def set_role(self, interaction):
        await self._pick(interaction, "roles")

    async def remove(self, interaction: discord.Interaction):
        c = self.cfg
        opts = []
        for u, p in list(c["members"].items())[:15]:
            m = self.guild.get_member(int(u))
            opts.append(discord.SelectOption(label=(m.display_name if m else f"member {u}")[:90], value=f"members:{u}",
                                             description=f"{p}%"))
        for r, p in list(c["roles"].items())[:10]:
            ro = self.guild.get_role(int(r))
            opts.append(discord.SelectOption(label=f"@{ro.name}"[:90] if ro else f"role {r}", value=f"roles:{r}",
                                             description=f"{p}%"))
        if not opts:
            return await interaction.response.send_message(err("Nothing set yet."), ephemeral=True)
        view = discord.ui.View(timeout=120)
        sel = discord.ui.Select(placeholder="Pick what to remove...", options=opts[:25])

        async def picked(i: discord.Interaction):
            kind, _, ident = sel.values[0].partition(":")
            c[kind].pop(ident, None)
            self.cog.save(self.guild.id)
            await i.response.edit_message(content=ok("Removed."), view=None)
            await self.refresh()
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Remove which one?", view=view, ephemeral=True)

    async def t_random(self, interaction: discord.Interaction):
        self.cfg["random"] = not self.cfg["random"]
        self.cog.save(self.guild.id)
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def t_negative(self, interaction: discord.Interaction):
        self.cfg["negative"] = not self.cfg.get("negative", True)
        self.cog.save(self.guild.id)
        self.render()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def t_person(self, interaction: discord.Interaction):
        """Pick a member: aura off for them. Pick them again: aura back on."""
        view = discord.ui.View(timeout=120)
        sel = discord.ui.UserSelect(placeholder="Pick a member...")

        async def picked(i: discord.Interaction):
            target = sel.values[0]
            off = self.cfg["disabled"]
            if target.id in off:
                off.remove(target.id)
                text = ok(f"Aura is back on for {target.mention}.")
            else:
                off.append(target.id)
                text = ok(f"Aura is off for {target.mention}.")
            self.cog.save(self.guild.id)
            await i.response.edit_message(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())
            await self.refresh()
        sel.callback = picked
        view.add_item(sel)
        await interaction.response.send_message("Pick the member (pick again to turn it back on):", view=view, ephemeral=True)

    async def close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(view=None)


class Aura(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._cfg: dict[int, dict] = {}

    # ---------- storage ----------
    def cfg(self, gid: int) -> dict:
        c = self._cfg.get(gid)
        if c is None:
            s = self.bot.db.get_settings(gid, KEY)
            c = self._cfg[gid] = {"members": dict(s.get("members", {})), "roles": dict(s.get("roles", {})),
                                  "random": s.get("random", True), "negative": s.get("negative", True), "disabled": [int(u) for u in s.get("disabled", [])]}
        return c

    def save(self, gid: int):
        self.bot.db.set_settings(gid, KEY, self.cfg(gid))

    def value(self, member: discord.Member) -> Optional[int]:
        """Fixed aura of the member > fixed aura of their highest role > a NEW random amount (if random is on)."""
        c = self.cfg(member.guild.id)
        v = c["members"].get(str(member.id))
        if v is not None:
            return int(v)
        for r in sorted(member.roles, key=lambda r: r.position, reverse=True):
            if str(r.id) in c["roles"]:
                return int(c["roles"][str(r.id)])
        if not c["random"]:
            return None
        return random.randint(-100 if c.get("negative", True) else 0, 100)

    def is_fixed(self, member: discord.Member) -> bool:
        c = self.cfg(member.guild.id)
        return str(member.id) in c["members"] or any(str(r.id) in c["roles"] for r in member.roles)

    async def pick(self, ctx: commands.Context, text: str) -> discord.Member:
        for m in ctx.message.mentions:
            if isinstance(m, discord.Member) and not m.bot:
                return m
        ref = ctx.message.reference
        if ref is not None and not text.strip():
            msg = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if msg is not None:
                m = ctx.guild.get_member(msg.author.id)
                if m is not None and not m.bot:
                    return m
        first = text.split(" ", 1)[0] if text else ""
        if first.isdigit() and len(first) >= 15:
            m = ctx.guild.get_member(int(first))
            if m is not None:
                return m
        return ctx.author

    # ---------- $aura ----------
    @commands.group(name="aura", invoke_without_command=True)
    @commands.guild_only()
    async def aura(self, ctx: commands.Context, *, text: str = ""):
        """$aura [@member] - show the aura of a member (default: you)."""
        member = await self.pick(ctx, text)
        mention = discord.AllowedMentions.none()
        if member.id in self.cfg(ctx.guild.id)["disabled"]:
            who = "Your" if member.id == ctx.author.id else f"{member.mention}'s"
            return await ctx.reply(f"{who} aura is switched off.", mention_author=False, allowed_mentions=mention)
        pct = self.value(member)
        if pct is None:
            return await ctx.reply(f"{member.mention} has no aura yet.", mention_author=False, allowed_mentions=mention)

        head = "Negative aura this user holds!" if pct < 0 else "Aura this user holds!"

        if not self.is_fixed(member):                          # random readings are remembered for $aura history
            try:
                self.bot.db.log_aura(ctx.guild.id, member.id, pct)
            except Exception:
                pass                                           # history is a bonus: never break $aura for it

        def line(p: int) -> str:
            return f"{head} {member.mention}:\n`[{bar(p)}]` `{p}%`"
        msg = await ctx.reply(line(0), mention_author=False, allowed_mentions=mention)
        if pct == 0:
            return
        for i in range(1, TICKS + 1):                         # 3 ticks: the bar fills up and lands on the real value
            await asyncio.sleep(0.7)
            try:
                await msg.edit(content=line(round(pct * i / TICKS)), allowed_mentions=mention)
            except discord.HTTPException:
                break

    @aura.command(name="history", aliases=["log", "past"])
    @commands.guild_only()
    async def aura_history(self, ctx: commands.Context, *, text: str = ""):
        """$aura history [@member] - graph of the last random aura readings."""
        member = await self.pick(ctx, text)
        rows = self.bot.db.aura_rows(ctx.guild.id, member.id, 24)
        who = "You have" if member.id == ctx.author.id else f"**{member.display_name}** has"
        if len(rows) < 2:
            return await ctx.reply(f"{I.s('fire')} {who} fewer than 2 readings yet. Use `{PREFIX}aura` a few times first.",
                                   mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        vals = [v for _, v in rows]
        png = chart.line_chart([{"values": vals, "color": chart.GOLD, "signed": True}], lo=-100, hi=100, zero_line=True,
                               dots=True, fill=False, unit="%", x_left=f"{len(vals)} readings ago", x_right="latest")
        sign, n = flair.streak(list(reversed(vals)))
        streak = f"{n} {'positive' if sign > 0 else 'negative'} in a row" if n >= 2 and sign else "no streak"
        recent = "\n".join(f"`{v:+d}%` <t:{int(t)}:R>" for t, v in reversed(rows[-5:]))
        e = discord.Embed(title=f"{I.s('fire')} Aura history · {member.display_name}", color=BRAND)
        e.add_field(name="Best", value=f"`{max(vals)}%`", inline=True)
        e.add_field(name="Lowest", value=f"`{min(vals)}%`", inline=True)
        e.add_field(name="Average", value=f"`{round(sum(vals) / len(vals))}%`", inline=True)
        e.add_field(name="Latest readings", value=recent, inline=True)
        e.add_field(name="Streak", value=streak, inline=True)
        e.set_image(url="attachment://aura.png")
        await ctx.reply(embed=e, file=discord.File(io.BytesIO(png), "aura.png"), mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())

    @aura.command(name="config")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def aura_config(self, ctx: commands.Context):
        """Set aura for members / roles, random on/off, switch a person's aura off."""
        view = AuraConfigView(self, ctx.author)
        view.message = await ctx.reply(embed=view.embed(), view=view, mention_author=False)


async def setup(bot):
    await bot.add_cog(Aura(bot))
