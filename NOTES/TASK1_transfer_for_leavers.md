# Task 1 - `$transfer` for people who left the server (rebuilt as the Role Transfer Studio)

## What you asked
`$transfer` should work for a user who already left: give their user ID + the person who should receive the roles, and all roles of the leaver move to that person. (Feedback: do it INSIDE `$transfer`, no separate command - done, `$leavers` is gone.)

## The real problem
Discord forgets a member's roles the second they leave. Nothing in the API can tell you "what roles did user X have". So the bot saves them **while the person is still in the server**.

## What `$transfer` can do now
| You type | What happens |
|---|---|
| `$transfer` | Opens the **Studio**: menus for *Someone who left*, *From (member)*, *To*, *Roles to move*, then **Transfer** |
| `$transfer @from @to` | Studio with both filled in - one tap on Transfer |
| `$transfer 123456789012345678 @to` | the user LEFT: @to gets the roles saved when they left |
| `$transfer rahul @to` | same, found by the name the bot saved (asks for the ID if two people match) |
| `$transfer @from @to @Role1 @Role2` | only those roles |

### Things you will not find in other bots
- **"Someone who left" menu**: every member the bot saw leave, with role count, "left 3h ago" and their ID. Pick one, no typing.
- **Tick / untick single roles** before confirming (all movable roles start ticked).
- **Live preview** with the exact reason for every skipped role: managed role, above you, above me, target already has it, role was deleted.
- **Undo button** for 10 minutes after a transfer (takes the roles back from the target and gives them back to the old member if still in the server).
- **History button**: last 10 transfers (who did it, from whom, to whom, which roles, ~~undone~~).
- Studio is only usable by the person who opened it. Same permission rules as before (bot owner, `$konextra`, Administrators; nobody can move roles above their own top role).

## How the saving works
- New tables in `kon.db`: `left_members` (roles + name + time) and `transfer_log`. Created automatically; old databases are fine.
- `on_member_remove` saves leavers / kicked / banned members (bots skipped). Records are pruned after 180 days. If the person joins again their record is deleted.
- The backup already covers it (the whole `kon.db` is backed up).

## Limits (honest)
- People who left **before this version** or while the bot was offline can't be transferred - the bot never saw their roles. Discord simply doesn't keep that.
- The role menu shows 25 roles max (Discord limit); roles beyond that stay ticked and move too.

## Files changed
`core/db.py`, `cogs/roletransfer.py`, `cogs/meta.py`, `README.md`

## How to test
1. Give a test account 2 roles, kick it, run `$transfer` -> it is in the *Someone who left* menu.
2. Pick a receiver -> Transfer -> roles appear -> press Undo -> they are gone again.
3. `$transfer <that id> @someone` does the same without the menus.
