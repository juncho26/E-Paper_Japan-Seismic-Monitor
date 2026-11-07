# Contributing

Thank you for helping! Whether you fix a typo, translate, or add a feature, it is welcome.

## Ways to help

- **Build one and tell us** how it went (and share a photo) in an issue.
- **Report problems:** include the output of `journalctl -u seismic-epaper -n 60 --no-pager` and which panel and Pi you use.
- **Improve docs,** especially the Japanese ones.
- **Add features.** Ideas: Earthquake Early Warning (code `556`), tsunami info (`552`), other languages, other Waveshare panel sizes, better label placement in crowded events.

## Try changes without hardware

```bash
pip install Pillow requests
python3 render.py --demo                        # writes preview.png
python3 render.py --sample samples/large.json
python3 render.py --demo --badge
```

Look at `preview.png` after each change. Add your own made-up sample to `samples/` if you need a new case (never present a sample as a real event).

## Guidelines

- Keep it light: this runs on a Pi Zero with 512 MB of RAM. Avoid heavy dependencies, and measure on real hardware if you change drawing code.
- Keep it reliable: the device should run for years unattended. Never let bad data crash the main loop.
- Keep the safety message: this is a convenience display, not a warning system.
- Keep personal data out: no real Wi-Fi names, passwords, emails or addresses in files or screenshots.
- Fonts: if you add characters, rebuild the font subset (see `fonts/README.md`).
- Rebuilding label positions (`tools/make_label_slots.py`) needs numpy and scipy and is run on a normal computer.

## Pull requests

Keep them small and describe what you changed and how you tested it. By contributing you agree that your contribution is licensed under the project's MIT licence.
