# Setup guide: Raspberry Pi Zero WH + Waveshare 4.2" e-Paper V2

A start-to-finish guide that has been walked through on real hardware, including the snags people actually hit. Every command is labelled with where it runs:

- `# MAC` / `# PC` — a normal terminal on your own computer
- `# PI` — in an SSH session on the Raspberry Pi

Replace `<username>` with the username you chose in step 1, and `seismic-pi` with the hostname you chose.

> **Golden rule:** if the prompt says `<username>@seismic-pi`, you are on the Pi. If it shows your computer's name, you are not. Many "command not found" and "file not found" errors come from running a command on the wrong machine.

---

## 1. Flash the SD card

1. Install **Raspberry Pi Imager** on your computer.
2. Choose **Raspberry Pi Zero** as the device, **Raspberry Pi OS Lite (32-bit)** as the OS, and your SD card (use a high-endurance card if you can).
3. Open the OS customisation settings and set:
   - hostname (e.g. `seismic-pi`)
   - a username and password — **write the username down**
   - Wi-Fi network and password (the Zero is **2.4 GHz only**) and your country
   - **enable SSH**
4. Write the card.

### If SSH says "Connection refused"
The Pi is on the network but SSH is off. With the SD card in your computer, create an empty file named `ssh` on the boot partition:
```bash
# MAC
touch /Volumes/bootfs/ssh
```
Put the card back in the Pi and boot it.

## 2. First connection

The first boot takes a few minutes. Connect with the `.local` name (the bare hostname without `.local` will not resolve):
```bash
# MAC
ssh <username>@seismic-pi.local
```
If it can't find the Pi, find its IP address in your router's device list and use that instead.

### If you see "REMOTE HOST IDENTIFICATION HAS CHANGED"
That happens after re-flashing a card under the same hostname. Clear the old key:
```bash
# MAC
ssh-keygen -R seismic-pi.local
```

## 3. Update and enable SPI

```bash
# PI
sudo apt update && sudo apt full-upgrade -y
sudo raspi-config nonint do_spi 0
sudo reboot
```
Reconnect after about a minute, then check SPI is on:
```bash
# PI
ls /dev/spidev*
```
Expected: `/dev/spidev0.0  /dev/spidev0.1`.

## 4. Attach the panel

