# David's AirTap compatibility build

This fork is an **APK-derived compatibility build, hardware-unverified**, not a claim that the T6 connection failure is fixed. The first build was tested on the vent and still received zero notifications despite successful acknowledged writes. Version: `2.2.0-fix.3`.

## Install this fork through HACS

1. Back up Home Assistant. In HACS, remove the downloaded original `ljmerza/ac-infinity-airtap-ble` repository/integration files. Do not keep two repositories managing the same integration folder. Existing HA configuration entries do not need deleting unless troubleshooting requires it.
2. HACS → ⋮ → **Custom repositories** → add `https://github.com/srajones/ac-infinity-airtap-ble-fix`, category **Integration**.
3. Download **AC Infinity AirTap (BLE)** from this fork. In HACS choose **Redownload → main** (enable beta versions if needed): fix.3 is on `main`, not a GitHub release/tag. Restart Home Assistant. Confirm the downloaded manifest version is `2.2.0-fix.3`.
4. Settings → Devices & services → Add integration → **AC Infinity AirTap (BLE)** → select `D-8DAQJ` / `A4:C1:38:F6:48:F6`.

Both repositories use domain/folder `ac_infinity_airtap_ble`; this **replaces**, rather than installs alongside, the original. No separate Python library installation is needed: the patched protocol library is bundled and HACS/HA installs its ordinary transport dependencies.

## If setup still fails

Developer Tools → Actions → YAML:

```yaml
action: logger.set_level
data:
  custom_components.ac_infinity_airtap_ble: debug
  bleak_retry_connector: debug
  habluetooth: debug
```

Retry setup once and download the **full text log** from Settings → System → Logs. Look for:

- `BLE service` / `BLE characteristic`: UUIDs, handles and properties, including the extra services.
- `BLE selected` and `BLE subscribe`: the exact notify/write pair used.
- `BLE write ... response=True/False bytes=...` and `BLE TX chunk`: what actually went out.
- `BLE RX sender=... bytes=... diagnostic_read=...`: every BLE value callback before parsing. **BlueZ can generate these on a characteristic READ, not just an on-air notification.** fix.3 disables control-value READs during type-6 setup so those cannot masquerade as replies.
- `Ignoring ... response`: CRC, sequence, or framing mismatch rather than a missing radio link.
- `BLE ... failed`: which step failed (`connect`, `service discovery`, `notification subscription`, `write`, `response wait`, `unsubscribe`, or `disconnect`). The setup screen now shows distinct errors.
- `BLE setup validated`: a real matching mode/speed reply was decoded; it is never faked from advertisements or a successful connection alone.

Retained from fix.2, setup also logs **read-only** diagnostics:

- `BLE connection context discovery_snapshot_not_live`: available cached Paired/Bonded/Trusted/ServicesResolved information, not proof of current link encryption. No pairing or bonding is initiated.
- `BLE descriptor`: CCCD UUID/handle discovery.
- `BLE CCCD stage=before_subscribe/after_subscribe/response_timeout`: `0100` means notifications enabled, `0000` means disabled. A successful `start_notify` without enabled readback deserves scrutiny; unavailable reads do not prove a subscription failed.
- `BLE diagnostic read label=firmware_revision/hardware_revision/software_revision/pnp_id`: raw bytes and escaped ASCII from 2A26/2A27/2A28/2A50. Metadata reads happen once per setup controller, not a background loop; each read has a 2s deadline.
- Control-reply snapshots are **disabled during type-6 setup** to avoid read-induced callbacks and stale-value false positives. Revision/PnP/CCCD reads remain; other models retain fix.2 snapshots.

The first hardware log disproves acknowledged writes alone as the explanation. fix.3 does not guess a new outgoing header, CRC algorithm, encryption key, pairing handshake, or setting write. It retains the fix.2 correction for a separately verified receive-parser defect: valid replies may begin `A5 00` through `A5 1F` (e.g. protocol-3 `A5 13`), while queries still begin `A5 00`. That cannot explain zero raw RX callbacks, but avoids rejecting a genuine reply if notification delivery is restored. There are no extra subscriptions to GAP/revision characteristics and no OTA reads/writes.

## fix.3: match the official app's Telink startup

Analysis of David's official **Android 2.1.2 APK** exposed a concrete missing startup step: the app waits **1500ms after subscribing/reading revisions for Telink devices, explicitly including type 6**. The failed HA attempts sent a command only **91–178ms after subscription**; every reconnect repeated that early write. A successful GATT write ACK does not prove the firmware is ready to process it. This is a strong compatibility lead, **not hardware-confirmed as the cause**.

