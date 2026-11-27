"""Kon names - the friendly new name of every command.

Nothing is removed: the new name is added as one more alias of the command, so the OLD name keeps working in chat,
in saved aliases, in `$disablecommand` rules and in the dashboard. `$help` shows the new name first and says
"was: $vclock" so nobody gets lost. Slash commands (/vclock ...) keep their names (so no re-sync is needed).

Rules for a new name: short, a plain word, says what it does, and does not clash with anything already there.
"""
from typing import Optional

# old name  ->  new name (only top-level commands / groups; sub commands keep theirs)
NEW_NAMES = {
    # ---- your voice room
    "vcinfo": "room", "vclock": "lock", "vcunlock": "unlock", "vchide": "hide", "vcunhide": "reveal",
    "vckick": "boot", "vcban": "banish", "vcunban": "pardon", "vcpermit": "allow", "vcpermitall": "allowall",
    "vctransfer": "handover", "vcmute": "hush", "vcunmute": "unhush", "vcdeafen": "deaf", "vcundeafen": "undeaf",
    "vcmuteall": "hushall", "vcunmuteall": "unhushall", "vcbans": "banlist", "vcclaim": "claim", "vclimit": "limit",
    "vcrename": "rename", "vcstatus": "topic", "vcbitrate": "quality", "denydrags": "nodrag", "dragme": "pullme",
    # ---- general
    "konping": "pulse", "koninfo": "about", "konextra": "coowner", "afktag": "afkbadge",
    "addemoji": "getemoji", "addsticker": "getsticker",
    # ---- admin / setup
    "interface": "panel", "customvc": "vcsetup", "backupload": "restore",
    "disablecommand": "cmdoff", "enablecommand": "cmdon",
    "purgebot": "cleanbots", "purgemember": "clean",
    "setsr": "rolepanel", "editsr": "editpanel", "point": "points",
    # ---- everyday
    "profile": "me", "leaderboard": "rank", "transfer": "move", "linkdel": "unlink", "ar": "react",
}
SHOWN = {v: k for k, v in NEW_NAMES.items()}


def apply(bot) -> list:
    """Add every new name as an alias. Skips (and returns) the ones that can't be added because the name is taken."""
    skipped = []
    for old, new in NEW_NAMES.items():
        cmd = bot.all_commands.get(old)
        if cmd is None:
            continue
        have = bot.all_commands.get(new)
        if have is cmd:                                   # e.g. "lock" was already an alias of vclock
            continue
        if have is not None:
            skipped.append(new)
            continue
        cmd.aliases = list(cmd.aliases) + [new]
        bot.all_commands[new] = cmd
    return skipped


def new_name(cmd) -> Optional[str]:
    """New name of a top-level command (None when it has none)."""
    return NEW_NAMES.get(cmd.name) if getattr(cmd, "parent", None) is None else None


def shown(qualified: str) -> str:
    """'vclock' -> 'lock', 'point add' -> 'points add'. Names without a new one stay as they are."""
    head, _, rest = qualified.partition(" ")
    head = NEW_NAMES.get(head, head)
    return f"{head} {rest}".strip()
