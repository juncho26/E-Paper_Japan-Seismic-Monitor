"""
main.py — persistent process for the Pi, hardened for years of unattended
operation as well as tuned for emergency responsiveness.

Responsiveness (unchanged from before):
Primary path is a live WebSocket connection to P2PQuake
(wss://api.p2pquake.net/v2/ws) — a new event triggers a partial refresh
within about a second of JMA issuing it. A slow polling fallback (60s)
runs alongside it purely as a safety net. Every event-triggered push is a
partial refresh; full refreshes live on their own independent clock so a
ghosting cleanup can never delay an emergency update.

What's new here is everything to do with running this for years without
a human ever needing to intervene:

- Exponential backoff (capped) on every network failure, instead of
  hammering the API or spinning tight on a dead connection.
- A systemd watchdog heartbeat, so a genuine hang (not a crash — a
  deadlock, a stuck SPI call) still gets caught and restarted, which a
  plain crash-only Restart= policy would miss entirely.
- Every hardware call (SPI/GPIO through the Waveshare driver) is wrapped
  so a single flaky transaction logs and retries rather than taking the
  whole process down.
- Interval timers use time.monotonic(), not time.time(), so an NTP clock
  correction after boot can't cause a spurious immediate (or indefinitely
  delayed) full refresh.
- A real startup retry loop: the first fetch keeps trying with backoff
  instead of giving up after one attempt and leaving the panel blank.
"""

import json
import logging
import os
import socket
import threading
import time
import traceback

import websocket  # pip install websocket-client

import netstatus
import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seismic-epaper")

WS_URL = "wss://api.p2pquake.net/v2/ws"
FALLBACK_POLL_SECONDS = 60                    # safety net only — not the primary path
MAINTENANCE_FULL_REFRESH_SECONDS = 6 * 3600   # ghosting cleanup cadence
WATCHDOG_MARGIN = 2.5                         # ping at WatchdogSec / this margin

OFFLINE_AFTER_SECONDS = 180          # no successful contact this long -> show the offline screen
STARTUP_OFFLINE_AFTER_SECONDS = 45   # shorter at boot, before any contact has ever succeeded
NET_CHECK_SECONDS = 30

# Reconnect backoff is capped low (30s): after an outage ends we want the
# live socket back within seconds, not minutes. A refused/unreachable
# connection costs almost nothing, so retrying every 30s is cheap.
BACKOFF_BASE = 2.0
BACKOFF_CAP = 30.0

MAX_PARTIALS_BEFORE_FULL = 12            # ghosting builds with each partial refresh; clean up at the next quiet moment
HEALTH_LOG_SECONDS = 6 * 3600
MAX_OPEN_FILES = 200                 # normal is ~25; a leak shows up long before the 1024 hard limit
MAX_RSS_MB = 250                     # normal is ~45 on a 512MB Pi Zero
QUIET_BEFORE_MAINTENANCE_SECONDS = 900   # no maintenance refresh within 15 min of an event
OFFLINE_REPAINT_MIN_SECONDS = 300    # never repaint the offline badge more often than this
LOG_THROTTLE_SECONDS = 1800          # repeated identical warnings at most this often
STATE_PATH = os.path.join(render.BASE_DIR, "state", "last_events.json")
CACHED_EVENTS = 3                    # the info box never shows more than this

state = {
    "epd": None,
    "last_event_id": None,
    "last_frame": None,
    "last_full_refresh": 0.0,
    "last_ok": 0.0,            # monotonic time of the last successful contact with P2PQuake
    "ever_online": False,
    "showing_offline": False,
    "offline_sig": None,
    "last_events": None,
    "last_offline_paint": 0.0,
    "dedupe_lock": threading.Lock(),   # guards last_event_id (ws and poll threads race)
    "lock": threading.Lock(),
}


# ---------- systemd integration (watchdog + ready notification) ----------
# Implemented directly over the notify socket rather than pulling in the
# sdnotify package — it's a five-line protocol and one fewer dependency to
# keep updated over five years.