Fix.3 waits 1.5s on **every new type-6 connection**, during setup and runtime (not again on an already-ready connection). Then setup uses the app's exact legacy model GET keys `10 02 03 20`, followed by the app's `11 12` speed GET if necessary. The first allocated sequence is 0, as in the app's counter initialization. Requests still use **A5 00**, command **01**, and dual **CCITT-FALSE** CRCs. Advertisement protocol version 3 does **not** go into the outgoing header; `A5 13` belongs to replies.

There is **no speculative fallback/probe scan**: the supplied app establishes one legacy wire format. Normal bounded read-only retries remain. The app also has clock synchronization (`command 03`, parameter `01`), used before normal dashboard refresh. **That changes the clock and is NOT sent by this patch**, in keeping with read-only setup. No pairing, extra revision/GAP subscriptions or OTA access is added.

Next-log checklist:

1. `BLE Telink readiness wait start ... delay=1.5s`, then `wait completed elapsed=...` **before any control write on each new connection**.
2. `BLE app protocol profile=android_2_1_2_telink ... keys=10020320`.
3. First model query bytes `a5000004000070f9000110020320a0b7` (sequence 0). A read retry may repeat the same request after a fresh connection/delay.
4. Actual `BLE RX` with a matching A5-family reply (possibly `a513...`), passing length/CRC/command/sequence and mode TLV validation.
5. `BLE app follow-up query ... keys=1112` if speeds are missing, then **both** `BLE protocol variant validated and selected for config entry` and `BLE setup validated`.

Only complete valid mode/speed data succeeds. The single app-profile identifier is saved in the config entry and restored at restart; runtime model readback uses the same key list. No success is inferred from advertisements, a GATT ACK, stale characteristic reads or partial replies.

If it remains silent after the wait, send the full log and a payload-bearing **official-app connect capture from this exact T6/software 2.2.14**. That can establish whether the normal clock write is an essential prerequisite, another init step exists, or true ATT notifications are absent. Changing the clock would need explicit permission; no such write is silently tried here.

To stop verbose logging, call `logger.set_level` again with these levels set to `info`, or restart HA (unless levels were set in configuration.yaml).

## Failure handling

Normal runtime read-only operations reconnect up to three times with exponential backoff (0.25s, 0.5s), in addition to the connector's bounded connection attempts. Each connect has a 45s cap; writes/subscription have 10s caps; reply waits have an 8s cap (matching the documented app timeout); type-6 connections also have the app-derived 1.5s readiness wait; unsubscribe/disconnect have 5s caps. We never replay an uncertain setting write. All sessions and pending reply futures are cleaned up in failure/cancellation paths. Failed commands mark entities unavailable until a fresh advertisement or valid reply permits another attempt. There is no background reconnect/polling loop. Failures have debug tracebacks and one warning per failure streak; a successful GATT reply resets the streak.

## Evidence and limits

See [the investigation](docs/AIRTAP_INVESTIGATION.md). Only mocked BLE transport and stubbed HA interfaces were tested locally on Python 3.13; David's actual HA uses Python 3.14 and must provide the hardware verification. No commands are sent to unidentified extra GATT services and no undocumented handshake is invented. The user-provided APK and decompiled code remain outside this repository and are not redistributed.

---

## Original project documentation

# AC Infinity AirTap (BLE)

<p align="center">
<img src="https://img.shields.io/github/stars/ljmerza/ac-infinity-airtap-ble?style=for-the-badge&label=Stars&color=orange" alt="Stars">
<a href="https://github.com/ljmerza/ac-infinity-airtap-ble/releases/latest"><img src="https://img.shields.io/github/v/release/ljmerza/ac-infinity-airtap-ble?style=for-the-badge&color=purple" alt="Version"></a>
<a href="https://github.com/ljmerza/ac-infinity-airtap-ble/actions/workflows/lint.yml"><img src="https://img.shields.io/github/actions/workflow/status/ljmerza/ac-infinity-airtap-ble/lint.yml?style=for-the-badge&label=Lint" alt="Lint"></a>
<a href="https://github.com/ljmerza/ac-infinity-airtap-ble/actions/workflows/validate.yml"><img src="https://img.shields.io/github/actions/workflow/status/ljmerza/ac-infinity-airtap-ble/validate.yml?style=for-the-badge&label=Validate" alt="Validate"></a>
<a href="https://github.com/ljmerza/ac-infinity-airtap-ble/blob/main/LICENSE"><img src="https://img.shields.io/github/license/ljmerza/ac-infinity-airtap-ble?style=for-the-badge&label=License&color=green" alt="License"></a>
</p>

