"""$transfer - move roles (and points) from one person to another. Move roles from one person to another, even if the first person LEFT the server.

    $transfer                          opens the transfer panel (pick everything with menus)
    $transfer @from @to                panel with both already filled in - one tap to confirm
    $transfer <user id> @to            the person already LEFT: Kon gives @to the roles saved when they left
    $transfer <name> @to               same, found by the name Kon saved (`$transfer rahul @to`)
    $transfer @from @to @Role1 @Role2  only those roles

What the panel does
  * "Someone who left" menu - every member Kon saw leave (with how many roles + when). Pick one, done.
  * Tick / untick single roles in the role menu before you confirm (all movable roles start ticked).
  * Points move too (toggle "Points: ON/OFF"): the whole balance of the old account goes to the new one, also for people who left.
  * Live preview: what moves, what is skipped and WHY (managed role, above you, above me, already has it, deleted).
  * Undo button for 10 minutes after the transfer.
  * History button: the last 10 transfers in this server (who, to whom, which roles, undone or not).

How the "left" part works: whenever a member leaves (or is kicked / banned) while Kon is in the server, Kon saves the
roles they had. Discord forgets them, Kon does not (kept 180 days; if they rejoin, the record is dropped).
People who left while Kon was offline, or before this version, can't be transferred - Kon never saw their roles.

Who may use it: the bot owner, the members added with `$konextra` in this server, and server Administrators.
(`$vctransfer` is the command that gives your custom VC to someone else.)
"""
import re
import time
from dataclasses import dataclass
from typing import Optional, Union

import discord
from discord.ext import commands

from core.config import BLURPLE, BRAND
from core.errors import VCError
from core.icons import I, err
from core.ui import OwnedView

ID_RE = re.compile(r"^<@!?(\d{15,22})>$|^(\d{15,22})$")
UNDO_SECONDS = 600


@dataclass
class Leaver:
    """A user who is not in the server any more (found by ID / mention / saved name)."""
    id: int


class MemberOrLeaver(commands.Converter):
    """A member of this server, or the ID / mention / saved name of someone who already left."""
    async def convert(self, ctx: commands.Context, argument: str) -> Union[discord.Member, Leaver]:
        try:
            return await commands.MemberConverter().convert(ctx, argument)
        except commands.MemberNotFound:
            m = ID_RE.match(argument.strip())
            if m:
                return Leaver(int(m.group(1) or m.group(2)))
            text = argument.strip().lower()
            found = ctx.bot.db.find_left(ctx.guild.id, text)
            exact = [r for r in found if (r["name"] or "").lower() == text
                     or (r["name"] or "").lower().split(" (@")[0] == text or f"(@{text})" in (r["name"] or "").lower()]
            pick = exact or found
            if len(pick) == 1:
                return Leaver(pick[0]["user_id"])
            if len(pick) > 1:
                raise commands.BadArgument("More than one person who left matches that name - use their ID "
                                           "or pick them in the `$transfer` menu.")
            raise


def kon_admin():
    """Bot owner, per-server Kon owner ($konextra) or server Administrator."""
    async def predicate(ctx: commands.Context):
        gid = ctx.guild.id if ctx.guild else None
        if await ctx.bot.is_owner_in(ctx.author, gid):
            return True
        if ctx.guild and ctx.author.guild_permissions.administrator:
            return True
        raise commands.CheckFailure("not allowed")      # no rights = no reply
    return commands.check(predicate)


def role_list(roles, limit=25) -> str:
    text = " ".join(r.mention for r in roles[:limit])
    return text + (f" +{len(roles) - limit} more" if len(roles) > limit else "")


def ago(ts: float) -> str:
    d = max(0, int(time.time() - ts))
    return (f"{d // 86400}d ago" if d >= 86400 else f"{d // 3600}h ago" if d >= 3600
            else f"{max(1, d // 60)}m ago")


NO_PING = discord.AllowedMentions.none()


