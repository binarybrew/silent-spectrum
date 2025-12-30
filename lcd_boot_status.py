#!/usr/bin/env python3
import glob
import json
import os
import re
import socket
import subprocess
import time

import serial
from smbus import SMBus

# ----------------------------
# Config
# ----------------------------
I2C_BUS = 1
LCD_ADDR_CANDIDATES = [0x27, 0x3F]  # common 1602 I2C backpack addresses

MODEM_PORT = "/dev/serial/by-id/usb-Android_LE910C4-NF_0123456789ABCDEF-if07-port0"
MODEM_BAUD = 115200

PAGE_SECONDS = 3.0       # seconds per page
REFRESH_SECONDS = 2.0    # how often we re-poll modem/network data

PI_IFACE_PREFERENCE = ["eth0", "wlan0"]
MODEM_IFACE_PREFERENCE_PREFIXES = ["wwan", "ppp", "usb"]

# Optional fallback if AT+CNUM returns nothing (leave "" to disable)
FALLBACK_PHONE_NUMBER = ""


# ----------------------------
# LCD driver (PCF8574 + HD44780)
# ----------------------------
RS = 0x01
E  = 0x04
BL = 0x08

def sleep_us(us: int) -> None:
    time.sleep(us / 1_000_000.0)

class I2CLCD1602:
    def __init__(self, bus: SMBus, addr: int, backlight: bool = True):
        self.bus = bus
        self.addr = addr
        self._bl_mask = BL if backlight else 0x00
        self._init_lcd()

    def _write_byte(self, data: int) -> None:
        self.bus.write_byte(self.addr, data | self._bl_mask)

    def _pulse_enable(self, data: int) -> None:
        self._write_byte(data | E)
        sleep_us(1)
        self._write_byte(data & ~E)
        sleep_us(50)

    def _write4(self, nibble: int, is_data: bool) -> None:
        data = nibble | (RS if is_data else 0x00)
        self._write_byte(data)
        self._pulse_enable(data)

    def _send(self, value: int, is_data: bool) -> None:
        hi = value & 0xF0
        lo = (value << 4) & 0xF0
        self._write4(hi, is_data)
        self._write4(lo, is_data)

    def command(self, cmd: int) -> None:
        self._send(cmd, is_data=False)

    def write_char(self, ch: str) -> None:
        self._send(ord(ch), is_data=True)

    def write_string(self, s: str) -> None:
        for ch in s:
            self.write_char(ch)

    def clear(self) -> None:
        self.command(0x01)
        time.sleep(0.002)

    def set_cursor(self, col: int, row: int) -> None:
        row_offsets = [0x00, 0x40]
        self.command(0x80 | (row_offsets[row] + col))

    def write_line(self, row: int, text: str) -> None:
        txt = (text[:16]).ljust(16)
        self.set_cursor(0, row)
        self.write_string(txt)

    def _init_lcd(self) -> None:
        time.sleep(0.05)
        self._write4(0x30, False); time.sleep(0.005)
        self._write4(0x30, False); time.sleep(0.005)
        self._write4(0x30, False); time.sleep(0.001)
        self._write4(0x20, False); time.sleep(0.001)
        self.command(0x28)
        self.command(0x08)
        self.clear()
        self.command(0x06)
        self.command(0x0C)

def open_lcd() -> I2CLCD1602:
    bus = SMBus(I2C_BUS)
    last_err = None
    for addr in LCD_ADDR_CANDIDATES:
        try:
            return I2CLCD1602(bus, addr, backlight=True)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Could not init LCD at {LCD_ADDR_CANDIDATES}: {last_err}")


# ----------------------------
# Modem helpers
# ----------------------------
REG_MAP = {0: "NO", 1: "HOME", 2: "SEARCH", 3: "DENY", 4: "UNK", 5: "ROAM"}

def csq_to_dbm(csq: int):
    if csq == 99:
        return None
    if 0 <= csq <= 31:
        return -113 + 2 * csq
    return None

def at_cmd(ser: serial.Serial, cmd: str, timeout_s: float = 2.5):
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode("ascii", errors="ignore"))
    ser.flush()

    deadline = time.time() + timeout_s
    lines = []
    final = None

    while time.time() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode(errors="ignore").strip()
        if not line or line == cmd:
            continue
        if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
            final = line
            break
        lines.append(line)
    return final, lines

