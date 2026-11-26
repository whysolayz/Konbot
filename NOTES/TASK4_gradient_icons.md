# Task 4 - New gradient icons, green sheet removed

## What was wrong
- The icons were cut out of a **green icon sheet** (`assets/icon_sheet.png`) and only recoloured, so every icon was a flat outline copy of someone else's shapes.
- 55 extra PNGs were in `assets/icons` that the bot never uses, plus a `panel_original_reference.png` and two generator tools that needed that sheet.
- Hard-coded plain Unicode emojis (green check, red cross, party emoji...) in the giveaway made it look different from the rest of the bot.

## What was done
- **All icons redrawn from scratch** (`tools/make_icons.py`, only pillow + numpy). Same Kon colours (light yellow -> golden -> amber -> orange -> dark orange) but a new "Kon Glass" look:
  soft filled shapes, diagonal gradient, lit top edge, darker lower edge, glossy shine, **engraved dark detail**, cream highlights and a soft shadow. They read well on dark AND light Discord themes (see `kon_icons_preview.png`).
- Team / signal icons (red shield, blue shield, red + green light) keep their real colours because the colour is the meaning.
- **Green sheet gone**: `icon_sheet.png`, `panel_original_reference.png`, `make_assets.py`, `make_game_icons.py` deleted. 88 icons remain = exactly the ones the bot uses (no leftovers; the generator deletes strays by itself).
- **Voice control panel image** (`assets/panel.png`) redrawn with the new icons.
- **Bug found + fixed**: the image-host page buttons asked for `rewind / prev / next / fast_forward` icons that were never uploaded, so those buttons had no icon. Drawn + registered. `member` icon added too.
- **Giveaway uses Kon icons by default** (sparkles, gift, trophy, member, clock, menu, users, check, cross, lock) instead of plain Unicode. The theme list now starts with "Kon Icons (default)". The green-check reaction on task messages is the Kon check.
- Icons re-upload by themselves: the emoji name contains a hash of the image, so the bot deletes the old green ones and uploads the new ones on the next start.

## Files
`tools/make_icons.py` (new), `assets/icons/*`, `assets/panel.png`, `core/icons.py`, `core/emoji.py`, `core/giveaway_logic.py`, `cogs/giveaway.py`, `requirements-tools.txt`, `README.md`. Deleted: sheet, reference image, 2 old tools.

## Regenerate / tweak
`pip install pillow numpy` then `python tools/make_icons.py`. Every icon is ~3 lines of drawing code in that file.
