"""Kon multiplayer party games: Guess the Anime, Guess the Emoji and Simon Says."""
import asyncio
import random
import time

import discord
from discord.ext import commands

from core.config import BRAND
from core.icons import I

ANIME = {
    "easy": [
        ("A boy with a straw hat wants to become the Pirate King.", "one piece", "A pirate crew sails the Grand Line."),
        ("A ninja dreams of becoming Hokage.", "naruto", "The main character wears orange and has a fox sealed inside him."),
        ("A boy becomes a demon slayer after his family is attacked.", "demon slayer", "The main character carries his sister in a box."),
        ("A young Saiyan protects Earth and loves fighting.", "dragon ball", "The hero's name is Goku."),
        ("A boy with a powerful quirk joins a school for heroes.", "my hero academia", "The main character is Deku."),
        ("A young alchemist searches for a way to restore his brother's body.", "fullmetal alchemist", "The brothers are Edward and Alphonse."),
        ("A student gets pulled into a world of Pokémon battles.", "pokemon", "Ash travels with Pikachu."),
        ("A chef dreams of becoming the best in a world of food battles.", "food wars", "The school is Totsuki."),
        ("A boy joins a volleyball team and aims for the top.", "haikyuu", "The team is Karasuno."),
        ("A young detective solves cases with a childlike body.", "detective conan", "The detective's real name is Shinichi Kudo."),
    ],
    "medium": [
        ("A notebook lets its owner kill anyone whose name is written in it.", "death note", "Light Yagami finds a notebook dropped by a Shinigami."),
        ("Humanity fights giant humanoid creatures beyond the walls.", "attack on titan", "Eren, Mikasa and Armin live inside the walls."),
        ("A boy enters a virtual game world where death in the game is real.", "sword art online", "Kirito and Asuna are central characters."),
        ("A young man hunts demons after joining a secret organisation.", "blue exorcist", "The hero is Rin Okumura."),
        ("A boy with no magic wants to become the Wizard King.", "black clover", "The main character is Asta."),
        ("A group of students investigate supernatural mysteries at school.", "mob psycho 100", "The main character is Shigeo Kageyama."),
        ("A spy, an assassin and a telepath form a pretend family.", "spy x family", "The family includes Loid, Yor and Anya."),
        ("A boy enters a world where he can see people's lifespan above their heads.", "platinum end", "The creators also made Death Note."),
        ("A group of students use alchemy to search for the Philosopher's Stone.", "fullmetal alchemist", "The brothers are Edward and Alphonse Elric."),
        ("A boy swallows a special object and shares his body with a curse.", "jujutsu kaisen", "The curse is Ryomen Sukuna."),
    ],
    "hard": [
        ("A salaryman is reborn as a slime and builds a monster nation.", "that time i got reincarnated as a slime", "Rimuru Tempest is the main character."),
        ("A quiet high-school student is pulled into a deadly game involving a mysterious app.", "darwins game", "Kaname Sudo is the protagonist."),
        ("A former assassin runs a small shop while trying to live peacefully.", "sakamoto days", "The shopkeeper is Taro Sakamoto."),
        ("A boy is trapped in a time loop after entering a fantasy world.", "re zero", "The protagonist is Subaru Natsuki."),
        ("A former hero is reincarnated and tries to live without regrets.", "mushoku tensei", "The protagonist is Rudeus Greyrat."),
        ("A legendary swordsman is reborn in a world where magic has changed.", "the misfit of demon king academy", "The demon king is Anos Voldigoad."),
        ("A boy becomes a professional shogi player while dealing with loneliness.", "march comes in like a lion", "The protagonist is Rei Kiriyama."),
        ("A young man seeks revenge after being betrayed in a fantasy world.", "arifureta", "The protagonist is Hajime Nagumo."),
        ("A girl joins a secret group that investigates strange phenomena.", "monogatari series", "The series features Koyomi Araragi."),
        ("A young man gains a strange ability to control time after a tragedy.", "charlotte", "The protagonist is Yuu Otosaka."),
    ],
}
EMOJI = {
    "easy": [
        ("🥞", "pancake", "PAN + CAKE"), ("🌈", "rainbow", "RAIN + BOW"),
        ("🦋", "butterfly", "BUTTER + FLY"), ("🌻", "sunflower", "SUN + FLOWER"),
        ("⭐🐟", "starfish", "STAR + FISH"), ("🐉🔥", "dragonfire", "DRAGON + FIRE"),
        ("🍯🌙", "honeymoon", "HONEY + MOON"), ("🌽🐶", "corn dog", "CORN + DOG"),
        ("🌞👓", "sunglasses", "SUN + GLASSES"), ("❄️👨", "snowman", "SNOW + MAN"),
    ],
    "medium": [
        ("🌙💡", "moonlight", "MOON + LIGHT"), ("🧊🍦", "ice cream", "ICE + CREAM"),
        ("🐝🍯", "honeybee", "HONEY + BEE"), ("🌊🐚", "seashell", "SEA + SHELL"),
        ("🌟💥", "starburst", "STAR + BURST"), ("🛏️🪲", "bedbug", "BED + BUG"),
        ("📚🐛", "bookworm", "BOOK + WORM"), ("🦶⚽", "football", "FOOT + BALL"),
        ("🕷️🕸️", "spiderweb", "SPIDER + WEB"), ("🌍🐛", "earthworm", "EARTH + WORM"),
    ],
    "hard": [
        ("🔥🪰", "firefly", "FIRE + FLY"), ("🧠⛈️", "brainstorm", "BRAIN + STORM"),
        ("🦷🧚", "tooth fairy", "TOOTH + FAIRY"), ("🧊🕰️", "ice age", "ICE + AGE"),
        ("🌧️✅", "rain check", "RAIN + CHECK"), ("🌱🏠", "greenhouse", "GREEN + HOUSE"),
        ("🧛🦇", "vampire bat", "VAMPIRE + BAT"), ("🌊🐴", "seahorse", "SEA + HORSE"),
        ("🧈🪰", "butterfly", "BUTTER + FLY"), ("🧠🌊", "brainwave", "BRAIN + WAVE"),
    ],
}
DIFF_POINTS = {"easy": 100, "medium": 150, "hard": 200}
COLORS = [
    ("Red", "🔴", discord.ButtonStyle.danger),
    ("Blue", "🔵", discord.ButtonStyle.primary),
    ("Green", "🟢", discord.ButtonStyle.success),
    ("Yellow", "🟡", discord.ButtonStyle.secondary),
]