class UndoView(OwnedView):
    """Shown under the 'Roles moved' message for 10 minutes."""
    def __init__(self, cog, ctx, log_id, source_member, target, roles, source_id=None, points=0):
        super().__init__(ctx.author.id, timeout=UNDO_SECONDS)
        self.cog, self.log_id, self.source_member, self.target, self.roles = cog, log_id, source_member, target, roles
        self.source_id, self.points = source_id, points
        self.undo.emoji = I.e("refresh")

    @discord.ui.button(label="Undo transfer", style=discord.ButtonStyle.secondary)
    async def undo(self, interaction: discord.Interaction, _):
        self.stop()
        reason = f"$transfer undo by {interaction.user} ({interaction.user.id})"
        notes = []
        try:
            live = [r for r in self.roles if self.target.guild.get_role(r.id)]
            if live:
                await self.target.remove_roles(*live, reason=reason)
                if self.source_member is not None and self.source_member.guild.get_member(self.source_member.id):
                    await self.source_member.add_roles(*live, reason=reason)
                else:
                    notes.append("They are not in the server, so the roles just went back to nobody.")
        except discord.HTTPException as e:
            return await interaction.response.edit_message(embed=discord.Embed(
                description=err(f"Couldn't undo: {getattr(e, 'text', None) or e}"), color=0xED4245), view=None)
        if self.points and self.source_id:
            back = self.cog.bot.db.move_points(self.target.guild.id, self.target.id, self.source_id, self.points,
                                               f"$transfer undo by {interaction.user.id}")
            notes.append(f"{I.s('coins')} **{back:,}** points went back." if back == self.points else
                         f"{I.s('coins')} Only **{back:,}** of **{self.points:,}** points could go back "
                         f"({self.target.display_name} already spent the rest).")
        self.cog.bot.db.mark_undone(self.log_id)
        e = discord.Embed(title=f"{I.s('refresh')} Transfer undone", color=BLURPLE,
                          description=(f"{role_list(self.roles)}\n" if self.roles else "") + "\n".join(notes))
        await interaction.response.edit_message(embed=e, view=None)