Power the Pi **off** first. Plug the display HAT onto the 40-pin header (or wire the bare panel per Waveshare's 4.2" V2 wiki), then power on.

## 5. Get the code

```bash
# PI
sudo apt install -y git
cd ~
git clone <your-repo-url> seismic-epaper
cd seismic-epaper
```
All project files (`main.py`, `render.py`, `data/`, `fonts/` …) should sit directly inside `~/seismic-epaper`.

### 5.1 Python packages
```bash
# PI
pip3 install -r requirements.txt --break-system-packages
```
This takes several minutes on a Zero. If it fails with `gcc` or `Python.h` errors, install the same packages from apt instead:
```bash
# PI
sudo apt install -y python3-pil python3-requests python3-websocket python3-rpi.gpio python3-spidev python3-gpiozero
```

### 5.2 System libraries for Pillow (needed on Lite)
Pip's Pillow needs a few libraries that Raspberry Pi OS Lite doesn't ship. Without them you'll see errors like `ImportError: libopenjp2.so.7: cannot open shared object file` or `libfreetype.so.6`. Install them all up front:
```bash
# PI
sudo apt install -y libopenjp2-7 libfreetype6 libtiff6 libjpeg62-turbo libwebp7 liblcms2-2 libxcb1 libharfbuzz0b libfribidi0
```
Check everything imports and the font loads:
```bash
# PI
python3 -c "import PIL, requests, websocket, spidev, RPi.GPIO, gpiozero; print('imports OK')"
python3 -c "from PIL import ImageFont; ImageFont.truetype('fonts/NotoSansJP-Bold.ttf', 17); print('fonts OK')"
```
(If apt reports that one of the package names doesn't exist, drop it from the command; `libfreetype6` and `libopenjp2-7` are the ones that matter.)

### 5.3 Waveshare driver
The driver isn't bundled (it belongs to Waveshare). Fetch only the library folder:
```bash
# PI
cd ~
git clone --depth 1 --filter=blob:none --sparse https://github.com/waveshareteam/e-Paper.git
cd e-Paper
git sparse-checkout set RaspberryPi_JetsonNano/python/lib/waveshare_epd
cp -r RaspberryPi_JetsonNano/python/lib/waveshare_epd ~/seismic-epaper/
cd ~ && rm -rf e-Paper
ls ~/seismic-epaper/waveshare_epd/epd4in2_V2.py ~/seismic-epaper/waveshare_epd/epdconfig.py
```
Both files should be listed. (`ls … | head` only shows the first few files alphabetically, so check these two by name.)

## 6. Test by hand

**Render a picture without touching the panel:**
```bash
# PI
cd ~/seismic-epaper
python3 render.py
```
To look at it, run this **on your computer, in a separate terminal window — not in the SSH session**:
```bash
# MAC
scp <username>@seismic-pi.local:~/seismic-epaper/preview.png ~/Desktop/
open ~/Desktop/preview.png
```

**Run it on the real panel:**
```bash
# PI
cd ~/seismic-epaper
python3 main.py
```
The first full refresh flashes black and white for a few seconds — normal. Watch the log, then press **Ctrl+C**. Pressing it several times can print a harmless traceback from the display's sleep call.

## 7. Install as a service

The service file ships with `User=pi`. Install a **copy** with your username filled in (this keeps your Git checkout clean, so `git pull` never conflicts):
```bash
# PI
cd ~/seismic-epaper
sed "s|User=pi|User=$USER|; s|/home/pi|/home/$USER|g" seismic-epaper.service | sudo tee /etc/systemd/system/seismic-epaper.service
```
Confirm the printed file shows your username in `User=`, `WorkingDirectory=` and `ExecStart=`. Then:
```bash
# PI
sudo systemctl daemon-reload
sudo systemctl enable --now seismic-epaper
systemctl status seismic-epaper --no-pager
journalctl -u seismic-epaper -n 30 --no-pager
```
Healthy output: `Active: active (running)` and log lines `display initialised`, `startup complete`, `Websocket connected`.

> Run step 6's manual `main.py` **before** this step. Once the service runs it owns the panel; stop it first (`sudo systemctl stop seismic-epaper`) if you want to test by hand again.

## 8. Harden the Pi for years of unattended running

```bash
# PI
# Wi-Fi power saving off (prevents silent dropouts)
sudo mkdir -p /etc/NetworkManager/conf.d
printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf

# Hardware watchdog: reboot if the whole system freezes
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' | sudo tee /etc/systemd/system.conf.d/watchdog.conf

# Keep logs in RAM to spare the SD card
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=20M\n' | sudo tee /etc/systemd/journald.conf.d/volatile.conf

sudo reboot
```
Reconnect after about 90 seconds and verify:
```bash
# PI
iw dev wlan0 get power_save              # Power save: off
systemctl show -p RuntimeWatchdogUSec    # RuntimeWatchdogUSec=15s
systemctl is-active seismic-epaper       # active
```
The panel should repaint the last known events on its own after boot. (If `iw` isn't found: `sudo apt install -y iw`.)

## 9. Test the "no network" badge

This turns Wi-Fi off for 5 minutes, then back on. SSH will drop — expected.

**Enter your sudo password first.** A background `sudo` can't ask for one, and the job will silently stop:
```bash
# PI
sudo -v
nohup sudo bash -c 'nmcli radio wifi off; sleep 300; nmcli radio wifi on' >/dev/null 2>&1 &
exit
```
What you should see on the panel:

| When | What |
|---|---|
| ~3–4 min after Wi-Fi drops | Small badge at bottom-right listing "no network" and your saved SSIDs; labels move out of its way |
| ~5 min | Wi-Fi returns |
| ~1 min after that | Badge disappears, labels return |

Reconnect with `ssh <username>@seismic-pi.local` afterwards. In the log, an `offline screen (wifi=unavailable)` line marks the badge appearing, and a `partial refresh` line marks the repaint after recovery.

## 10. Measure real speed

After the first real earthquake (or a few days):
```bash
# PI
journalctl -u seismic-epaper --no-pager | grep -E "partial|health" | tail -20
```
Typical on a Pi Zero WH: `partial refresh: render 0.06s, panel 1.14s`.

## 11. Updating later

```bash
# PI
cd ~/seismic-epaper
git pull
sudo systemctl restart seismic-epaper
```
If `seismic-epaper.service` itself changed, redo step 7 and run `sudo systemctl daemon-reload` before restarting.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ImportError: libopenjp2.so.7` / `libfreetype.so.6` | Step 5.2 |
| `ModuleNotFoundError: waveshare_epd` | Step 5.3; run from inside `~/seismic-epaper` |
| `/dev/spidev0.0` missing | `sudo raspi-config nonint do_spi 0`, reboot |
| `scp: … Is a directory` / `open: command not found` | You ran a Mac command on the Pi. Use a separate terminal on your computer |
| Service `status=217/USER` | Wrong username in the service file; redo step 7 |
| Service `203/EXEC` or `200/CHDIR` | Wrong path; check `ls /home/<username>/seismic-epaper/main.py` |
| Background `sudo` job shows `Stopped` | Run `sudo -v` first (step 9) |
| `ssh: Connection refused` | Step 1: create the `ssh` file on the boot partition |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | `ssh-keygen -R seismic-pi.local` |
| Panel blank, no errors | Re-seat the HAT/ribbon; stop the service before testing by hand |
| Service restarts repeatedly | `journalctl -u seismic-epaper -n 60 --no-pager` |

## Handy commands

```bash
# PI
sudo systemctl restart seismic-epaper         # restart
sudo systemctl stop seismic-epaper            # stop (to test by hand)
journalctl -u seismic-epaper -f               # live log, Ctrl+C to leave
sudo systemctl disable --now seismic-epaper   # stop and don't start on boot
```

For the long haul: a decent 5 V / 2.5 A supply and a high-endurance microSD card prevent most "it died after a year" failures.