def clean(s):
    return " ".join((s or "").lower().strip().replace("-", " ").split())


class PartyLobby(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=300)
        self.game = game

    def host(self, user):
        return user.id == self.game.host.id or user.guild_permissions.manage_guild

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="➕")
    async def join(self, i, _):
        g = self.game
        if i.user.id in g.players:
            return await i.response.send_message("You're already in the lobby.", ephemeral=True)
        if len(g.players) >= 10:
            return await i.response.send_message("This lobby is full (10 players).", ephemeral=True)
        g.players.append(i.user.id)
        await i.response.edit_message(embed=g.lobby_embed(), view=self)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, emoji="↩️")
    async def leave(self, i, _):
        g = self.game
        if i.user.id == g.host.id:
            return await i.response.send_message("The host can't leave. Use Cancel to close the lobby.", ephemeral=True)
        if i.user.id not in g.players:
            return await i.response.send_message("You're not in this lobby.", ephemeral=True)
        g.players.remove(i.user.id)
        await i.response.edit_message(embed=g.lobby_embed(), view=self)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, emoji="▶️")
    async def start(self, i, _):
        if not self.host(i.user):
            return await i.response.send_message("Only the host can start the game.", ephemeral=True)
        if len(self.game.players) < 2:
            return await i.response.send_message("At least 2 players are needed.", ephemeral=True)
        self.game.started.set()
        await i.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️")
    async def cancel(self, i, _):
        if not self.host(i.user):
            return await i.response.send_message("Only the host can cancel.", ephemeral=True)
        self.game.cancelled = True
        self.game.started.set()
        await i.response.defer()


