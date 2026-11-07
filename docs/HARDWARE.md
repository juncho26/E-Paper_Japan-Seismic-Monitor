# Hardware

## Parts

| Part | Notes |
|---|---|
| Raspberry Pi Zero WH | "WH" = Wi-Fi + pre-soldered header. Tested model. Any Pi with Wi-Fi and the 40-pin header should work. |
| Waveshare 4.2" e-Paper Module **V2**, 400×300, black/white | Must be **V2** (driver `epd4in2_V2`, supports partial refresh). Check the label on the back of the board. |
| microSD card, 8 GB or more | High-endurance recommended. The program keeps logs in RAM to spare it. |
| 5 V power supply, 2.5 A | Use a decent one; weak supplies cause most "it died" problems. |
| micro-USB cable | Plug into the Pi's **PWR** port (the one nearer the corner), not the USB data port. |
| 3D-printed case (optional) | See [`../hardware/`](../hardware/). |

## Wiring

The Waveshare module is not a HAT for the Zero, so it connects with its 8-pin cable. Power the Pi **off** while wiring.

| Display pin | Wire colour (Waveshare cable) | Pi pin (physical) | Pi GPIO (BCM) |
|---|---|---|---|
| VCC | Grey | 1 (3.3 V) | – |
| GND | Brown | 6 (GND) | – |
| DIN | Blue | 19 | GPIO 10 (MOSI) |
| CLK | Yellow | 23 | GPIO 11 (SCLK) |
| CS | Orange | 24 | GPIO 8 (CE0) |
| DC | Green | 22 | GPIO 25 |
| RST | White | 11 | GPIO 17 |
| BUSY | Purple | 18 | GPIO 24 |

Notes:

- Use **3.3 V** for VCC (pin 1), not 5 V.
- Wire colours are Waveshare's usual cable; if yours differs, go by the pin names printed on the board.
- The software also defines a power-control pin (GPIO 18) from Waveshare's driver; the 8-pin module does not need it connected.
- The pin numbers are set in Waveshare's `epdconfig.py`, installed in step 5.3 of [SETUP.md](../SETUP.md). If you wire differently, change them there.
- If you use a version of the module on a HAT that plugs straight onto the header, no wiring is needed.

SPI must be enabled: `sudo raspi-config nonint do_spi 0`.

## Power

The Pi Zero draws roughly 100-150 mA with Wi-Fi on. The e-paper panel uses power only while redrawing (about a second per update) and then goes to sleep, holding the picture with no power.

## Case

STL/STEP files are in [`../hardware/`](../hardware/). Print settings and assembly notes are in that folder's README.

## Cautions

- E-paper panels are fragile: do not bend the ribbon cable sharply or press on the glass.
- Keep the panel out of long direct sunlight and heat.
- Never hot-plug the display cable while the Pi is powered.