def _sd_notify(message):
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return  # not running under systemd (e.g. manual `python3 main.py`) — no-op
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(addr)
        sock.sendall(message.encode())
        sock.close()
    except OSError as e:
        log.warning("sd_notify failed: %s", e)


def run_watchdog_pings():
    watchdog_usec = os.environ.get("WATCHDOG_USEC")
    if not watchdog_usec:
        return  # WatchdogSec not configured in the unit — nothing to do
    interval = float(watchdog_usec) / 1_000_000 / WATCHDOG_MARGIN
    while True:
        time.sleep(interval)
        # Only ping if the fallback-poll thread has proven itself alive
        # recently — a real hang in that thread should NOT be pinged over.
        if time.monotonic() - state.get("last_poll_alive", 0) < FALLBACK_POLL_SECONDS * 3:
            _sd_notify("WATCHDOG=1")
        else:
            log.warning("fallback poll thread looks stalled — withholding watchdog ping")


# ---------- hardware access, wrapped for resilience ----------

def release_hw():
    """Close the SPI bus and cut panel power (the driver's own module_exit).

    Why this exists: the Waveshare driver's init() re-opens the SPI device every
    time, and the underlying spidev library's open() does NOT close the previous
    handle, so calling init() twice leaks one file descriptor. Maintenance
    refreshes and fault recovery both re-init, so over months that adds up to
    the process's 1024-descriptor limit, after which nothing (network sockets
    included) can be opened. Always release before re-initialising."""
    try:
        from waveshare_epd import epdconfig
        epdconfig.module_exit()
    except Exception as e:
        log.warning("release_hw failed: %s", e)


def restart_epd(epd):
    """Re-initialise the controller without leaking the SPI handle."""
    release_hw()
    return epd.init()


def get_epd(clear=True):
    """Initialise the panel. clear=False when a full-screen repaint follows
    straight away anyway: Clear() is a whole extra ~4s flashing refresh, wasted
    time and wear when the next thing drawn replaces every pixel."""
    from waveshare_epd import epd4in2_V2
    release_hw()                      # harmless on first start, essential on every re-init
    epd = epd4in2_V2.EPD()
    epd.init()
    if clear:
        epd.Clear()
    return epd