class PuzzleView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    @discord.ui.button(label="Hint", style=discord.ButtonStyle.secondary, emoji="💡")
    async def hint(self, i, _):
        g = self.game
        if i.user.id not in g.players:
            return await i.response.send_message("You're spectating this match.", ephemeral=True)
        if g.current is None:
            return await i.response.send_message("There isn't an active question.", ephemeral=True)
        hint = g.current[2]
        await i.response.send_message(f"Hint: **{hint}**", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip(self, i, _):
        g = self.game
        if i.user.id != g.host.id and not i.user.guild_permissions.manage_guild:
            return await i.response.send_message("Only the host can skip a question.", ephemeral=True)
        g.skipped = True
        g.answer_event.set()
        await i.response.defer()


class PartyGame:
    def __init__(self, cog, ctx, kind, difficulty, rounds):
        self.cog, self.ctx, self.kind = cog, ctx, kind
        self.host = ctx.author
        self.difficulty = difficulty
        self.rounds = rounds
        self.players = [ctx.author.id]
        self.scores = {ctx.author.id: 0}
        self.started = asyncio.Event()
        self.cancelled = False
        self.answer_event = asyncio.Event()
        self.current = None
        self.winner = None
        self.view = None
        self.message = None
        self.question_started = 0
        self.skipped = False

    @property
    def title(self):
        return "Guess the Anime" if self.kind == "anime" else "Guess the Emoji"

    def lobby_embed(self):
        e = discord.Embed(
            title=f"{I.s('gamepad')} {self.title}",
            description=("Join the lobby, then the host starts the match.\n\n"
                         f"**Players:** {len(self.players)}/10\n"
                         f"**Rounds:** {self.rounds}\n"
                         f"**Difficulty:** {self.difficulty.title()}\n"
                         "**Timer:** 15 seconds per question\n"
                         "**Scoring:** Faster correct answers earn more points."),
            color=BRAND,
        )
        e.add_field(name="Players", value="\n".join(f"{n}. <@{uid}>" for n, uid in enumerate(self.players, 1)))
        e.set_footer(text="2–10 players · 10–20 rounds · First correct answer wins the round")
        return e

    def scoreboard(self):
        rows = sorted(self.scores.items(), key=lambda x: (-x[1], x[0]))
        return "\n".join(f"**{n}.** <@{uid}> — **{score}** pts" for n, (uid, score) in enumerate(rows, 1))

    async def run(self):
        self.view = PartyLobby(self)
        self.message = await self.ctx.reply(embed=self.lobby_embed(), view=self.view, mention_author=False)
        await self.started.wait()
        if self.cancelled:
            await self.message.edit(content="Lobby cancelled.", embed=None, view=None)
            return
        self.view.stop()
        await self.play()

    async def play(self):
        pool = list(ANIME[self.difficulty] if self.kind == "anime" else EMOJI[self.difficulty])
        random.shuffle(pool)
        pool = (pool * ((self.rounds + len(pool) - 1) // len(pool)))[:self.rounds]
        self.view = PuzzleView(self)
        for rnd, q in enumerate(pool, 1):
            self.current = q
            self.winner = None
            self.skipped = False
            self.answer_event = asyncio.Event()
            self.question_started = time.monotonic()
            prompt = q[0]
            e = discord.Embed(
                title=f"{I.s('gamepad')} {self.title} · Round {rnd}/{self.rounds}",
                description=(f"**{prompt}**\n\n" if self.kind == "emoji" else f"**{prompt}**\n\n")
                            + "Type your answer in chat. You have **15 seconds**.",
                color=BRAND,
            )
            e.add_field(name="Difficulty", value=self.difficulty.title(), inline=True)
            e.add_field(name="Points", value=f"{DIFF_POINTS[self.difficulty]} max", inline=True)
            e.add_field(name="Live scoreboard", value=self.scoreboard()[:1000], inline=False)
            await self.message.edit(embed=e, view=self.view)
            try:
                await asyncio.wait_for(self.answer_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
            result = ""
            if self.winner:
                uid, points, elapsed = self.winner
                result = f"Correct: <@{uid}> earned **{points}** points in **{elapsed:.1f}s**."
            else:
                prefix = "Question skipped." if self.skipped else "Time's up!"
                result = f"{prefix} Answer: **{q[1].title()}**."
            e.description = f"**{prompt}**\n\n{result}\n\n**Live scoreboard**\n{self.scoreboard()[:850]}"
            await self.message.edit(embed=e, view=None)
            await asyncio.sleep(2)
        self.current = None
        rows = sorted(self.scores.items(), key=lambda x: (-x[1], x[0]))
        top = rows[0][1] if rows else 0
        winners = [uid for uid, score in rows if score == top]
        title = "It's a tie!" if len(winners) > 1 else "Match winner"
        desc = (", ".join(f"<@{u}>" for u in winners) if winners else "No winner")
        e = discord.Embed(title=f"🏆 {title}", description=f"{desc}\n\n**Final scoreboard**\n{self.scoreboard()}", color=BRAND)
        e.set_footer(text=f"{self.title} · {self.rounds} rounds")
        await self.message.edit(embed=e, view=None)
        self.cog.active.pop(self.ctx.channel.id, None)

    async def accept(self, message):
        if self.current is None or message.author.bot or message.author.id not in self.players:
            return
        if self.winner:
            return
        if clean(message.content) == clean(self.current[1]):
            elapsed = min(15, time.monotonic() - self.question_started)
            points = max(10, round(DIFF_POINTS[self.difficulty] * (1 - elapsed / 20)))
            self.scores[message.author.id] = self.scores.get(message.author.id, 0) + points
            self.winner = (message.author.id, points, elapsed)
            self.answer_event.set()
            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass


class SimonView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game
        self.set_buttons()

    def set_buttons(self):
        for label, emoji, style in COLORS:
            b = discord.ui.Button(label=label, emoji=emoji, style=style, disabled=True)
            async def callback(i, name=label):
                await self.game.tap(i, name)
            b.callback = callback
            self.add_item(b)

    def enable(self, enabled=True):
        for item in self.children:
            item.disabled = not enabled


class SimonReplayView(discord.ui.View):
    def __init__(self, cog, ctx):
        super().__init__(timeout=300)
        self.cog, self.ctx = cog, ctx

    @discord.ui.button(label="Play Again", style=discord.ButtonStyle.success, emoji="🔁")
    async def replay(self, interaction, _):
        if interaction.user.id not in (self.ctx.author.id,) and not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Ask the host to start another match.", ephemeral=True)
        if self.ctx.channel.id in self.cog.active:
            return await interaction.response.send_message("A game is already running here.", ephemeral=True)
        await interaction.response.defer()
        game = SimonGame(self.cog, self.ctx)
        self.cog.active[self.ctx.channel.id] = game
        asyncio.create_task(self.cog._run_simon(game))


class SimonGame:
    def __init__(self, cog, ctx):
        self.cog, self.ctx = cog, ctx
        self.host = ctx.author
        self.players = [ctx.author.id]
        self.alive = set()
        self.scores = {}
        self.started = asyncio.Event()
        self.cancelled = False
        self.sequence = []
        self.progress = {}
        self.view = None
        self.message = None
        self.round = 0
        self.lock = asyncio.Lock()
        self.finished = False

    def lobby_embed(self):
        e = discord.Embed(title="🎨 Simon Says", description=(
            "Remember the colour sequence and repeat it in the correct order.\n\n"
            f"**Players:** {len(self.players)}/10\n**Rounds:** Sequence grows by one colour each round\n"
            "**How to play:** Watch the sequence, then tap the colour buttons in order.\n"
            "A wrong tap or timeout eliminates you. Last player standing wins."),
            color=BRAND)
        e.add_field(name="Players", value="\n".join(f"{n}. <@{u}>" for n, u in enumerate(self.players, 1)))
        e.set_footer(text="2–10 players · Host starts manually")
        return e

    async def lobby(self):
        view = PartyLobby(self)
        self.message = await self.ctx.reply(embed=self.lobby_embed(), view=view, mention_author=False)
        await self.started.wait()
        if self.cancelled:
            await self.message.edit(content="Lobby cancelled.", embed=None, view=None)
            return
        await self.play()

    async def play(self):
        self.alive = set(self.players)
        self.scores = {u: 0 for u in self.players}
        for rnd in range(1, 31):
            if len(self.alive) <= 1:
                break
            self.round = rnd
            self.sequence.append(random.choice(COLORS)[0])
            self.progress = {u: 0 for u in self.alive}
            self.finished = False
            self._round_done.clear()
            self.view = SimonView(self)
            e = discord.Embed(title=f"🎨 Simon Says · Level {rnd}",
                              description=("**Memorise this sequence:**\n"
                                           + "  →  ".join(next(c[1] for c in COLORS if c[0] == x) for x in self.sequence)
                                           + "\n\nNow repeat it using the buttons below. You have **20 seconds**."),
                              color=BRAND)
            e.add_field(name="Players left", value=", ".join(f"<@{u}>" for u in self.alive)[:1000])
            await self.message.edit(embed=e, view=self.view)
            await asyncio.sleep(min(4 + len(self.sequence) * 0.35, 10))
            e.description = f"**Level {rnd}**\nRepeat the sequence using the buttons below."
            self.view.enable(True)
            await self.message.edit(embed=e, view=self.view)
            try:
                await asyncio.wait_for(self._round_done.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass
            failed = [u for u in self.alive if self.progress.get(u, 0) < len(self.sequence)]
            self.alive.difference_update(failed)
            for u in self.alive:
                self.scores[u] = rnd
            e.description = (f"**Level {rnd} complete.**\n"
                            f"Still in: {', '.join(f'<@{u}>' for u in self.alive) or 'Nobody'}\n"
                            f"Eliminated: {', '.join(f'<@{u}>' for u in failed) or 'Nobody'}")
            await self.message.edit(embed=e, view=None)
            await asyncio.sleep(2)
        winners = list(self.alive)
        try:
            records = self.cog.bot.db.get_settings(self.ctx.guild.id, "simon_records")
            if not isinstance(records, dict):
                records = {}
            for uid, level in self.scores.items():
                records[str(uid)] = max(int(records.get(str(uid), 0)), int(level))
            self.cog.bot.db.set_settings(self.ctx.guild.id, "simon_records", records)
            top_records = sorted(records.items(), key=lambda item: int(item[1]), reverse=True)[:10]
            record_text = "\\n".join(f"<@{uid}> — Level **{level}**" for uid, level in top_records)
        except Exception:
            record_text = "Records unavailable."
        if len(winners) == 1:
            result = f"Winner: <@{winners[0]}>"
        elif winners:
            result = "It's a tie: " + ", ".join(f"<@{u}>" for u in winners)
        else:
            result = "No one completed the final level."
        result += f"\\n\\n**Highest level:** {max(self.scores.values(), default=0)}"
        e = discord.Embed(title="🏆 Simon Says · Results", description=result, color=BRAND)
        e.add_field(name="Personal bests", value=record_text[:1000] or "No records yet.", inline=False)
        await self.message.edit(embed=e, view=SimonReplayView(self.cog, self.ctx),)
        self.cog.active.pop(self.ctx.channel.id, None)

    async def tap(self, interaction, color):
        uid = interaction.user.id
        async with self.lock:
            if uid not in self.alive or self.finished:
                return await interaction.response.send_message("You're not active in this round.", ephemeral=True)
            idx = self.progress.get(uid, 0)
            if idx >= len(self.sequence):
                return await interaction.response.send_message("You've completed this sequence.", ephemeral=True)
            if self.sequence[idx] != color:
                self.alive.discard(uid)
                if not self.alive or all(self.progress.get(u, 0) >= len(self.sequence) for u in self.alive):
                    self.finished = True
                    self._round_done.set()
                return await interaction.response.send_message("Wrong colour — you're out.", ephemeral=True)
            self.progress[uid] = idx + 1
            if all(self.progress.get(u, 0) >= len(self.sequence) for u in self.alive):
                self.finished = True
                self._round_done.set()
            await interaction.response.send_message(f"Correct · {self.progress[uid]}/{len(self.sequence)}", ephemeral=True)

    @property
    def _round_done(self):
        if not hasattr(self, "_done_event"):
            self._done_event = asyncio.Event()
        return self._done_event


class PartyGames(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.active = {}

    async def open_puzzle(self, ctx, kind, difficulty="medium", rounds=10):
        if ctx.channel.id in self.active:
            return await ctx.reply("A game is already running in this channel.", mention_author=False)
        if difficulty not in ("easy", "medium", "hard"):
            return await ctx.reply("Choose easy, medium or hard.", mention_author=False)
        if kind == "anime":
            rounds = 10
        game = PartyGame(self, ctx, kind, difficulty, max(10, min(20, rounds)))
        self.active[ctx.channel.id] = game
        try:
            await game.run()
        finally:
            self.active.pop(ctx.channel.id, None)

    @commands.command(name="guessanime", aliases=["animequiz"])
    @commands.guild_only()
    async def guessanime(self, ctx, difficulty: str = "medium"):
        """$guessanime [easy|medium|hard] - multiplayer anime quiz."""
        await self.open_puzzle(ctx, "anime", difficulty, 10)

    @commands.command(name="guessemoji", aliases=["emojiquiz"])
    @commands.guild_only()
    async def guessemoji(self, ctx, difficulty: str = "medium", rounds: int = 10):
        """$guessemoji [easy|medium|hard] [10-20 rounds] - multiplayer emoji word puzzle."""
        await self.open_puzzle(ctx, "emoji", difficulty, rounds)

    async def _run_simon(self, game):
        try:
            await game.lobby()
        finally:
            self.active.pop(game.ctx.channel.id, None)

    @commands.command(name="simonsays", aliases=["simon"])
    @commands.guild_only()
    async def simonsays(self, ctx):
        """$simonsays - multiplayer colour-sequence memory game."""
        if ctx.channel.id in self.active:
            return await ctx.reply("A game is already running in this channel.", mention_author=False)
        game = SimonGame(self, ctx)
        self.active[ctx.channel.id] = game
        await self._run_simon(game)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.guild and not message.author.bot:
            game = self.active.get(message.channel.id)
            if isinstance(game, PartyGame):
                await game.accept(message)


async def setup(bot):
    await bot.add_cog(PartyGames(bot))