def pick_working_modem_port(preferred: str) -> str:
    if preferred and os.path.exists(preferred):
        return preferred
    byid = sorted(glob.glob("/dev/serial/by-id/*"))
    for p in byid:
        if os.path.exists(p):
            return p
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if ports:
        return ports[0]
    raise FileNotFoundError("No modem serial ports found.")

def parse_ints(line: str):
    return [int(x) for x in re.findall(r"(-?\d+)", line)]

def normalize_phone(s: str) -> str:
    # Keep + and digits
    s = s.strip()
    s = re.sub(r"[^\d+]", "", s)
    return s

def get_phone_number(ser: serial.Serial) -> str:
    """
    Best-effort MSISDN retrieval.
    AT+CNUM often returns nothing depending on SIM provisioning.
    """
    final, lines = at_cmd(ser, "AT+CNUM", 3.0)
    if final != "OK":
        return FALLBACK_PHONE_NUMBER or ""

    # Typical: +CNUM: "","<number>",145,7,4
    for ln in lines:
        if ln.startswith("+CNUM:"):
            m = re.search(r'\+CNUM:\s*"[^"]*"\s*,\s*"([^"]*)"', ln)
            if m:
                num = normalize_phone(m.group(1))
                if num:
                    return num

            # fallback: any quoted thing that looks like a number
            qs = re.findall(r'"([^"]+)"', ln)
            for q in qs:
                num = normalize_phone(q)
                if num and len(num) >= 7:
                    return num

    return FALLBACK_PHONE_NUMBER or ""

def get_modem_snapshot(port: str):
    snap = {
        "port": port,
        "operator": "",
        "reg": "UNK",
        "attached": 0,
        "csq": 99,
        "rssi_dbm": None,
        "rat": "",
        "phone_number": "",
    }

    with serial.Serial(port, MODEM_BAUD, timeout=1, write_timeout=1) as ser:
        at_cmd(ser, "ATE0", 1.0)
        at_cmd(ser, "AT+CMEE=2", 1.0)

        # Phone number (best effort)
        snap["phone_number"] = get_phone_number(ser)

        # Operator
        final, lines = at_cmd(ser, "AT+COPS?", 3.0)
        if final == "OK":
            for ln in lines:
                if ln.startswith("+COPS:"):
                    m = re.search(r'"([^"]+)"', ln)
                    if m:
                        snap["operator"] = m.group(1)
                    nums = parse_ints(ln)
                    if nums:
                        act = nums[-1]
                        snap["rat"] = {7: "LTE"}.get(act, str(act))
                    break

        # LTE registration
        final, lines = at_cmd(ser, "AT+CEREG?", 2.5)
        if final == "OK":
            for ln in lines:
                if ln.startswith("+CEREG:"):
                    nums = parse_ints(ln)
                    if len(nums) >= 2:
                        snap["reg"] = REG_MAP.get(nums[1], str(nums[1]))
                    break

        # Packet attach
        final, lines = at_cmd(ser, "AT+CGATT?", 2.5)
        if final == "OK":
            for ln in lines:
                if ln.startswith("+CGATT:"):
                    nums = parse_ints(ln)
                    if nums:
                        snap["attached"] = 1 if nums[0] == 1 else 0
                    break

        # Signal
        final, lines = at_cmd(ser, "AT+CSQ", 2.5)
        if final == "OK":
            for ln in lines:
                if ln.startswith("+CSQ:"):
                    nums = parse_ints(ln)
                    if len(nums) >= 2:
                        snap["csq"] = nums[0]
                        snap["rssi_dbm"] = csq_to_dbm(nums[0])
                    break

    return snap


# ----------------------------
# Network / IP helpers
# ----------------------------
def run_ip_json(args):
    out = subprocess.check_output(["ip", "-j"] + args, text=True)
    return json.loads(out) if out.strip() else []

def get_ipv4s_by_iface():
    data = run_ip_json(["-4", "addr", "show"])
    by_iface = {}
    for iface in data:
        name = iface.get("ifname", "")
        addrs = []
        for a in iface.get("addr_info", []):
            if a.get("family") == "inet" and a.get("scope") in ("global", "site"):
                local = a.get("local")
                if local:
                    addrs.append(local)
        if addrs:
            by_iface[name] = addrs
    return by_iface