class Studio(OwnedView):
    """The whole $transfer experience: menus on top, a live preview in the embed, Transfer at the bottom."""
    def __init__(self, cog: "RoleTransfer", ctx: commands.Context, source=None, target=None, only_roles=None):
        super().__init__(ctx.author.id, timeout=600)
        self.cog, self.ctx, self.g = cog, ctx, ctx.guild
        self.src_member: Optional[discord.Member] = None
        self.src_left: Optional[dict] = None
        self.target: Optional[discord.Member] = None
        self.chosen: Optional[set] = {r.id for r in only_roles} if only_roles else None   # None = every movable role
        self.notice = ""
        self.move_pts = True
        if isinstance(source, discord.Member):
            self.src_member = source
        elif isinstance(source, Leaver):
            self.src_left = cog.bot.db.get_left(self.g.id, source.id)
            if self.src_left is None and cog.bot.db.get_points(self.g.id, source.id) > 0:
                self.src_left = {"user_id": source.id, "name": "", "roles": [], "left_ts": time.time()}   # points only
            if self.src_left is None:
                raise VCError(f"I have no saved roles or points for `{source.id}`.\nI save the roles of people who leave "
                              "**while I'm in the server**, so older leavers can't be transferred.")
        if target is not None:
            self.set_target(target)
        self.render()

    # ---------- state ----------
    def set_target(self, m: discord.Member):
        self.notice = ""
        if m.bot:
            self.notice = "A bot can't receive roles."
        elif self.src_id == m.id:
            self.notice = "Pick two different people."
        else:
            self.target = m

    @property
    def src_id(self) -> Optional[int]:
        return self.src_member.id if self.src_member else (self.src_left["user_id"] if self.src_left else None)

    @property
    def src_points(self) -> int:
        return self.cog.bot.db.get_points(self.g.id, self.src_id) if self.src_id else 0

    def ready(self, movable) -> bool:
        has_something = bool(self.selected(movable)) or (self.move_pts and self.src_points > 0)
        return bool(self.src_id and self.target and has_something)

    @property
    def src_label(self) -> str:
        if self.src_member:
            return self.src_member.mention
        if self.src_left:
            return f"**{discord.utils.escape_markdown(self.src_left['name'] or 'Unknown')}** (`{self.src_left['user_id']}`) *left*"
        return "*not picked yet*"

    async def analyse(self, actor: discord.Member):
        """-> (movable roles, skipped lines). Looks at the roles of the source RIGHT NOW (or the saved ones)."""
        g, me = self.g, self.g.me
        skipped, movable = [], []
        if self.src_member:
            have = {r.id for r in self.src_member.roles}
            wanted = [r for r in self.src_member.roles if not r.is_default()]
        elif self.src_left:
            have = set(self.src_left["roles"])
            wanted = []
            for rid in self.src_left["roles"]:
                r = g.get_role(rid)
                (wanted.append(r) if r else skipped.append(f"`{rid}` - that role was deleted"))
        else:
            return [], []
        bypass = actor.id == g.owner_id or await self.cog.bot.is_owner_in(actor, g.id)
        for r in sorted(wanted, key=lambda x: x.position, reverse=True):
            if r.id not in have or r.is_default():
                continue
            if r.managed:
                skipped.append(f"{r.mention} - managed by an integration")
            elif r >= me.top_role:
                skipped.append(f"{r.mention} - above my highest role")
            elif not bypass and r >= actor.top_role:
                skipped.append(f"{r.mention} - above your highest role")
            elif self.target and r in self.target.roles:
                skipped.append(f"{r.mention} - {self.target.display_name} already has it")
            else:
                movable.append(r)
        return movable, skipped

    def selected(self, movable):
        return [r for r in movable if self.chosen is None or r.id in self.chosen]

    # ---------- embed + components ----------
    async def embed(self) -> discord.Embed:
        movable, skipped = await self.analyse(self.ctx.author)
        chosen = self.selected(movable)
        e = discord.Embed(title=f"{I.s('swap')} Transfer", color=BRAND)
        e.add_field(name="From", value=self.src_label, inline=True)
        e.add_field(name="To", value=self.target.mention if self.target else "*not picked yet*", inline=True)
        if self.src_id:
            e.add_field(name=f"Roles that will move ({len(chosen)}/{len(movable)})", inline=False,
                        value=role_list(chosen) if chosen else "*nothing ticked*")
        if self.src_id:
            pts = self.src_points
            e.add_field(name="Points", inline=False, value=(
                f"{I.s('coins')} **{pts:,}** will move" if self.move_pts and pts else
                "*nothing to move*" if self.move_pts else "*not moving*"))
        if skipped:
            e.add_field(name=f"{I.s('warning')} Skipped ({len(skipped)})", inline=False, value="\n".join(skipped[:8])[:1024])
        if self.notice:
            e.add_field(name="Note", value=f"{I.s('warning')} {self.notice}", inline=False)
        if not self.src_id:
            e.description = "**1.** Pick who it comes from - a member, or someone who **left**."
        elif not self.target:
            e.description = "**2.** Pick who receives it."
        else:
            e.description = "Untick what you don't want to move, then press **Transfer**."
        e.set_footer(text="Kon remembers the roles of everyone who leaves (180 days).")
        return e

    def render(self, movable=None, ready=False):
        self.clear_items()
        row = 0
        leavers = self.cog.bot.db.list_left(self.g.id, 25)
        if leavers:
            opts = []
            for r in leavers:
                opts.append(discord.SelectOption(
                    label=(r["name"] or str(r["user_id"]))[:90], value=str(r["user_id"]),
                    description=f"{len(r['roles'])} roles · left {ago(r['left_ts'])} · ID {r['user_id']}"[:100],
                    default=bool(self.src_left and self.src_left["user_id"] == r["user_id"])))
            sel = discord.ui.Select(placeholder="Someone who LEFT the server...", options=opts, row=row)
            sel.callback = self.pick_left
            self.add_item(sel)
            row += 1
        frm = discord.ui.UserSelect(placeholder="From: a member in the server", row=row)
        frm.callback = self.pick_from
        self.add_item(frm)
        row += 1
        to = discord.ui.UserSelect(placeholder="To: who gets the roles", row=row)
        to.callback = self.pick_to
        self.add_item(to)
        row += 1
        if movable:
            top = movable[:25]
            sel = discord.ui.Select(placeholder="Roles to move (untick to keep out)", min_values=0, max_values=len(top),
                                    row=row, options=[discord.SelectOption(
                                        label=f"@{r.name}"[:90], value=str(r.id),
                                        default=self.chosen is None or r.id in self.chosen) for r in top])
            sel.callback = self.pick_roles
            self.add_item(sel)
            row += 1
        go = discord.ui.Button(label="Transfer", style=discord.ButtonStyle.success, emoji=I.e("check"), row=row,
                               disabled=not ready)
        go.callback = self.go
        pts = discord.ui.Button(label=f"Points: {'ON' if self.move_pts else 'OFF'}", emoji=I.e("coins"), row=row,
                                style=discord.ButtonStyle.primary if self.move_pts else discord.ButtonStyle.secondary)
        pts.callback = self.toggle_points
        hist = discord.ui.Button(label="History", style=discord.ButtonStyle.secondary, emoji=I.e("clock"), row=row)
        hist.callback = self.history
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, emoji=I.e("cross"), row=row)
        cancel.callback = self.cancel
        for b in (go, pts, hist, cancel):
            self.add_item(b)

    async def refresh(self, interaction: discord.Interaction):
        movable, _ = await self.analyse(interaction.user)
        self.render(movable, self.ready(movable))
        await interaction.response.edit_message(embed=await self.embed(), view=self)

    # ---------- menu callbacks ----------
    async def pick_left(self, interaction: discord.Interaction):
        uid = int(interaction.data["values"][0])
        rec = self.cog.bot.db.get_left(self.g.id, uid)
        if rec is None:
            self.notice = "That record is gone (they may have rejoined)."
        else:
            self.src_left, self.src_member, self.chosen, self.notice = rec, None, None, ""
            if self.target and self.target.id == uid:
                self.target = None
        await self.refresh(interaction)

    async def pick_from(self, interaction: discord.Interaction):
        m = self.g.get_member(int(interaction.data["values"][0]))
        if m is None:
            self.notice = "I can't see that member."
        elif m.bot:
            self.notice = "Roles can't be taken from a bot."
        else:
            self.src_member, self.src_left, self.chosen, self.notice = m, None, None, ""
            if self.target and self.target.id == m.id:
                self.target = None
        await self.refresh(interaction)

    async def pick_to(self, interaction: discord.Interaction):
        m = self.g.get_member(int(interaction.data["values"][0]))
        if m is None:
            self.notice = "I can't see that member."
        else:
            self.target = None
            self.set_target(m)
        await self.refresh(interaction)

    async def pick_roles(self, interaction: discord.Interaction):
        movable, _ = await self.analyse(interaction.user)
        shown = {r.id for r in movable[:25]}
        keep = {int(v) for v in interaction.data["values"]}
        hidden = {r.id for r in movable[25:] if self.chosen is None or r.id in self.chosen}   # past the 25-option limit
        self.chosen = (keep & shown) | hidden
        await self.refresh(interaction)

    # ---------- buttons ----------
    async def toggle_points(self, interaction: discord.Interaction):
        self.move_pts = not self.move_pts
        await self.refresh(interaction)

    async def go(self, interaction: discord.Interaction):
        me, g = self.g.me, self.g
        if not me.guild_permissions.manage_roles:
            return await interaction.response.send_message(err("I need **Manage Roles**."), ephemeral=True)
        movable, skipped = await self.analyse(interaction.user)
        roles = self.selected(movable)
        if not self.ready(movable):
            return await interaction.response.send_message(err("Nothing to move yet."), ephemeral=True)
        self.stop()
        await interaction.response.edit_message(embed=discord.Embed(
            description=f"{I.s('hourglass')} Moving...", color=BLURPLE), view=None)
        reason = f"$transfer by {interaction.user} ({interaction.user.id})"
        try:
            if roles:
                await self.target.add_roles(*roles, reason=reason)
                if self.src_member is not None:             # someone who left has nothing to remove
                    await self.src_member.remove_roles(*roles, reason=reason)
        except discord.Forbidden:
            return await interaction.edit_original_response(embed=discord.Embed(
                description=err("I can't move them. I need **Manage Roles** and a higher role."), color=0xED4245))
        except discord.HTTPException as e:
            return await interaction.edit_original_response(embed=discord.Embed(
                description=err(f"Discord said no: {e.text or e.status}"), color=0xED4245))
        moved = 0
        if self.move_pts:
            moved = self.cog.bot.db.move_points(g.id, self.src_id, self.target.id, self.src_points,
                                                f"$transfer by {interaction.user.id}")
        name = (self.src_member.display_name if self.src_member else (self.src_left["name"] or "Unknown"))
        log_id = self.cog.bot.db.log_transfer(g.id, interaction.user.id, self.src_id, name, self.target.id,
                                              [r.id for r in roles], self.src_member is None, moved)
        lines = [f"{self.src_label} → {self.target.mention}", ""]
        if roles:
            lines.append(role_list(roles))
        if moved:
            lines.append(f"{I.s('coins')} **{moved:,}** points")
        e = discord.Embed(title=f"{I.s('check')} Transfer done", color=BRAND, description="\n".join(lines))
        if skipped:
            e.add_field(name=f"{I.s('warning')} Skipped ({len(skipped)})", inline=False, value="\n".join(skipped[:8])[:1024])
        e.set_footer(text="Made a mistake? Undo works for 10 minutes.")
        undo = UndoView(self.cog, self.ctx, log_id, self.src_member, self.target, roles, self.src_id, moved)
        await interaction.edit_original_response(embed=e, view=undo)
        undo.message = await interaction.original_response()

    async def history(self, interaction: discord.Interaction):
        rows = self.cog.bot.db.transfer_history(self.g.id, 10)
        if not rows:
            return await interaction.response.send_message(err("No transfers yet."), ephemeral=True)
        lines = []
        for r in rows:
            roles = [x for x in (self.g.get_role(i) for i in r["roles"]) if x]
            what = role_list(roles, 4) or f"{len(r['roles'])} roles"
            who = discord.utils.escape_markdown(r["source_name"] or str(r["source_id"]))
            left = " *(left)*" if r["left_member"] else ""
            undone = " ~~undone~~" if r["undone"] else ""
            pts = f" + {r['points']:,} points" if r.get("points") else ""
            lines.append(f"<t:{int(r['ts'])}:R> · <@{r['actor_id']}> moved {what}{pts} from **{who}**{left} → <@{r['target_id']}>{undone}")
        await interaction.response.send_message(embed=discord.Embed(
            title=f"{I.s('clock')} Last transfers", description="\n".join(lines)[:4000], color=BLURPLE),
            ephemeral=True, allowed_mentions=NO_PING)

    async def cancel(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(embed=discord.Embed(description=f"{I.s('cross')} Cancelled.",
                                                                    color=BLURPLE), view=None)


class RoleTransfer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---------- remember the roles of everybody who leaves ----------
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.bot:
            return
        roles = [r.id for r in member.roles if not r.is_default()]
        name = member.name if member.display_name == member.name else f"{member.display_name} (@{member.name})"
        try:
            self.bot.db.save_left(member.guild.id, member.id, name, roles)
        except Exception:
            pass                                          # never break leave handling for other cogs

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            self.bot.db.forget_left(member.guild.id, member.id)     # they are back: nothing to transfer
        except Exception:
            pass

    # ---------- $transfer ----------
    @commands.command(name="transfer", aliases=["roletransfer", "rtransfer"])
    @commands.guild_only()
    @kon_admin()
    async def transfer(self, ctx: commands.Context, source: Optional[MemberOrLeaver] = None,
                       target: Optional[discord.Member] = None, roles: commands.Greedy[discord.Role] = None):
        """$transfer [@from | <left user id>] [@to] [@roles...] - role transfer studio (works for people who left)."""
        if not ctx.guild.me.guild_permissions.manage_roles:
            raise VCError("I need **Manage Roles**.")
        studio = Studio(self, ctx, source, target, roles or None)
        movable, _ = await studio.analyse(ctx.author)
        studio.render(movable, studio.ready(movable))
        studio.message = await ctx.reply(embed=await studio.embed(), view=studio, mention_author=False,
                                         allowed_mentions=NO_PING)


async def setup(bot):
    await bot.add_cog(RoleTransfer(bot))