def safe_hw_call(fn, *args, retries=2, **kwargs):
    """Every SPI/GPIO transaction goes through here. A single flaky
    transaction (a marginal wire, a brief power sag) shouldn't take the
    whole process down — log it, back off briefly, retry a couple of
    times, and only propagate if it's genuinely stuck."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            log.warning("hardware call %s failed (attempt %d/%d): %s",
                        getattr(fn, "__name__", fn), attempt + 1, retries + 1, e)
            time.sleep(1.5 * (attempt + 1))
    raise last_exc


def reinit_display(frame=None):
    """Called when the display seems stuck — a fresh init sometimes clears
    a wedged SPI/GPIO state without needing a full process restart. get_epd()
    clears the panel, so pass the frame that should be on screen and it is
    painted straight back; otherwise a hardware hiccup would leave the panel
    blank until the next earthquake."""
    log.warning("reinitialising display")
    state["epd"] = safe_hw_call(get_epd, clear=frame is None)
    if frame is not None:
        safe_hw_call(state["epd"].display, safe_hw_call(state["epd"].getbuffer, frame))
        state["last_full_refresh"] = time.monotonic()
        state["partials_since_full"] = 0


# ---------- small utilities ----------

_last_warn = {}


def warn_throttled(key, msg, *args):
    """During a days-long outage the same failure repeats thousands of times;
    log it once per LOG_THROTTLE_SECONDS instead, which keeps the journal
    useful and the SD card quiet. Cleared whenever we get back online."""
    now = time.monotonic()
    if now - _last_warn.get(key, -1e9) >= LOG_THROTTLE_SECONDS:
        _last_warn[key] = now
        log.warning(msg, *args)


def sleep_alive(seconds):
    """Sleep in short slices, stamping the poll thread's heartbeat as we go.
    The watchdog should only trip on a genuine hang (stuck fetch, stuck
    lock) — never on a thread that is simply waiting out a backoff."""
    end = time.monotonic() + seconds
    while True:
        state["last_poll_alive"] = time.monotonic()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(5.0, remaining))


def save_cache(events):
    """Persist the newest few events (atomic write) so a reboot with no
    network still starts on the last known picture instead of a blank
    panel. Written only when a NEW event arrives — a handful of tiny
    writes a day, nothing for SD wear."""
    try:
        slim = [events[0]] + [dict(e, points=[]) for e in events[1:CACHED_EVENTS]]
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(slim, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        warn_throttled("cache-save", "could not save event cache: %s", e)


def load_cache():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            events = json.load(f)
        if isinstance(events, list) and events and events[0].get("id"):
            return events
    except (OSError, ValueError, AttributeError):
        pass
    return None


# ---------- connectivity tracking ----------

def mark_online():
    state["last_ok"] = time.monotonic()
    state["ever_online"] = True
    if _last_warn:
        _last_warn.clear()   # next failure after a recovery gets logged again


def fetch_events_tracked():
    events = render.fetch_events()
    mark_online()
    return events


def is_offline():
    limit = OFFLINE_AFTER_SECONDS if state["ever_online"] else STARTUP_OFFLINE_AFTER_SECONDS
    return time.monotonic() - state["last_ok"] > limit


def show_offline_screen():
    """Paint the 'no network' screen (saved networks etc.). Repaints only when
    something about the network state changed, so a long outage doesn't wear the panel."""
    info = netstatus.gather()          # slow nmcli calls, deliberately outside the lock
    sig = netstatus.signature(info)
    if state["showing_offline"]:
        if sig == state["offline_sig"]:
            return
        # Wi-Fi scan results flicker with signal strength; don't let that
        # turn into a repaint every 30s. (First paint is never delayed.)
        if time.monotonic() - state["last_offline_paint"] < OFFLINE_REPAINT_MIN_SECONDS:
            return
    events = state["last_events"]
    if events:
        # Normal home screen with the latest event, plus a small
        # "no network + saved SSIDs" badge in the bottom-right corner.
        frame = render.build_frame_safe(events, net=info)
    else:
        frame = render.build_offline_frame(info)   # nothing to show yet
    with state["lock"]:
        if not is_offline():           # recovered while we were gathering — don't paint stale news
            return
        state["last_frame"] = frame
        try:
            buf = safe_hw_call(state["epd"].getbuffer, frame)
            log.info("offline screen (wifi=%s)", info["wifi_state"])
            safe_hw_call(state["epd"].display_Partial, buf)
            state["showing_offline"] = True
            state["offline_sig"] = sig
            state["last_offline_paint"] = time.monotonic()
            state["partials_since_full"] = state.get("partials_since_full", 0) + 1
        except Exception:
            log.error("offline screen failed, reinitialising:\n%s", traceback.format_exc())
            try:
                reinit_display(frame)
            except Exception:
                log.error("reinit also failed — will try again")


def run_network_monitor():
    while True:
        time.sleep(NET_CHECK_SECONDS)
        try:
            if is_offline():
                show_offline_screen()
        except Exception:
            log.warning("network monitor error:\n%s", traceback.format_exc())


# ---------- event handling ----------

def push_partial(events, reason):
    with state["lock"]:
        t0 = time.monotonic()
        frame = render.build_frame_safe(events)
        t1 = time.monotonic()
        state["last_frame"] = frame
        state["last_events"] = events
        try:
            buf = safe_hw_call(state["epd"].getbuffer, frame)
            safe_hw_call(state["epd"].display_Partial, buf)
            state["showing_offline"] = False
            state["last_push"] = time.monotonic()
            state["partials_since_full"] = state.get("partials_since_full", 0) + 1
            log.info("partial refresh (%s): render %.2fs, panel %.2fs",
                     reason, t1 - t0, time.monotonic() - t1)
        except Exception:
            log.error("partial refresh failed after retries, reinitialising:\n%s",
                       traceback.format_exc())
            try:
                reinit_display(frame)
            except Exception:
                log.error("reinit also failed — will try again on the next event")


