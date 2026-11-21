"""Custom (join-to-create) voice channels: service + commands + events."""
import asyncio
import contextvars
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.http import Route

from core import emoji, ratelimit
from core.config import BLURPLE, VC_PREFIX
from core.errors import VCError
from core.icons import I, ok
from core.ui import replied_message

log = logging.getLogger("kon.vc")

NOT_IN_VC = "Join your custom VC first."
# everything a VC-banned member loses in that VC (voice AND its text chat)
BAN_DENY = dict(connect=False, view_channel=False, send_messages=False, send_messages_in_threads=False,
                add_reactions=False, read_message_history=False, attach_files=False, embed_links=False,
                create_public_threads=False, create_private_threads=False)
BAN_CLEAR = {k: None for k in BAN_DENY}
CHAT_VC = contextvars.ContextVar("chat_vc", default=None)     # the voice channel whose chat a command was typed in
CFG_KEY = "customvc"


def parse_duration(text: str) -> int:
    """'10m', '1h30m', '45s', '2d' -> seconds (max 24h). A bare number = minutes."""
    text = text.strip().lower()
    if text.isdigit():
        return int(text) * 60
    parts = re.findall(r"(\d+)\s*([smhd])", text)
    if not parts or re.sub(r"(\d+)\s*([smhd])", "", text).strip():
        raise VCError("Try something like `30s`, `10m` or `2h`.")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    secs = sum(int(n) * mult[u] for n, u in parts)
    if secs < 5 or secs > 86400:
        raise VCError("Pick between 5 seconds and 24 hours.")
    return secs


def fmt_secs(s: int) -> str:
    out = []
    for n, u in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s")):
        q, s = divmod(s, n)
        if q:
            out.append(f"{q}{u}")
    return " ".join(out) or "0s"


