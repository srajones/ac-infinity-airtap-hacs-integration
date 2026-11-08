#!/usr/bin/env python3
"""
AC Infinity AirTap T6 BLE HCI Snoop Capture & App Automation Tool (Hardened)

Automates:
  1. ADB device discovery, multi-device disambiguation, and target validation
  2. Split APK installation and permission granting
  3. Bluetooth HCI snoop log configuration & robust activation with verification
  4. AC Infinity app launch & navigation to AirTap T6
  5. Fan speed modification (+1 increment) with timestamp and before/after verification
  6. Multi-tier snoop log extraction (direct pull + adb bugreport fallback)
  7. HCI snoop log deactivation & teardown cleanup in guaranteed finally block
"""

import sys
import os
import time
import subprocess
import xml.etree.ElementTree as ET
import re
import argparse
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict, Any

TARGET_MAC = "A4:C1:38:F6:48:F6"
APP_PKG = "com.eternal.acinfinity"
START_ACT = "com.eternal.start.StartActivity"
def find_repo_root() -> str:
    curr = os.path.dirname(os.path.abspath(__file__))
    while curr and curr != os.path.dirname(curr):
        if os.path.exists(os.path.join(curr, "PROJECT.md")) or os.path.exists(os.path.join(curr, ".git")):
            return curr
        curr = os.path.dirname(curr)
    return os.path.dirname(os.path.abspath(__file__))

REPO_ROOT = find_repo_root()
DEFAULT_APKS_PATH = os.path.join(REPO_ROOT, "AC-Infinity_2.1.2_apkcube.apks")
DEFAULT_OUTPUT_LOG = "/tmp/btsnoop_hci.log"


class ADBController:
    """Encapsulates ADB operations for a specific device serial with multi-device robustness."""

    def __init__(self, serial: Optional[str] = None):
        self.serial = serial

    def build_cmd(self, subcmd: List[str]) -> List[str]:
        base = ["adb"]
        if self.serial:
            base.extend(["-s", self.serial])
        return base + subcmd

    def run(self, subcmd: List[str], check: bool = True, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        full_cmd = self.build_cmd(subcmd)
        res = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        if check and res.returncode != 0:
            raise RuntimeError(f"ADB command failed: {' '.join(full_cmd)}\nstderr: {res.stderr.strip()}")
        return res

    def shell(self, command: List[str], check: bool = True, timeout: Optional[float] = None) -> str:
        res = self.run(["shell"] + command, check=check, timeout=timeout)
        return res.stdout.strip()

    def get_state(self) -> str:
        res = self.run(["get-state"], check=False)
        return res.stdout.strip()

    def install_multiple(self, split_files: List[str], check: bool = True) -> subprocess.CompletedProcess:
        subcmd = ["install-multiple", "-r", "-d", "-g"] + split_files
        return self.run(subcmd, check=check)

    def pull(self, remote_path: str, local_path: str, timeout: Optional[float] = 30.0) -> bool:
        res = self.run(["pull", remote_path, local_path], check=False, timeout=timeout)
        return res.returncode == 0 and os.path.exists(local_path) and os.path.getsize(local_path) > 0

    def bugreport(self, dest_zip: str, timeout: Optional[float] = 180.0) -> bool:
        res = self.run(["bugreport", dest_zip], check=False, timeout=timeout)
        return res.returncode == 0 and os.path.exists(dest_zip) and os.path.getsize(dest_zip) > 0

    def uiautomator_dump(self, remote_sd: str = "/sdcard/ui_dump.xml", retries: int = 3) -> Optional[ET.Element]:
        for attempt in range(retries):
            self.shell(["rm", "-f", remote_sd], check=False)
            self.shell(["uiautomator", "dump", remote_sd], check=False)
            raw_xml = self.shell(["cat", remote_sd], check=False)
            self.shell(["rm", "-f", remote_sd], check=False)
            if raw_xml and "<hierarchy" in raw_xml:
                try:
                    return ET.fromstring(raw_xml)
                except Exception as e:
                    print(f"[*] uiautomator dump XML parse attempt {attempt + 1} failed: {e}")
            time.sleep(1)
        return None


def list_connected_devices() -> List[Dict[str, str]]:
    """Query `adb devices -l` and return structured device records."""
    res = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True)
    if res.returncode != 0:
        return []
    
    devices = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            serial = parts[0]
            state = parts[1]
            details = " ".join(parts[2:])
            devices.append({"serial": serial, "state": state, "details": details, "raw": line})
    return devices


