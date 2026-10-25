"""
netstatus.py — Wi-Fi state and saved networks, read through NetworkManager.

Used by the "no network" screen so you can see, on the panel itself,
whether the Pi is connected and which networks it knows about — without
needing SSH (which is exactly what you lose when the Wi-Fi is down).

Defensive by design: every call has a timeout and nothing here raises.
On any failure the caller just gets less information. Only network NAMES
are ever read — never passwords.
"""

import logging
import shutil
import socket
import subprocess
import time

log = logging.getLogger("seismic-epaper.net")

WIFI_TYPES = ("wifi", "802-11-wireless")


def _run(args, timeout=10):
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("%s failed: %s", args[0], e)
        return ""
    return res.stdout if res.returncode == 0 else ""


def _split_terse(line):
    """Split nmcli -t output on unescaped ':' (nmcli escapes ':' and '\\' with a backslash)."""
    fields, cur, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    fields.append("".join(cur))
    return fields


def _single_value(raw):
    """-g output for one field: an SSID containing ':' comes back escaped; rejoin it."""
    return ":".join(_split_terse(raw.strip()))


def wifi_state():
    """(device, state, connection) for the first Wi-Fi device, else (None, 'unavailable', '')."""
    out = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) >= 4 and parts[1] in WIFI_TYPES:
            return parts[0], parts[2], parts[3]
    return None, "unavailable", ""


_ssid_cache = {}          # profile name -> (monotonic time, ssid)
SSID_CACHE_SECONDS = 3600


def profile_ssid(name):
    """The real SSID of a saved profile. Profiles made by Raspberry Pi Imager are
    named 'preconfigured', not by their SSID, so ask the profile itself.
    Cached: profiles almost never change, and every lookup is a process spawn."""
    hit = _ssid_cache.get(name)
    if hit and time.monotonic() - hit[0] < SSID_CACHE_SECONDS:
        return hit[1]
    ssid = _profile_ssid_uncached(name)
    _ssid_cache[name] = (time.monotonic(), ssid)
    return ssid


def _profile_ssid_uncached(name):
    out = _run(["nmcli", "-t", "-g", "802-11-wireless.ssid", "connection", "show", name])
    ssid = _single_value(out) if out.strip() else ""
    return ssid or name


def saved_wifi_ssids():
    out = _run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"])
    seen, result = set(), []
    for line in out.splitlines():
        parts = _split_terse(line)
        if len(parts) >= 2 and parts[1] in WIFI_TYPES:
            ssid = profile_ssid(parts[0])
            if ssid and ssid not in seen:
                seen.add(ssid)
                result.append(ssid)
    return result


def visible_ssids():
    out = _run(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "no"], timeout=20)
    return {_single_value(line) for line in out.splitlines() if line.strip()}


def ip_address(device):
    if not device:
        return ""
    out = _run(["nmcli", "-t", "-g", "IP4.ADDRESS", "device", "show", device])
    first = out.strip().splitlines()[0] if out.strip() else ""
    return first.split("|")[0].split("/")[0]


def gather():
    """Everything the offline screen needs, in one dict. Never raises."""
    info = {
        "hostname": socket.gethostname(),
        "wifi_connected": False,
        "wifi_state": "unknown",
        "active_ssid": "",
        "ip": "",
        "saved": [],          # [(ssid, visible_bool), ...]
        "error": "",
    }
    if shutil.which("nmcli") is None:
        info["error"] = "nmcli not found"
        return info
    try:
        device, state, connection = wifi_state()
        info["wifi_state"] = state
        info["wifi_connected"] = state.startswith("connected")
        if info["wifi_connected"] and connection:
            info["active_ssid"] = profile_ssid(connection)
            info["ip"] = ip_address(device)
        saved = saved_wifi_ssids()
        visible = visible_ssids()
        info["saved"] = [(s, s in visible) for s in saved]
    except Exception as e:  # never let diagnostics take the monitor down
        log.warning("gather failed: %s", e)
        info["error"] = str(e)[:60]
    return info


def signature(info):
    """Hashable summary of only what the panel actually shows, so internal
    churn (NetworkManager cycling through 'connecting' states, IP changes)
    can't trigger a repaint."""
    return (info["wifi_connected"], info["active_ssid"], tuple(info["saved"]), info["error"])