<p align="center">
<a href="https://www.buymeacoffee.com/JMISm06AD"><img src="https://img.shields.io/badge/Buy%20Me%20A%20Coffee-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Buy Me A Coffee"></a>
</p>

A Home Assistant custom integration for **local Bluetooth LE control** of
[AC Infinity AIRTAP](https://acinfinity.com/register-booster-fans/) register /
booster-fan vents (e.g. the AIRTAP T4) — no vendor cloud or phone app required.

This is a trimmed, BLE-only build focused on the AirTap: **fan control +
temperature**, nothing else.

## Features

- **Fan**: on / off, 10-step speed control and an **Auto** preset (`fan.*`).
- **Temperature** and **Current Speed** sensors (`sensor.*`).
- **Auto mode**: high and low temperature triggers, each with an enable switch, plus
  the Auto Transition step. The vent runs these itself, so Auto keeps working when
  Home Assistant or Bluetooth is down.
- **Off Speed**, **Temperature Calibration** and **Temperature Unit** settings.
- **Display** switch and **Display Brightness** select.
- **Refresh Settings** button.

The AirTap T4 reports no humidity, so no humidity entity is created.

### How Auto mode runs

| Auto trigger state | Speed the vent reported |
|---|---|
| Triggered | On speed (the fan's speed setting) |
| Not triggered | Off Speed |

Changing a trigger keeps the vent's current mode. Choosing the Auto preset is what
starts Auto. Pick a speed or turn the fan off to leave it.

Plain off mode reported speed 0 with an Off Speed of 10, so Off Speed only seems to
apply in Auto. A vent with a high Off Speed can run fast in Auto when it isn't triggered.

## Requirements

- A **connectable** Bluetooth path in range of the vent — typically an
  [ESPHome `bluetooth_proxy`](https://esphome.io/components/bluetooth_proxy.html)
  node, or a local adapter on the HA host.
- **Active scanning** is recommended on that proxy
  (`esp32_ble_tracker: scan_parameters: active: true`). The AirTap does not
  reliably broadcast its manufacturer data, so passive-only scanners may never
  surface it for auto-discovery.

## Install

**HACS (custom repository):**

1. HACS → ⋯ → *Custom repositories* → add
   `https://github.com/srajones/ac-infinity-airtap-ble-fix` as an **Integration**.
2. Install **AC Infinity AirTap (BLE)**, then restart Home Assistant.

**Manual:** copy `custom_components/ac_infinity_airtap_ble/` into your HA `config/custom_components/`.

## Setup

Settings → **Devices & Services** → **Add Integration** → *AC Infinity AirTap (BLE)*.

- If the vent is advertising AC Infinity manufacturer data (company id `0x0902`),
  it is offered for auto-discovery / appears in the device list.
- If it isn't (common on the AirTap), the flow falls back to a **manual entry**
  where you paste the vent's **BLE MAC address**. The integration then connects
  to it directly through your proxy.

## How it talks to the vent

Control uses the AC Infinity GATT protocol (service `70D51000-…`, write `…1001`,
notify `…1002`). Commands are `cmd 3` writes: key `0x10` = mode (`01` off / `02` on /
`03` auto), key `0x12` = fan speed (`0`–`10`), key `0x11` = off speed, key `0x13` = Auto
triggers, key `0x20` = unit, key `0x22` = Auto transition, key `0x24` = calibration.

The integration only connects to carry a command or a Refresh Settings press. It does
not poll, because the vent can stop advertising until it is power cycled after a failed
connection. While connected for a command, it also reads the vent's mode, speed and
settings (`cmd 1`), so those stay correct without extra connections. A setting changed
on the vent itself or in the app shows up after the next command or refresh. Between
those, temperature comes from the advertisement's manufacturer data, so it can trail
the real value slightly.

## Troubleshooting

Enable debug logging:

```yaml
logger:
  default: info
  logs:
    ac_infinity_ble: debug
    custom_components.ac_infinity_airtap_ble: debug
```

## Credits

This builds directly on the work of others:

- [**mtsphere/ac-infinity-airtap-hacs**](https://github.com/mtsphere/ac-infinity-airtap-hacs)
  — the AirTap-focused fork this project is based on.
- [**hunterjm/ac-infinity-hacs**](https://github.com/hunterjm/ac-infinity-hacs)
  and the [**ac-infinity-ble**](https://github.com/hunterjm/ac-infinity-ble)
  library by Jason Hunter — the original integration and BLE codec.

Licensed under the MIT License (see [`LICENSE`](LICENSE)); original copyright
retained.
