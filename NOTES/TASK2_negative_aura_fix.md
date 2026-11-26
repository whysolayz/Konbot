# Task 2 - Negative aura started from the wrong side

## The bug
`bar()` in `cogs/aura.py` mirrored the bar for negative values, so `-60%` looked like `[░░░░██████]` - the blocks filled from the **right**, the opposite of a normal aura bar. While the message animated (3 edits) the bar also grew from the wrong end.

## The fix
- Positive and negative aura now **both fill from the left**: `-60%` -> `[██████░░░░]` `-60%`.
- A negative aura is still easy to see: title says **"Negative aura this user holds!"** and the number keeps its minus sign.
- Found + fixed a second small bug on the way: `round()` in Python is *banker's rounding*, so `5%` showed **0 blocks** and `15%` / `25%` jumped unevenly. Now it rounds half up (`5%` -> 1 block).

## Files changed
`cogs/aura.py`

## Quick test
`$aura` a few times (random aura is -100..100). Every bar must start at the left edge.
| value | bar |
|---|---|
| -100 | `[██████████]` |
| -60 | `[██████░░░░]` |
| -5 | `[█░░░░░░░░░]` |
| 0 | `[░░░░░░░░░░]` |
| 60 | `[██████░░░░]` |