def resolve_device(requested_serial: Optional[str] = None) -> Optional[ADBController]:
    """Resolve and validate the target ADB device with multi-device disambiguation."""
    print("[*] Inspecting connected ADB devices...")
    all_devs = list_connected_devices()
    
    if not all_devs:
        print("\n[!] ERROR: No Android device detected via ADB!")
        print("    Please connect your physical Android phone (e.g. Samsung R3GYB0L62ZK) via USB cable")
        print("    (with USB Debugging enabled in Developer Options) or connect via 'adb connect <IP>:<PORT>'.")
        return None

    # Filter for active authorized devices
    active_devs = [d for d in all_devs if d["state"] == "device"]
    unauth_devs = [d for d in all_devs if d["state"] in ("unauthorized", "offline")]

    if unauth_devs:
        for d in unauth_devs:
            print(f"[!] Warning: Device {d['serial']} is in state '{d['state']}'. Check phone screen for authorization.")

    if not active_devs:
        print("\n[!] ERROR: No authorized Android devices available in 'device' state.")
        return None

    # Case 1: User explicitly requested a serial
    if requested_serial:
        matched = [d for d in active_devs if d["serial"] == requested_serial]
        if not matched:
            print(f"\n[!] ERROR: Requested device serial '{requested_serial}' not found among active devices:")
            for d in active_devs:
                print(f"    - {d['serial']} ({d['details']})")
            return None
        print(f"[+] Using specified device serial: {requested_serial}")
        return ADBController(serial=requested_serial)

    # Case 2: Environment variable ANDROID_SERIAL set
    env_serial = os.environ.get("ANDROID_SERIAL")
    if env_serial:
        matched = [d for d in active_devs if d["serial"] == env_serial]
        if matched:
            print(f"[+] Using device serial from ANDROID_SERIAL: {env_serial}")
            return ADBController(serial=env_serial)

    # Case 3: Exactly one active device
    if len(active_devs) == 1:
        chosen = active_devs[0]["serial"]
        print(f"[+] Automatically selected single active device: {chosen} ({active_devs[0]['details']})")
        return ADBController(serial=chosen)

    # Case 4: Multiple active devices - attempt smart disambiguation (prefer physical over emulator)
    physical_devs = [d for d in active_devs if not d["serial"].startswith("emulator-")]
    if len(physical_devs) == 1:
        chosen = physical_devs[0]["serial"]
        print(f"[+] Multiple devices detected ({len(active_devs)} total). Disambiguated single physical device: {chosen}")
        return ADBController(serial=chosen)

    # Ambiguous multi-device configuration
    print(f"\n[!] ERROR: Multiple active ADB devices detected ({len(active_devs)}).")
    print("    Cannot determine target device automatically. Please specify with -s / --serial <serial>:")
    for d in active_devs:
        print(f"    - {d['serial']}: {d['details']}")
    return None


def wake_and_unlock(adb: ADBController):
    print("[*] Waking screen and dismissing keyguard...")
    adb.shell(["input", "keyevent", "KEYCODE_WAKEUP"], check=False)
    adb.shell(["wm", "dismiss-keyguard"], check=False)
    time.sleep(1)


