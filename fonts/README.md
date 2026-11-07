# Fonts

`NotoSansJP-Bold.ttf` is **Noto Sans JP Bold**, cut down to roughly 6,600 glyphs so it is small enough to load quickly on a Raspberry Pi Zero. The full font is many megabytes.

Licence: SIL Open Font License 1.1, see `OFL.txt`. Copyright Adobe; Reserved Font Name 'Source'.

## Which characters are included

`../data/font_charset.txt` lists the characters kept (kana, ASCII, the kanji used in Japanese place names, earthquake terms and so on). If the screen ever shows an empty box for a character, it is missing from this list.

## Rebuilding the subset

On a normal computer (not the Pi):

```bash
pip install fonttools brotli
# 1. Download a Noto Sans JP variable font, e.g. from https://fonts.google.com/noto/specimen/Noto+Sans+JP
# 2. Make a static Bold instance
fonttools varLib.instancer NotoSansJP[wght].ttf wght=700 -o NotoSansJP-Bold-full.ttf
# 3. Keep only the characters in font_charset.txt
pyftsubset NotoSansJP-Bold-full.ttf --text-file=../data/font_charset.txt \
  --output-file=NotoSansJP-Bold.ttf --layout-features='*' --no-hinting
```

Keep `OFL.txt` next to the font file.
