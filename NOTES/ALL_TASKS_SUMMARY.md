# Kon - all tasks in one page

| # | Task | Result |
|---|---|---|
| 1 | `$transfer` for people who left | Role Transfer Studio: leaver menu, ID / name, tick roles, live preview, Undo, History. Roles are saved when a member leaves (180 days). |
| 2 | Negative aura starts on the wrong side | Bar fills from the left for + and -; half-up rounding fix. |
| 3 | Redesign / rename, easy, unique, bug hunt | 41 friendlier names (old ones still work), `$pulse` graph, aura memory, `$help` search, did-you-mean, clean report, bug fixes. |
| 4 | Remove green sheet, new gradient icons | 88 icons redrawn from scratch (Kon Glass), sheet + leftovers removed, panel redrawn, giveaway on Kon icons. |

Detailed notes: `NOTES/TASK1_...`, `TASK2_...`, `TASK3_...`, `TASK4_...`.

## Honest notes
- Everything was compile-checked and statically checked, and the database + helper logic were unit-tested. The bot itself could not be started here (no Discord connection), so please try the new commands once on a test server.
- The new icons are uploaded automatically on the next start (old green ones are deleted by the bot).
- Slash command descriptions changed a little (`/konping`), the bot re-syncs them by itself.