def get_default_route_iface():
    routes = run_ip_json(["route", "show", "default"])
    for r in routes:
        if "dev" in r:
            return r["dev"]
    return None

def choose_pi_ip(by_iface):
    for pref in PI_IFACE_PREFERENCE:
        if pref in by_iface and by_iface[pref]:
            return pref, by_iface[pref][0]
    d = get_default_route_iface()
    if d and d in by_iface and by_iface[d]:
        return d, by_iface[d][0]
    for iface, ips in by_iface.items():
        if ips:
            return iface, ips[0]
    return None, None

def choose_modem_ip(by_iface):
    for iface, ips in by_iface.items():
        if any(iface.startswith(pfx) for pfx in MODEM_IFACE_PREFERENCE_PREFIXES) and ips:
            return iface, ips[0]
    d = get_default_route_iface()
    if d and d in by_iface and by_iface[d] and d not in PI_IFACE_PREFERENCE:
        return d, by_iface[d][0]
    return None, None


# ----------------------------
# Main loop
# ----------------------------
def main():
    lcd = open_lcd()
    lcd.clear()
    lcd.write_line(0, "Booting Pi 5")
    lcd.write_line(1, "Starting...")
    time.sleep(1.5)

    hostname = socket.gethostname()

    last_refresh = 0.0
    modem = {"operator":"", "reg":"UNK", "attached":0, "csq":99, "rssi_dbm":None, "rat":"", "phone_number":"", "port":MODEM_PORT}
    by_iface = {}
    pi_iface = pi_ip = None
    m_iface = m_ip = None

    pages = ["MODEM", "MODEM_NUM", "PI_IP", "MODEM_IP", "HOST"]
    page_idx = 0
    next_page_at = time.time()

    modem_port = pick_working_modem_port(MODEM_PORT)

    while True:
        now = time.time()

        if now - last_refresh >= REFRESH_SECONDS:
            last_refresh = now

            # Network info
            try:
                by_iface = get_ipv4s_by_iface()
                pi_iface, pi_ip = choose_pi_ip(by_iface)
                m_iface, m_ip = choose_modem_ip(by_iface)
            except Exception:
                pass

            # Modem status
            try:
                modem_port = pick_working_modem_port(modem_port)
                modem = get_modem_snapshot(modem_port)
            except FileNotFoundError:
                modem["reg"] = "NO PORT"
                modem["attached"] = 0
                modem["phone_number"] = FALLBACK_PHONE_NUMBER or ""
            except Exception:
                pass

        if now >= next_page_at:
            page_idx = (page_idx + 1) % len(pages)
            next_page_at = now + PAGE_SECONDS

        page = pages[page_idx]

        if page == "MODEM":
            op = modem.get("operator") or "NO OP"
            rat = modem.get("rat") or ""
            reg = modem.get("reg") or "UNK"
            att = modem.get("attached", 0)
            rssi = modem.get("rssi_dbm")

            line1 = f"{op[:10]} {rat[:3]} {reg}".strip()
            rssi_str = "??" if rssi is None else str(rssi)
            line2 = f"RSSI:{rssi_str:>4} A:{att}"

            lcd.write_line(0, line1)
            lcd.write_line(1, line2)

        elif page == "MODEM_NUM":
            num = modem.get("phone_number") or "unknown"
            lcd.write_line(0, "Modem Number")
            lcd.write_line(1, num)

        elif page == "PI_IP":
            if pi_ip:
                lcd.write_line(0, f"Pi IP ({pi_iface})")
                lcd.write_line(1, pi_ip)
            else:
                lcd.write_line(0, "Pi IP")
                lcd.write_line(1, "No IP")

        elif page == "MODEM_IP":
            if m_ip:
                lcd.write_line(0, f"Modem IP ({m_iface})")
                lcd.write_line(1, m_ip)
            else:
                lcd.write_line(0, "Modem IP")
                lcd.write_line(1, "No IP")

        elif page == "HOST":
            lcd.write_line(0, "Hostname")
            lcd.write_line(1, hostname)

        time.sleep(0.2)

if __name__ == "__main__":
    main()
