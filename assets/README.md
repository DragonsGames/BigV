# BigV visual assets

`brand/bigv_logo.png` is the local source used in verification panels. Replace
that file with another transparent PNG using the same filename to update the
panel identity without changing Python code. A square image with clear padding
and a strong silhouette works best at Discord thumbnail sizes.

`emojis/` contains compact PNG exports prepared for manual upload as Discord
application emojis. See `EMOJIS.md` for the expected names and fallback
behavior.

The canonical logo was supplied by the BigV project owner. `bigv_shield.png`
is a compact export of that logo. The remaining emoji assets use rounded
Material icons recolored with BigV's brand and semantic colors. See
`THIRD_PARTY.md` for source and license details.

BigV does not hotlink any image at runtime. Replacing or removing an optional
emoji file does not affect verification because the code resolves application
emojis by name and falls back to Unicode.