def handle_new_events(events, reason):
    if not events:
        return
    latest_id = events[0].get("id")
    with state["dedupe_lock"]:
        # Recovering from the offline badge must repaint even when no new
        # event arrived while we were away.
        is_new = latest_id != state["last_event_id"]
        if not is_new and not state["showing_offline"]:
            state["last_events"] = events
            return
        state["last_event_id"] = latest_id
    push_partial(events, reason)
    if is_new:
        save_cache(events)


# ---------- WebSocket (primary, low-latency path) ----------

def on_ws_open(ws):
    mark_online()


def on_ws_message(ws, message):
    state["ws_backoff"] = BACKOFF_BASE  # any message proves the connection is healthy
    mark_online()
    try:
        data = json.loads(message)
    except ValueError:
        return
    if data.get("code") != 551 or data.get("cancelled"):
        return  # this renderer only handles the detailed-intensity report type
    # Fastest path: the pushed message is already a complete event in the
    # same shape as the history API. Draw it NOW, merged in front of the
    # history we already hold, instead of first waiting on an extra HTTP
    # round-trip (DNS + TLS + download on a Pi Zero). The slow fallback poll
    # reconciles the history list afterwards.
    old = state["last_events"] or []
    events = [data] + [e for e in old if e.get("id") != data.get("id")]
    handle_new_events(events[:render.HISTORY_LIMIT], "websocket")


def on_ws_error(ws, error):
    warn_throttled("ws-error", "websocket error: %s", error)


def on_ws_close(ws, *_):
    log.debug("websocket closed — reconnecting")


def run_websocket_forever():
    state["ws_backoff"] = BACKOFF_BASE
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_ws_open,
                on_message=on_ws_message,
                on_error=on_ws_error,
                on_close=on_ws_close,
            )
            # p2pquake force-closes idle sockets roughly every 10 minutes —
            # expected, and just lands us back here, not an error worth
            # backing off for (on_ws_message already reset the backoff).
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception:
            log.warning("websocket loop crashed:\n%s", traceback.format_exc())
        backoff = state.get("ws_backoff", BACKOFF_BASE)
        time.sleep(backoff)
        state["ws_backoff"] = min(backoff * 2, BACKOFF_CAP)


# ---------- fallback poll (redundancy, and the watchdog's proof-of-life) ----------

def run_fallback_poll():
    """Safety net (and the watchdog's proof-of-life). Each cycle asks the
    API for just the newest event's id and only downloads the full history
    when something actually changed."""
    while True:
        sleep_alive(FALLBACK_POLL_SECONDS)
        try:
            latest_id = render.fetch_latest_id()
            mark_online()
            if latest_id is not None and (latest_id != state["last_event_id"]
                                          or state["showing_offline"]):
                handle_new_events(fetch_events_tracked(), "fallback poll")
        except Exception as e:
            warn_throttled("poll", "fallback poll failed: %s", e)
        finally:
            state["last_poll_alive"] = time.monotonic()


# ---------- maintenance (ghosting cleanup, fully decoupled from events) ----------

def check_health():
    """Cheap self-check, run every minute. If something has slowly gone wrong
    (a leaked file descriptor, a memory leak) over weeks, restart cleanly NOW
    — systemd brings the service back in seconds and the last event is redrawn
    from the cache — rather than let it degrade silently for months."""
    try:
        fds = len(os.listdir("/proc/self/fd"))
        with open("/proc/self/statm") as f:
            rss_mb = int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError):
        return
    now = time.monotonic()
    if now - state.get("last_health_log", -1e9) >= HEALTH_LOG_SECONDS:
        state["last_health_log"] = now
        log.info("health: %d open files, %.0f MB resident", fds, rss_mb)
    if fds > MAX_OPEN_FILES or rss_mb > MAX_RSS_MB:
        log.error("health limit exceeded (%d files, %.0f MB) — exiting for a clean restart", fds, rss_mb)
        os._exit(70)


