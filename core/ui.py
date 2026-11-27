import re

import discord
from discord.ext import commands

from .errors import VCError
from .icons import I, err


def parse_int(text: str, lo: int, hi: int, name: str) -> int:
    try:
        n = int(text.strip())
    except ValueError:
        raise VCError(f"**{name}** must be a number.")
    if not lo <= n <= hi:
        raise VCError(f"**{name}** must be {lo}-{hi}.")
    return n


def to_partial(text):
    """'<:name:id>' / unicode / our icon name -> PartialEmoji (or None)."""
    if not text:
        return None
    text = text.strip()
    if text in I.map:
        return discord.PartialEmoji(name=I.map[text].name, id=I.map[text].id, animated=I.map[text].animated)
    try:
        return discord.PartialEmoji.from_str(text)
    except Exception:
        return None


def clean_emoji(text: str):
    """Value we store: custom emoji string, unicode emoji, or None."""
    text = (text or "").strip()
    if not text:
        return None
    if text in I.map:
        return str(I.map[text])
    pe = to_partial(text)
    if pe is None:
        raise VCError("That emoji doesn't work. Use a normal one or `<:name:123456>`.")
    return str(pe)


class FieldsModal(discord.ui.Modal):
    """fields: list of (label, default, max_length, required)"""

    def __init__(self, title, fields, callback):
        super().__init__(title=title, timeout=300)
        self._cb = callback
        self._inputs = []
        for label, default, max_len, required in fields:
            ti = discord.ui.TextInput(label=label, default=default or None, max_length=max_len, required=required)
            self._inputs.append(ti)
            self.add_item(ti)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await self._cb(interaction, [i.value.strip() for i in self._inputs])
        except VCError as e:
            text = err(str(e))
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)


class OwnedView(discord.ui.View):
    """View only the invoking user can use."""

    def __init__(self, invoker_id: int, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(err("That menu isn't yours."), ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


async def replied_message(ctx):
    """The message `ctx.message` is a reply to (or None)."""
    ref = ctx.message.reference
    if ref is None:
        return None
    if isinstance(ref.resolved, discord.Message):
        return ref.resolved
    if ref.message_id:
        try:
            return await ctx.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None
    return None


_ID = re.compile(r"^<@!?(\d{15,22})>$|^(\d{15,22})$")


class UserRef:
    """Somebody picked by @mention, name or plain user ID - they do not have to be in the server any more."""

    def __init__(self, uid: int, member=None):
        self.id, self.member = uid, member

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    @property
    def label(self) -> str:
        return self.member.display_name if self.member else f"`{self.id}`"


class MemberOrID(commands.Converter):
    """`@member`, a name, or a raw user ID (also for people who already left)."""

    async def convert(self, ctx, argument: str) -> UserRef:
        try:
            m = await commands.MemberConverter().convert(ctx, argument)
            return UserRef(m.id, m)
        except commands.MemberNotFound:
            hit = _ID.match(argument.strip())
            if hit:
                return UserRef(int(hit.group(1) or hit.group(2)))
            raise