def ensure_installed(adb: ADBController, apks_path: str, app_pkg: str):
    print(f"[*] Checking if {app_pkg} is installed...")
    pkgs = adb.shell(["pm", "list", "packages", app_pkg], check=False)
    if app_pkg not in pkgs:
        if not os.path.exists(apks_path):
            raise FileNotFoundError(f"APKS bundle not found at {apks_path}. Please supply valid --apks path.")
        print(f"[*] App not installed. Installing splits from {apks_path}...")
        tmp_dir = "/tmp/ac_install_stage"
        os.makedirs(tmp_dir, exist_ok=True)
        try:
            import zipfile
            with zipfile.ZipFile(apks_path, 'r') as zf:
                splits = ["base.apk", "split_config.arm64_v8a.apk", "split_config.en.apk", "split_config.xxhdpi.apk"]
                for s in splits:
                    zf.extract(s, tmp_dir)
            split_files = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir) if f.endswith(".apk")]
            adb.install_multiple(split_files, check=True)
            print("[+] App installed successfully.")
        finally:
            subprocess.run(["rm", "-rf", tmp_dir])
    else:
        print(f"[+] App {app_pkg} is already installed.")

    print("[*] Pre-granting necessary runtime permissions...")
    perms = [
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.ACCESS_COARSE_LOCATION",
        "android.permission.BLUETOOTH_CONNECT",
        "android.permission.BLUETOOTH_SCAN",
        "android.permission.POST_NOTIFICATIONS"
    ]
    for p in perms:
        adb.shell(["pm", "grant", app_pkg, p], check=False)


def configure_btsnoop(adb: ADBController, enable: bool = True, timeout: float = 15.0):
    val = "1" if enable else "0"
    action_str = "Enabling" if enable else "Disabling"
    print(f"[*] {action_str} Bluetooth HCI snoop logging (setting={val})...")
    adb.shell(["settings", "put", "secure", "bluetooth_hci_log", val], check=False)
    if enable:
        adb.shell(["setprop", "persist.bluetooth.btsnooplogmode", "full"], check=False)
        adb.shell(["rm", "-f", "/data/misc/bluetooth/logs/btsnoop_hci.log*"], check=False)
        adb.shell(["rm", "-f", "/sdcard/btsnoop_hci.log*"], check=False)
    
    print("[*] Toggling Bluetooth stack...")
    res = adb.run(["shell", "cmd", "bluetooth_manager", "disable"], check=False)
    if res.returncode != 0:
        adb.shell(["svc", "bluetooth", "disable"], check=False)
    time.sleep(3)
    
    res = adb.run(["shell", "cmd", "bluetooth_manager", "enable"], check=False)
    if res.returncode != 0:
        adb.shell(["svc", "bluetooth", "enable"], check=False)
    
    # Verification loop for Bluetooth state
    print("[*] Verifying Bluetooth stack state...")
    deadline = time.time() + timeout
    bt_on = "0"
    while time.time() < deadline:
        bt_on = adb.shell(["settings", "get", "global", "bluetooth_on"], check=False)
        if bt_on == "1":
            break
        time.sleep(1)

    print(f"[+] Bluetooth state: {'ON' if bt_on == '1' else 'OFF'}")
    if enable and bt_on != "1":
        raise RuntimeError("Bluetooth stack failed to enable after snoop activation. Aborting capture to prevent invalid run.")