def run_maintenance_refresh():
    while True:
        time.sleep(60)
        check_health()
        due_by_time = time.monotonic() - state["last_full_refresh"] >= MAINTENANCE_FULL_REFRESH_SECONDS
        due_by_count = state.get("partials_since_full", 0) >= MAX_PARTIALS_BEFORE_FULL
        if not (due_by_time or due_by_count):
            continue
        if state["last_frame"] is None:
            continue
        # Never start a ~5s full refresh while a quake sequence may still be
        # unfolding: aftershock reports would have to wait behind it. It simply
        # runs at the next quiet moment instead.
        if time.monotonic() - state.get("last_push", -1e9) < QUIET_BEFORE_MAINTENANCE_SECONDS:
            continue
        with state["lock"]:
            try:
                log.info("maintenance full refresh")
                safe_hw_call(restart_epd, state["epd"])
                safe_hw_call(state["epd"].display, safe_hw_call(state["epd"].getbuffer, state["last_frame"]))
                state["last_full_refresh"] = time.monotonic()
                state["partials_since_full"] = 0
            except Exception:
                log.error("maintenance refresh failed:\n%s", traceback.format_exc())


# ---------- startup ----------

def fetch_with_retry():
    """The very first fetch keeps trying with backoff instead of giving up
    after one attempt — a slow DHCP lease or not-yet-up WiFi at boot
    shouldn't leave the panel blank until the next lucky event."""
    backoff = BACKOFF_BASE
    while True:
        try:
            return fetch_events_tracked()
        except Exception as e:
            warn_throttled("startup", "startup fetch failed (retrying in %.0fs): %s", backoff, e)
            time.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_CAP)


def main():
    cached = load_cache()             # read first: if there is one, the first paint is a full repaint, so skip Clear()
    state["epd"] = safe_hw_call(get_epd, clear=cached is None, retries=5)
    now = time.monotonic()
    state["last_full_refresh"] = state["last_poll_alive"] = state["last_ok"] = now

    # Signal readiness now, independent of network — the display is
    # initialised and the process is genuinely alive. An outage should
    # degrade to "stale display," never "blank forever."
    _sd_notify("READY=1")
    log.info("display initialised, starting background threads")

    # Warm every cache (fonts, map, projection) now, so the first real
    # event is drawn at steady-state speed rather than paying load costs.
    try:
        render.warm_up()
    except Exception:
        log.warning("cache warm-up failed:\n%s", traceback.format_exc())

    threading.Thread(target=run_fallback_poll, daemon=True).start()
    threading.Thread(target=run_maintenance_refresh, daemon=True).start()
    threading.Thread(target=run_watchdog_pings, daemon=True).start()
    threading.Thread(target=run_network_monitor, daemon=True).start()

    # If we were restarted (power cut, update), show the last known event
    # immediately — from disk, before any network — rather than a blank panel.
    if cached:
        try:
            with state["lock"]:
                frame = render.build_frame_safe(cached)
                state["last_frame"] = frame
                safe_hw_call(state["epd"].display, safe_hw_call(state["epd"].getbuffer, frame), retries=5)
                state["last_events"] = cached
                state["last_event_id"] = cached[0].get("id")
                state["showing_offline"] = False
                state["last_full_refresh"] = time.monotonic()
            log.info("showing cached event while connecting")
        except Exception:
            log.error("cached startup frame failed:\n%s", traceback.format_exc())

    events = fetch_with_retry()  # may legitimately take a while after an outage — that's fine now
    if cached:
        handle_new_events(events, "startup")   # repaints only if something changed
    else:
        frame = render.build_frame_safe(events)
        # Held under the lock: the background threads are already running and
        # must not touch the panel while this first full refresh is in progress.
        with state["lock"]:
            state["last_frame"] = frame
            safe_hw_call(state["epd"].display, safe_hw_call(state["epd"].getbuffer, frame), retries=5)
            state["showing_offline"] = False
            state["last_events"] = events
            state["last_full_refresh"] = time.monotonic()
        if events:
            state["last_event_id"] = events[0].get("id")
            save_cache(events)

    log.info("startup complete")

    try:
        run_websocket_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("sleeping display")
        try:
            state["epd"].sleep()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
