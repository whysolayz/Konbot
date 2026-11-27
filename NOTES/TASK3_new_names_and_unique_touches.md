# Task 3 - New names, easier bot, things nobody else has

## New names (nothing removed)
`core/names.py` gives every command a friendlier name. The new name is added as **one more alias**, so:
- the old name still works everywhere (chat, saved custom aliases, `$cmdoff` rules, dashboard, no-prefix users),
- slash commands keep their names (no re-sync, nothing to re-learn),
- `$help` shows the new name first and "Old name: `$vclock` (still works)" in the card.
If a new name were ever taken the bot just skips it and logs a warning (it can't crash on a clash). 41 renames, checked for clashes:

| new name | old name |
|---|---|
| `$room` | `$vcinfo` |
| `$lock` | `$vclock` |
| `$unlock` | `$vcunlock` |
| `$hide` | `$vchide` |
| `$reveal` | `$vcunhide` |
| `$boot` | `$vckick` |
| `$banish` | `$vcban` |
| `$pardon` | `$vcunban` |
| `$allow` | `$vcpermit` |
| `$allowall` | `$vcpermitall` |
| `$handover` | `$vctransfer` |
| `$hush` | `$vcmute` |
| `$unhush` | `$vcunmute` |
| `$deaf` | `$vcdeafen` |
| `$undeaf` | `$vcundeafen` |
| `$hushall` | `$vcmuteall` |
| `$unhushall` | `$vcunmuteall` |
| `$banlist` | `$vcbans` |
| `$claim` | `$vcclaim` |
| `$limit` | `$vclimit` |
| `$rename` | `$vcrename` |
| `$topic` | `$vcstatus` |
| `$quality` | `$vcbitrate` |
| `$nodrag` | `$denydrags` |
| `$pullme` | `$dragme` |
| `$pulse` | `$konping` |
| `$about` | `$koninfo` |
| `$coowner` | `$konextra` |
| `$afkbadge` | `$afktag` |
| `$getemoji` | `$addemoji` |
| `$getsticker` | `$addsticker` |
| `$panel` | `$interface` |
| `$vcsetup` | `$customvc` |
| `$restore` | `$backupload` |
| `$cmdoff` | `$disablecommand` |
| `$cmdon` | `$enablecommand` |
| `$cleanbots` | `$purgebot` |
| `$clean` | `$purgemember` |
| `$rolepanel` | `$setsr` |
| `$editpanel` | `$editsr` |
| `$points` | `$point` |

Gotcha handled: `$clean` for someone WITHOUT Manage Messages does nothing (the old `$p` still shows a profile like before).

## Unique touches (not copies of anything)
1. **`$transfer` Studio** (task 1) - menus, leaver menu, tick roles, Undo, History.
2. **`$pulse`** (was `$konping`) - a **heartbeat graph** `▁▂▃▅▃▂▂▃` of the last 12 minutes (latency sampled every 30 s), best / average / worst, plus API + DB latency and status.
3. **`$aura history`** - random aura readings are saved and the graph is shown only when requested, keeping `$aura` itself compact. Fixed auras are not logged.
4. **`$help <word>`** - search by new name, old name, alias or what it does: `$help mute` lists `$hush`, `$hushall`... Only commands YOU can use are shown.
5. **Did you mean?** - a mistyped command (`$lokc`, `$tranfser`) gets a quiet "Did you mean `$lock`?" (close typos only, once per 15 s per channel, disappears after 10 s, never suggests commands you can't use, never for no-prefix users).
6. **Clean report** - `$clean` / `$cleanbots` now say what was removed: `31 text · 4 files · 3 links` / `6 bot replies · 4 commands`.
7. **Giveaway** uses Kon icons by default (task 4).

## Bugs found and fixed (whole project checked)
- Static check of every file (undefined names, duplicate keys/methods, un-awaited coroutines): clean after fixes.
- `rewind / prev / next / fast_forward` icons were requested by the image-link pages but never uploaded -> buttons had no icon. Fixed.
- Duplicate `mic` key in the icon table. Fixed.
- Round-half bug in the aura bar (task 2). Negative bar direction (task 2).
- `$transfer` could not touch people who left (task 1).

## Files changed
`core/names.py` (new), `core/flair.py` (new, pure helpers), `core/db.py`, `main.py`, `cogs/meta.py`, `cogs/errors.py`, `cogs/aura.py`, `cogs/purge.py`, `README.md`.

## How to test
`$lokc` -> hint · `$help mute` -> results · `$pulse` a few times over some minutes -> graph grows · `$aura` 3x -> memory line · `$clean @x 20` -> report · `$vclock` and `$lock` both work.