class VCService:
    """All VC actions. Used by prefix/slash commands AND the interface buttons."""

    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self._creating: set[int] = set()
        self._cfg: dict[int, dict] = {}

    # ---------- helpers ----------
    def is_god(self, member) -> bool:
        """Kon owners (bot owner + $konextra of this server) can manage EVERY custom VC."""
        return self.bot.is_owner_fast(member.id, member.guild.id)

    def owned(self, member: discord.Member):
        """-> (channel, record) of the VC `member` may manage.
        Normal members: only the VC they own and sit in. Kon owners: any custom VC - the one they sit in,
        or the one whose chat they typed the command in."""
        vs = member.voice
        here = vs.channel if vs and vs.channel else None
        if self.is_god(member):
            for ch in (here, CHAT_VC.get()):
                if ch is not None and ch.guild.id == member.guild.id and self.store.vc(ch.id):
                    return ch, self.store.vc(ch.id)
            raise VCError("Join a custom VC, or use the command in its chat.")
        if here is None:
            raise VCError(NOT_IN_VC)
        rec = self.store.vc(here.id)
        if not rec:
            raise VCError("This isn't a custom VC.")
        if rec["owner"] != member.id:
            raise VCError("Only this VC's owner can do that.")
        return here, rec

    # ---------- $customvc config (who can't be vc-banned / kicked / muted) ----------
    def cfg(self, gid: int) -> dict:
        c = self._cfg.get(gid)
        if c is None:
            raw = self.bot.db.get_settings(gid, CFG_KEY)
            c = self._cfg[gid] = {"roles": [int(r) for r in raw.get("roles", [])], "admins": raw.get("admins", True)}
        return c

    def save_cfg(self, gid: int):
        self.bot.db.set_settings(gid, CFG_KEY, self.cfg(gid))

    def reset_cache(self):
        self._cfg.clear()

    def protected(self, target: discord.Member) -> bool:
        """True = this member can't be vc-banned, kicked or muted."""
        if target.id == target.guild.owner_id or self.is_god(target):
            return True
        c = self.cfg(target.guild.id)
        if c["admins"] and target.guild_permissions.administrator:
            return True
        return any(r.id in c["roles"] for r in getattr(target, "roles", ()))

    def guard(self, actor: discord.Member, target: discord.Member, verb: str):
        if self.protected(target) and not self.is_god(actor):
            raise VCError(f"You can't {verb} {target.display_name}.")

    async def _ov(self, ch, target, reason: str, **perms):
        ov = ch.overwrites_for(target)
        ov.update(**perms)
        await ch.set_permissions(target, overwrite=None if ov.is_empty() else ov, reason=reason)

    @staticmethod
    def _http(e: discord.HTTPException) -> VCError:
        if isinstance(e, discord.Forbidden):
            return VCError("I'm missing a permission for that.")
        return VCError(f"Discord said no: {e.text or e.status}")

    # ---------- lock / hide ----------
    async def lock(self, member):
        ch, _ = self.owned(member)
        try:
            await self._ov(ch, ch.guild.default_role, "VC locked", connect=False)
        except discord.HTTPException as e:
            raise self._http(e)
        return "VC locked."

    async def unlock(self, member):
        ch, _ = self.owned(member)
        try:
            await self._ov(ch, ch.guild.default_role, "VC unlocked", connect=None)
        except discord.HTTPException as e:
            raise self._http(e)
        return "VC unlocked."

    async def hide(self, member):
        ch, _ = self.owned(member)
        try:
            await self._ov(ch, ch.guild.default_role, "VC hidden", view_channel=False)
        except discord.HTTPException as e:
            raise self._http(e)
        return "VC hidden."

    async def unhide(self, member):
        ch, _ = self.owned(member)
        try:
            await self._ov(ch, ch.guild.default_role, "VC unhidden", view_channel=None)
        except discord.HTTPException as e:
            raise self._http(e)
        return "VC visible again."

    # ---------- member actions ----------
    async def kick(self, member, target: discord.Member):
        ch, _ = self.owned(member)
        if target.id == member.id:
            raise VCError("You can't kick yourself.")
        if target not in ch.members:
            raise VCError("They're not in this VC.")
        self.guard(member, target, "kick")
        try:
            await target.move_to(None, reason=f"VC kick by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        return f"Kicked **{target.display_name}**."

    async def ban(self, member, target: discord.Member):
        ch, rec = self.owned(member)
        if target.id == member.id:
            raise VCError("You can't ban yourself.")
        if target.bot:
            raise VCError("You can't ban a bot.")
        self.guard(member, target, "ban")
        try:
            await self._ov(ch, target, f"VC ban by {member}", **BAN_DENY)
            if target.id not in rec["banned"]:
                rec["banned"].append(target.id)
                self.store.save_soon()
            if target in ch.members:
                await target.move_to(None, reason=f"VC ban by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        return f"Banned **{target.display_name}** from this VC."

    async def unban(self, member, target: discord.abc.User):
        ch, rec = self.owned(member)
        if target.id not in rec["banned"]:
            raise VCError("They aren't banned here.")
        reason = f"VC unban by {member}"
        try:
            m = ch.guild.get_member(target.id)
            if m is not None:
                await self._ov(ch, m, reason, **BAN_CLEAR)
            else:
                # not cached / left the server: remove the overwrite by id (set_permissions needs a Member or Role)
                try:
                    await self.bot.http.delete_channel_permissions(ch.id, target.id, reason=reason)
                except discord.NotFound:
                    pass
        except discord.HTTPException as e:
            raise self._http(e)
        if target.id in rec["banned"]:
            rec["banned"].remove(target.id)
        self.store.save_soon()
        return f"Unbanned **{getattr(target, 'display_name', None) or getattr(target, 'name', target.id)}**."

    async def permit(self, member, target: discord.Member):
        ch, rec = self.owned(member)
        try:
            await self._ov(ch, target, f"VC permit by {member}", **{**BAN_CLEAR, "connect": True, "view_channel": True})
        except discord.HTTPException as e:
            raise self._http(e)
        if target.id in rec["banned"]:
            rec["banned"].remove(target.id)
            self.store.save_soon()
        return f"**{target.display_name}** can join now."

    async def permit_all(self, member):
        ch, rec = self.owned(member)
        targets = [m for m in ch.members if m.id != rec["owner"]]
        if not targets:
            raise VCError("Nobody else is in the VC.")
        try:
            for m in targets:
                await self._ov(ch, m, f"VC permit all by {member}", **{**BAN_CLEAR, "connect": True, "view_channel": True})
                if m.id in rec["banned"]:
                    rec["banned"].remove(m.id)
                await asyncio.sleep(0.25)
        except discord.HTTPException as e:
            raise self._http(e)
        self.store.save_soon()
        return f"Permitted **{len(targets)}** member(s)."

    async def transfer(self, member, target: discord.Member):
        ch, rec = self.owned(member)
        if target.id == rec["owner"]:
            raise VCError("They already own this VC.")
        if target.bot:
            raise VCError("A bot can't own a VC.")
        if target not in ch.members:
            raise VCError("They're not in this VC.")
        await self.set_owner(ch, rec, target, old=ch.guild.get_member(rec["owner"]))
        return f"**{target.display_name}** is the new owner."

    async def set_owner(self, ch, rec, new: discord.Member, old=None):
        try:
            await ch.set_permissions(new, overwrite=discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, stream=True), reason="VC owner changed")
            if old is not None:
                await ch.set_permissions(old, overwrite=None, reason="VC owner changed")
        except discord.HTTPException as e:
            raise self._http(e)
        rec["owner"] = new.id
        if new.id in rec["banned"]:
            rec["banned"].remove(new.id)
        self.store.save_soon()

    # ---------- channel settings ----------
    async def limit(self, member, n: int):
        ch, _ = self.owned(member)
        if not 0 <= n <= 99:
            raise VCError("Limit must be 0-99 (0 = no limit).")
        try:
            await ch.edit(user_limit=n, reason=f"VC limit by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        return "Limit removed." if n == 0 else f"Limit set to **{n}**."

    async def rename(self, member, name: str):
        ch, _ = self.owned(member)
        if not name.strip():
            raise VCError("Give me a name.")
        # exactly what the owner typed - no automatic prefix. Discord shows custom / animated emojis in channel names as raw
        # "<:name:id>" text, so they are removed here; normal (unicode) emojis stay. Custom emojis work in the VC status.
        new, dropped = await emoji.sanitize(self.bot, member.guild, name, limit=100, keep_custom=False)
        if not new:
            raise VCError("Custom/animated emojis can't be shown in a VC **name** (Discord only allows normal emojis there), "
                          "so nothing is left. Add some text, or put the custom emoji in the VC status.")
        ra = ratelimit.check("rename", ch.id)
        if ra:
            raise VCError(f"Discord allows 2 renames per 10 minutes. Try again in **{ratelimit.fmt(ra)}**.")
        try:
            await ch.edit(name=new, reason=f"VC rename by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        note = " (custom emoji removed - Discord can't show them in VC names; use VC status for those)" if dropped else ""
        return f"Renamed to **{new}**.{note}"

    async def status(self, member, text: str):
        ch, _ = self.owned(member)
        # custom + animated emojis work here: <:name:id>, :name: or a plain emoji ID (only emojis the bot can use)
        text, _ = await emoji.sanitize(self.bot, member.guild, text.strip(), limit=500)
        try:
            await self.bot.http.request(
                Route("PUT", "/channels/{channel_id}/voice-status", channel_id=ch.id),
                json={"status": text or None},
            )
        except discord.Forbidden:
            raise VCError("I need the **Set Voice Channel Status** permission.")
        except discord.HTTPException as e:
            raise self._http(e)
        return f"Status set: **{text}**" if text else "Status cleared."

    async def bitrate(self, member, kbps: int):
        ch, _ = self.owned(member)
        top = ch.guild.bitrate_limit // 1000
        if not 8 <= kbps <= top:
            raise VCError(f"Bitrate must be 8-{int(top)} kbps here.")
        try:
            await ch.edit(bitrate=kbps * 1000, reason=f"VC bitrate by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        return f"Bitrate is now **{kbps} kbps**."

    async def mute(self, member, target: discord.Member, seconds: int):
        ch, _ = self.owned(member)
        if target.id == member.id:
            raise VCError("You can't mute yourself.")
        if target not in ch.members:
            raise VCError("They're not in this VC.")
        self.guard(member, target, "mute")
        try:
            await target.edit(mute=True, reason=f"VC mute by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        self.store.data["mutes"][f"{ch.guild.id}:{target.id}"] = discord.utils.utcnow().timestamp() + seconds
        self.store.save_soon()
        return f"Muted **{target.display_name}** for **{fmt_secs(seconds)}**."

    async def unmute(self, member, target: discord.Member):
        ch, _ = self.owned(member)
        if target not in ch.members:
            raise VCError("They're not in this VC.")
        try:
            await target.edit(mute=False, reason=f"VC unmute by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        self.store.data["mutes"].pop(f"{ch.guild.id}:{target.id}", None)
        self.store.save_soon()
        return f"Unmuted **{target.display_name}**."

    async def deafen(self, member, target: discord.Member, state: bool = True):
        ch, _ = self.owned(member)
        if target not in ch.members:
            raise VCError("They're not in this VC.")
        if state:
            if target.id == member.id:
                raise VCError("You can't deafen yourself.")
            self.guard(member, target, "deafen")
        try:
            await target.edit(deafen=state, reason=f"VC {'deafen' if state else 'undeafen'} by {member}")
        except discord.HTTPException as e:
            raise self._http(e)
        return f"{'Deafened' if state else 'Undeafened'} **{target.display_name}**."

    async def mute_all(self, member, state: bool = True):
        ch, rec = self.owned(member)
        targets = [m for m in ch.members if m.id != member.id and m.id != rec["owner"] and not m.bot
                   and (not state or not self.protected(m))]
        if not targets:
            raise VCError("Nobody else can be changed here.")
        try:
            for m in targets:
                await m.edit(mute=state, reason=f"VC {'mute' if state else 'unmute'} all by {member}")
                if not state:
                    self.store.data["mutes"].pop(f"{ch.guild.id}:{m.id}", None)
                await asyncio.sleep(0.3)
        except discord.HTTPException as e:
            raise self._http(e)
        self.store.save_soon()
        return f"{'Muted' if state else 'Unmuted'} **{len(targets)}** member(s)."

    async def ban_list(self, member) -> str:
        ch, rec = self.owned(member)
        if not rec["banned"]:
            return "Nobody is banned from this VC."
        return "Banned here: " + ", ".join(f"<@{u}>" for u in rec["banned"][:40])

    async def claim(self, member):
        """Take the VC when its owner is not inside any more."""
        vs = member.voice
        ch = vs.channel if vs and vs.channel else None
        rec = self.store.vc(ch.id) if ch else None
        if not rec:
            raise VCError("Join the custom VC you want to claim first.")
        if rec["owner"] == member.id:
            raise VCError("You already own this VC.")
        owner = ch.guild.get_member(rec["owner"])
        if owner is not None and owner in ch.members:
            raise VCError("The owner is still in this VC.")
        await self.set_owner(ch, rec, member, old=owner)
        return "You own this VC now."

    async def toggle_drags(self, member):
        _, rec = self.owned(member)
        rec["block_drags"] = not rec.get("block_drags", False)
        self.store.save_soon()
        return ("Drag requests are **blocked**." if rec["block_drags"]
                else "Drag requests are **allowed**.")

    # ---------- create / cleanup ----------
    async def create_for(self, member: discord.Member, create_ch: discord.VoiceChannel):
        if member.id in self._creating:
            return
        ra = ratelimit.check("create_vc", member.id)
        if ra:
            log.info("create_vc rate limited for %s", member)
            try:
                await member.move_to(None, reason="Creating VCs too fast")
            except discord.HTTPException:
                pass
            return
        self._creating.add(member.id)
        try:
            guild = member.guild
            cat = create_ch.category
            overwrites = dict(cat.overwrites) if cat else {}
            overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, stream=True)
            kwargs = dict(
                name=f"{VC_PREFIX}{member.name}"[:100],
                category=cat,
                user_limit=0,                        # no user limit on new custom VCs
                bitrate=min(create_ch.bitrate, guild.bitrate_limit),
                reason=f"Custom VC for {member}",
            )
            try:
                me_ov = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True, move_members=True,
                                                    manage_messages=True, send_messages=True)
                ch = await guild.create_voice_channel(overwrites={**overwrites, guild.me: me_ov}, **kwargs)
            except discord.Forbidden:
                try:
                    ch = await guild.create_voice_channel(overwrites=overwrites, **kwargs)
                except discord.HTTPException as e:
                    log.warning("could not create VC in %s: %s", guild, e)
                    return
            except discord.HTTPException as e:
                log.warning("could not create VC in %s: %s", guild, e)
                return
            self.store.add_vc(ch.id, guild.id, member.id)
            try:
                await member.move_to(ch)
            except discord.HTTPException:
                self.store.remove_vc(ch.id)
                try:
                    await ch.delete(reason="Owner left before being moved")
                except discord.HTTPException:
                    pass
        finally:
            self._creating.discard(member.id)

    async def on_leave(self, member, ch: discord.VoiceChannel, rec: dict):
        humans = [m for m in ch.members if not m.bot]
        if not humans:
            self.store.remove_vc(ch.id)
            try:
                await ch.delete(reason="Custom VC empty")
            except discord.HTTPException:
                pass
            return
        if member.id == rec["owner"]:
            new = humans[0]
            try:
                await self.set_owner(ch, rec, new, old=member)
                emb = discord.Embed(
                    description=f"{I.s('crown')} {new.mention} owns this VC now.", color=BLURPLE)
                await ch.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
            except (VCError, discord.HTTPException):
                pass


class VC(commands.Cog):
    """Custom VC commands (hybrid = work as $prefix AND /slash)."""

    def __init__(self, bot):
        self.bot = bot
        self.store = bot.store
        self.svc = bot.vc_service
        self._ready_done = False

    async def cog_load(self):
        self.mute_loop.start()

    async def cog_unload(self):
        self.mute_loop.cancel()

    # ---------- plumbing ----------
    async def _run(self, ctx: commands.Context, op, *args):
        await ctx.defer()
        if isinstance(ctx.channel, discord.VoiceChannel):      # Kon owners can type in a VC's chat to manage it
            CHAT_VC.set(ctx.channel)
        msg = await op(ctx.author, *args)
        await ctx.reply(ok(msg), mention_author=False)

    async def _who(self, ctx, user, as_user=False):
        """The member from the command - or the author of the message you replied to."""
        if user is None and ctx.interaction is None and ctx.message.reference:
            msg = await replied_message(ctx)
            if msg is not None:
                user = msg.author if as_user else ctx.guild.get_member(msg.author.id)
        if user is None:
            raise VCError("Mention someone or reply to their message.")
        return user

    # ---------- events ----------
    @commands.Cog.listener()
    async def on_ready(self):
        if self._ready_done:
            return
        self._ready_done = True
        for cid, rec in list(self.store.data["vcs"].items()):
            ch = self.bot.get_channel(int(cid))
            if ch is None:
                self.store.remove_vc(int(cid))
            elif not [m for m in ch.members if not m.bot]:
                self.store.remove_vc(int(cid))
                try:
                    await ch.delete(reason="Custom VC empty (startup cleanup)")
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or before.channel == after.channel:
            return
        if after.channel:
            g = self.store.guild(member.guild.id)
            if after.channel.id == g.get("create"):
                await self.svc.create_for(member, after.channel)
            else:
                rec = self.store.vc(after.channel.id)
                if rec and member.id in rec["banned"] and member.id != rec["owner"]:
                    try:
                        await member.move_to(None, reason="Banned from this custom VC")
                    except discord.HTTPException:
                        pass
        if before.channel:
            rec = self.store.vc(before.channel.id)
            if rec:
                await self.svc.on_leave(member, before.channel, rec)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """A VC-banned member can't chat in that VC's text chat.
        Channel overwrites already deny it, but Administrators ignore overwrites - so their messages are deleted here."""
        if message.guild is None or message.author.bot or not isinstance(message.channel, discord.VoiceChannel):
            return
        rec = self.store.vc(message.channel.id)
        if not rec or message.author.id not in rec["banned"] or message.author.id == rec["owner"]:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        self.store.remove_vc(channel.id)
        g = self.store.data["guilds"].get(str(channel.guild.id), {})
        changed = False
        for k in ("create", "interface"):
            if g.get(k) == channel.id:
                g.pop(k, None)
                if k == "interface":
                    g.pop("panel_msg", None)
                changed = True
        if changed:
            self.store.save_soon()

    @tasks.loop(seconds=15)
    async def mute_loop(self):
        now = discord.utils.utcnow().timestamp()
        for key, exp in list(self.store.data["mutes"].items()):
            if exp > now:
                continue
            self.store.data["mutes"].pop(key, None)
            self.store.save_soon()
            gid, uid = map(int, key.split(":"))
            guild = self.bot.get_guild(gid)
            m = guild.get_member(uid) if guild else None
            if m and m.voice and m.voice.mute:
                try:
                    await m.edit(mute=False, reason="VC mute expired")
                except discord.HTTPException:
                    pass

    @mute_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    # ---------- info ----------
    @commands.hybrid_command(name="vcinfo", aliases=['vci'], description="View the current owner and details of this voice channel")
    @commands.guild_only()
    async def vcinfo(self, ctx: commands.Context):
        ch = None
        v = ctx.author.voice
        if v and v.channel and self.store.vc(v.channel.id):
            ch = v.channel
        elif isinstance(ctx.channel, discord.VoiceChannel) and self.store.vc(ctx.channel.id):
            ch = ctx.channel
        if ch is None:
            raise VCError("This isn't a custom VC.")
        rec = self.store.vc(ch.id)
        owner = ctx.guild.get_member(rec["owner"])
        owner_name = owner.name if owner else "Unknown"
        embed = discord.Embed(
            description=(
                f"{I.s('speaker')} **Channel:** `{ch.name}`\n"
                f"{I.s('crown')} **Owner:** {owner_name} (`{rec['owner']}`)\n"
                f"{I.s('users')} **Members:** `{len(ch.members)}`"
            ),
            color=BLURPLE,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Voice Channel Information",
                         icon_url=(ctx.guild.icon.url if ctx.guild.icon else self.bot.user.display_avatar.url))
        if owner:
            embed.set_thumbnail(url=owner.display_avatar.url)
        await ctx.reply(embed=embed, mention_author=False)

    # ---------- lock / hide ----------
    @commands.hybrid_command(name="vclock", aliases=['lock', 'vclk'], description="Lock session")
    @commands.guild_only()
    async def vclock(self, ctx):
        await self._run(ctx, self.svc.lock)

    @commands.hybrid_command(name="vcunlock", aliases=['unlock', 'vcul'], description="Unlock session")
    @commands.guild_only()
    async def vcunlock(self, ctx):
        await self._run(ctx, self.svc.unlock)

    @commands.hybrid_command(name="vchide", aliases=['hide'], description="Hide session")
    @commands.guild_only()
    async def vchide(self, ctx):
        await self._run(ctx, self.svc.hide)

    @commands.hybrid_command(name="vcunhide", aliases=['unhide', 'vcshow'], description="Unhide session")
    @commands.guild_only()
    async def vcunhide(self, ctx):
        await self._run(ctx, self.svc.unhide)

    # ---------- member actions ----------
    @commands.hybrid_command(name="vckick", aliases=['kick', 'vck'], description="Kick user")
    @app_commands.describe(user="Member to kick from your VC")
    @commands.guild_only()
    async def vckick(self, ctx, user: Optional[discord.Member] = None):
        await self._run(ctx, self.svc.kick, await self._who(ctx, user, False))

    @commands.hybrid_command(name="vcban", aliases=['vcb'], description="Ban user")
    @app_commands.describe(user="Member to ban from your VC")
    @commands.guild_only()
    async def vcban(self, ctx, user: Optional[discord.Member] = None):
        await self._run(ctx, self.svc.ban, await self._who(ctx, user, False))

    @commands.hybrid_command(name="vcunban", aliases=['vcub'], description="Unban user")
    @app_commands.describe(user="User to unban from your VC")
    @commands.guild_only()
    async def vcunban(self, ctx, user: Optional[discord.User] = None):
        await self._run(ctx, self.svc.unban, await self._who(ctx, user, True))

    @commands.hybrid_command(name="vcpermit", aliases=['permit', 'allow', 'vcp'], description="Permit user")
    @app_commands.describe(user="Member allowed to join your locked/hidden VC")
    @commands.guild_only()
    async def vcpermit(self, ctx, user: Optional[discord.Member] = None):
        await self._run(ctx, self.svc.permit, await self._who(ctx, user, False))

    @commands.hybrid_command(name="vcpermitall", aliases=['permitall', 'vcpa'], description="Permit all current users in your custom VC")
    @commands.guild_only()
    async def vcpermitall(self, ctx):
        await self._run(ctx, self.svc.permit_all)

    @commands.hybrid_command(name="vctransfer", aliases=['vct'], description="Transfer ownership")
    @app_commands.describe(user="New owner (must be in your VC)")
    @commands.guild_only()
    async def vctransfer(self, ctx, user: Optional[discord.Member] = None):
        if user is None and ctx.interaction is None and ctx.message.reference:      # "$vctransfer" as a reply
            ref = ctx.message.reference
            msg = ref.resolved if isinstance(ref.resolved, discord.Message) else None
            if msg is None and ref.message_id:
                try:
                    msg = await ctx.channel.fetch_message(ref.message_id)
                except discord.HTTPException:
                    msg = None
            if msg is not None:
                user = ctx.guild.get_member(msg.author.id)
        if user is None:
            raise VCError("Mention someone or reply to them: `$vctransfer @user`.")
        await self._run(ctx, self.svc.transfer, user)

    @commands.hybrid_command(name="vcmute", aliases=['vcm'], description="Server-mute user for a duration")
    @app_commands.describe(user="Member to mute", duration="e.g. 30s, 10m, 2h (max 24h)")
    @commands.guild_only()
    async def vcmute(self, ctx, user: Optional[discord.Member] = None, duration: str = "10m"):
        await self._run(ctx, self.svc.mute, await self._who(ctx, user), parse_duration(duration))

    @commands.hybrid_command(name="vcunmute", aliases=['vcum'], description="Unmute user")
    @app_commands.describe(user="Member to unmute")
    @commands.guild_only()
    async def vcunmute(self, ctx, user: Optional[discord.Member] = None):
        await self._run(ctx, self.svc.unmute, await self._who(ctx, user))

    @commands.hybrid_command(name="vcdeafen", aliases=['vcdeaf'], description="Server-deafen user")
    @app_commands.describe(user="Member to deafen")
    @commands.guild_only()
    async def vcdeafen(self, ctx, user: Optional[discord.Member] = None):
        await self._run(ctx, self.svc.deafen, await self._who(ctx, user), True)

    @commands.hybrid_command(name="vcundeafen", aliases=['vcundeaf'], description="Undeafen user")
    @app_commands.describe(user="Member to undeafen")
    @commands.guild_only()
    async def vcundeafen(self, ctx, user: Optional[discord.Member] = None):
        await self._run(ctx, self.svc.deafen, await self._who(ctx, user), False)

    @commands.hybrid_command(name="vcmuteall", aliases=['vcma'], description="Mute everyone in your VC (except you)")
    @commands.guild_only()
    async def vcmuteall(self, ctx):
        await self._run(ctx, self.svc.mute_all, True)

    @commands.hybrid_command(name="vcunmuteall", aliases=['vcuma'], description="Unmute everyone in your VC")
    @commands.guild_only()
    async def vcunmuteall(self, ctx):
        await self._run(ctx, self.svc.mute_all, False)

    @commands.hybrid_command(name="vcbans", aliases=['vcbanlist'], description="Who is banned from your VC")
    @commands.guild_only()
    async def vcbans(self, ctx):
        await self._run(ctx, self.svc.ban_list)

    @commands.hybrid_command(name="vcclaim", aliases=['claim'], description="Claim the VC when its owner left")
    @commands.guild_only()
    async def vcclaim(self, ctx):
        await self._run(ctx, self.svc.claim)

    # ---------- settings ----------
    @commands.hybrid_command(name="vclimit", aliases=['limit', 'vclim'], description="Set capacity")
    @app_commands.describe(limit="0 = unlimited, max 99")
    @commands.guild_only()
    async def vclimit(self, ctx, limit: commands.Range[int, 0, 99]):
        await self._run(ctx, self.svc.limit, limit)

    @commands.hybrid_command(name="vcrename", aliases=['rename', 'vcn', 'vcname'], description="Update VC name")
    @app_commands.describe(name="New channel name")
    @commands.guild_only()
    async def vcrename(self, ctx, *, name: str):
        await self._run(ctx, self.svc.rename, name)

    @commands.hybrid_command(name="vcstatus", aliases=['vcs'], description="Set voice status")
    @app_commands.describe(text="Status text (leave empty to clear)")
    @commands.guild_only()
    async def vcstatus(self, ctx, *, text: str = ""):
        await self._run(ctx, self.svc.status, text)

    @commands.hybrid_command(name="vcbitrate", aliases=['bitrate', 'vcbr'], description="Change quality")
    @app_commands.describe(kbps="Bitrate in kbps (8 - server max)")
    @commands.guild_only()
    async def vcbitrate(self, ctx, kbps: commands.Range[int, 8, 384]):
        await self._run(ctx, self.svc.bitrate, kbps)

    @commands.hybrid_command(name="denydrags", aliases=['denydrag', 'dd'], description="VC Owner: Toggle privacy to block drag requests for your VC")
    @commands.guild_only()
    async def denydrags(self, ctx):
        await self._run(ctx, self.svc.toggle_drags)


async def setup(bot):
    await bot.add_cog(VC(bot))