def parse_bounds(bounds_str: str) -> Optional[Tuple[int, int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
    if m:
        x1, y1, x2, y2 = map(int, m.groups())
        return (x1 + x2) // 2, (y1 + y2) // 2
    return None


def automate_session(adb: ADBController, change_fan: bool = True, settle_delay: float = 6.0) -> Dict[str, Any]:
    print("\n[*] --- Beginning App Automation & Vent Interaction ---")
    adb.shell(["am", "force-stop", APP_PKG], check=False)
    time.sleep(1)
    
    t_start = datetime.now(timezone.utc).isoformat()
    print(f"[+] Launching AC Infinity App: {t_start}")
    adb.shell(["am", "start", "-n", f"{APP_PKG}/{START_ACT}"], check=False)
    time.sleep(5)
    
    print("[*] Inspecting dashboard for AirTap T6...")
    root = adb.uiautomator_dump()
    tap_target = None
    if root is not None:
        for node in root.iter("node"):
            text = node.attrib.get("text", "")
            res_id = node.attrib.get("resource-id", "")
            if "AirTap" in text or "D-8DAQJ" in text or "8DAQJ" in text or res_id.endswith("tv_device_name"):
                bounds = node.attrib.get("bounds", "")
                tap_target = parse_bounds(bounds)
                print(f"[+] Found device entry '{text}' at {bounds} -> center {tap_target}")
                break
    
    if tap_target:
        adb.shell(["input", "tap", str(tap_target[0]), str(tap_target[1])], check=False)
        time.sleep(4)
    else:
        print("[!] Note: Exact device name not located in initial dump; checking for device cards...")
        if root is not None:
            for node in root.iter("node"):
                res_id = node.attrib.get("resource-id", "")
                if "cl_content" in res_id:
                    bounds = node.attrib.get("bounds", "")
                    tap_target = parse_bounds(bounds)
                    print(f"[+] Tapping card container at center {tap_target}")
                    adb.shell(["input", "tap", str(tap_target[0]), str(tap_target[1])], check=False)
                    time.sleep(4)
                    break
    
    print("[*] Inspecting AirTap Control Screen...")
    root_ctrl = adb.uiautomator_dump()
    plus_target = None
    current_speed = "Unknown"
    
    if root_ctrl is not None:
        nodes = list(root_ctrl.iter("node"))
        for i, node in enumerate(nodes):
            res_id = node.attrib.get("resource-id", "")
            text = node.attrib.get("text", "")
            if res_id.endswith(":id/plus") or "plus" in res_id:
                bounds = node.attrib.get("bounds", "")
                plus_target = parse_bounds(bounds)
                print(f"[+] Located Plus Button at {bounds} -> center {plus_target}")
            if text == "Fan Level" and i + 1 < len(nodes):
                next_node = nodes[i + 1]
                current_speed = next_node.attrib.get("text", "Unknown")
    
    print(f"[+] Current detected Fan Speed: {current_speed}")
    
    event_record = {
        "session_start": t_start,
        "initial_speed": current_speed,
        "modification_timestamp": None,
        "updated_speed": None,
        "fan_changed": False,
    }

    if change_fan and plus_target:
        t_event = datetime.now(timezone.utc).isoformat()
        print("\n=======================================================")
        print("[>>>] EXECUTING FAN SPEED MODIFICATION")
        print(f"      Event Timestamp: {t_event}")
        print(f"      Initial Speed:   {current_speed}")
        print("=======================================================\n")
        adb.shell(["input", "tap", str(plus_target[0]), str(plus_target[1])], check=False)
        time.sleep(2)
        
        root_post = adb.uiautomator_dump()
        new_speed = "Unknown"
        if root_post is not None:
            nodes_post = list(root_post.iter("node"))
            for i, node in enumerate(nodes_post):
                if node.attrib.get("text") == "Fan Level" and i + 1 < len(nodes_post):
                    new_speed = nodes_post[i + 1].attrib.get("text", "Unknown")
        print(f"[+] Updated Fan Speed: {new_speed}")
        event_record["modification_timestamp"] = t_event
        event_record["updated_speed"] = new_speed
        event_record["fan_changed"] = True
    elif not change_fan:
        print("[*] Diagnostic mode: Fan modification skipped per --no-fan-change.")
    else:
        print("[!] Plus button not found automatically. Waiting for manual tap or BLE traffic...")
        time.sleep(4)
    
    print(f"[*] Waiting {settle_delay} seconds for BLE GATT transmission & notification settling...")
    time.sleep(settle_delay)
    t_end = datetime.now(timezone.utc).isoformat()
    event_record["session_end"] = t_end
    print(f"[+] Session completed at {t_end}")
    return event_record


def pull_snoop_log(adb: ADBController, output_path: str):
    print("\n[*] --- Extracting Bluetooth HCI Snoop Log ---")
    pulled = False
    candidates = [
        "/data/misc/bluetooth/logs/btsnoop_hci.log",
        "/sdcard/btsnoop_hci.log",
        "/data/log/bt/btsnoop_hci.log"
    ]
    for c in candidates:
        if adb.pull(c, output_path):
            print(f"[+] Successfully pulled directly from {c} ({os.path.getsize(output_path)} bytes)")
            pulled = True
            break
    
    if not pulled:
        print("[*] Direct pull restricted by Android sandbox; capturing via adb bugreport...")
        tmp_zip = "/tmp/bugreport_airtap.zip"
        if adb.bugreport(tmp_zip):
            import zipfile
            with zipfile.ZipFile(tmp_zip, 'r') as zf:
                snoop_names = [n for n in zf.namelist() if "btsnoop_hci.log" in n and not n.endswith(".last")]
                if not snoop_names:
                    snoop_names = [n for n in zf.namelist() if "btsnoop_hci" in n]
                if snoop_names:
                    chosen = snoop_names[0]
                    print(f"[+] Extracting {chosen} from bugreport...")
                    with open(output_path, "wb") as f_out:
                        f_out.write(zf.read(chosen))
                    print(f"[+] Successfully extracted {output_path} ({os.path.getsize(output_path)} bytes)")
                    pulled = True
            try:
                os.remove(tmp_zip)
            except OSError:
                pass
    
    if not pulled or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError("Failed to extract non-empty btsnoop_hci.log from attached device")


def verify_snoop_file(output_path: str):
    print("\n[*] --- Verifying Extracted Snoop Log ---")
    with open(output_path, "rb") as f:
        magic = f.read(8)
        if magic != b'btsnoop\x00':
            print(f"[!] Warning: File does not start with RFC 1761 'btsnoop\\0' magic: {magic}")
        else:
            print("[+] Header confirmed valid RFC 1761 btsnoop format.")
    print(f"[+] Log size: {os.path.getsize(output_path)} bytes at {output_path}")


def parse_args():
    parser = argparse.ArgumentParser(description="AC Infinity AirTap T6 BLE HCI Snoop Capture & Automation Tool")
    parser.add_argument("-s", "--serial", help="ADB device serial (overrides auto-detection and ANDROID_SERIAL)")
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_LOG, help=f"Destination path for btsnoop log (default: {DEFAULT_OUTPUT_LOG})")
    parser.add_argument("--apks", default=DEFAULT_APKS_PATH, help=f"Path to AC Infinity APKS bundle (default: {DEFAULT_APKS_PATH})")
    parser.add_argument("--skip-install", action="store_true", help="Skip checking/installing the APK")
    parser.add_argument("--no-fan-change", action="store_true", help="Capture without modifying fan level")
    parser.add_argument("--timeout", type=float, default=6.0, help="Settle delay in seconds after GATT interaction (default: 6.0)")
    return parser.parse_args()


def main():
    args = parse_args()
    adb = resolve_device(requested_serial=args.serial)
    if adb is None:
        print("\n[-] Automation stopped: No valid target device available.")
        return 1

    wake_and_unlock(adb)
    if not args.skip_install:
        ensure_installed(adb, args.apks, APP_PKG)

    try:
        configure_btsnoop(adb, enable=True)
        automate_session(adb, change_fan=(not args.no_fan_change), settle_delay=args.timeout)
    finally:
        try:
            pull_snoop_log(adb, args.output)
        except Exception as e:
            print(f"[!] Pull snoop log failed: {e}")
        try:
            configure_btsnoop(adb, enable=False)
        except Exception as e:
            print(f"[!] Deactivating btsnoop failed: {e}")
        adb.shell(["am", "force-stop", APP_PKG], check=False)

    if os.path.exists(args.output) and os.path.getsize(args.output) > 0:
        verify_snoop_file(args.output)
        print("\n[+] Done! Snoop log ready for decoding.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
