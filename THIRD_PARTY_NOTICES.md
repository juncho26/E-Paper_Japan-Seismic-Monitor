# Third-party notices

This project builds on data and software from others. Thank you to all of them.
Please keep these credits if you redistribute or modify the project.

## Earthquake information: Japan Meteorological Agency (気象庁) via P2PQuake

- Source: earthquake information published by the **Japan Meteorological Agency (JMA)**, relayed by **[P2PQuake (P2P地震情報)](https://www.p2pquake.net/)** through its public API (`https://api.p2pquake.net/v2/`).
- P2PQuake's secondary-use terms allow non-commercial and commercial use, and ask that you credit JMA as the source (and the Geospatial Information Authority of Japan (GSI) if you display its map tiles; this project does not use map tiles).
- P2PQuake gives **no guarantee of accuracy** and accepts **no liability** for the information. Neither does this project.
- Please read P2PQuake's current terms yourself before relying on them: <https://www.p2pquake.net/>. They are the authority, not this summary. Be considerate: this project keeps one WebSocket connection open and makes no heavy polling, and any fork should do the same.

## Prefecture outlines: dataofjapan/land

- File: `data/prefectures.geojson`, derived from [dataofjapan/land](https://github.com/dataofjapan/land).
- That data is based on **Global Map Japan (地球地図日本)** from the **Geospatial Information Authority of Japan (国土地理院, GSI)**.
- The dataofjapan/land README asks for attribution of 地球地図日本 for non-commercial use, and says commercial use additionally requires reporting the use to the copyright holder. **If you plan commercial use, check the current terms there and with GSI.**

## Font: Noto Sans JP

- File: `fonts/NotoSansJP-Bold.ttf`, a Bold instance of Noto Sans JP reduced to the characters this project needs (see `fonts/README.md`).
- Copyright 2014-2021 Adobe (<https://www.adobe.com/>), with Reserved Font Name 'Source'. Licensed under the **SIL Open Font License 1.1**: see `fonts/OFL.txt`.
- The file is a modified subset of the original font and is not sold by itself.

## Display driver: Waveshare e-Paper

- The `waveshare_epd` Python driver is **not included** in this repository. You download it from <https://github.com/waveshareteam/e-Paper> (MIT licence) during setup (SETUP.md, step 5.3).

## Python packages (installed with pip, not included)

| Package | Licence |
|---|---|
| [Pillow](https://python-pillow.org/) | HPND (MIT-style) |
| [requests](https://requests.readthedocs.io/) | Apache-2.0 |
| [websocket-client](https://github.com/websocket-client/websocket-client) | Apache-2.0 |
| [gpiozero](https://gpiozero.readthedocs.io/) | BSD-3-Clause |
| [spidev](https://github.com/doceme/py-spidev) | MIT |
| [RPi.GPIO](https://pypi.org/project/RPi.GPIO/) | MIT |

## A note on `data/stations.json`

This is a **hand-compiled, approximate** list of about 80 towns used only as visual reference marks. It is **not** an official list of seismic observation stations, and the marks are not live sensors.
