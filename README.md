# David's AirTap compatibility build

Version **2.2.0-fix.4**, on `main` only. **Hardware-unverified.** Fix.3's readiness delay and exact model query still produced zero replies on David's T6; the delay alone was not the cause.

## Install / update in HACS

1. Back up HA. Remove the downloaded original `ljmerza/ac-infinity-airtap-ble` repository/files from HACS if still present. Both builds use **the same domain/folder `ac_infinity_airtap_ble`**: replace, do not install alongside. Existing HA config entries need not be deleted just to update.
2. HACS → ⋮ → **Custom repositories** → URL `https://github.com/srajones/ac-infinity-airtap-ble-fix`, category **Integration**.
3. Open this fork → **Redownload → main** (enable beta versions if needed), restart HA, and check its manifest reads `2.2.0-fix.4`. There is **no fix.4 release/tag**; choosing the old release will not install this change.
4. Confirm HA's configured timezone is correct (David: **America/Detroit**). Fully close the AC Infinity phone app so it does not compete for the BLE connection.
5. Settings → Devices & services → Add integration → **AC Infinity AirTap (BLE)** → `D-8DAQJ` / `A4:C1:38:F6:48:F6`, or retry/reload the existing entry.

The patched protocol library is bundled; no manual pip installation is needed.

## What fix.4 changes

The official Android 2.1.2 app's normal dashboard path sends a **clock sync before its first GET refresh**. On every new type-6 connection this build now subscribes, retains the 1.5s Telink wait, then sends the app's calendar command at sequence **0**. It **sets only the vent clock** to HA's configured local calendar; initialization does not change fan mode, speeds, triggers, units or display settings.

The calendar payload is `01 07 YY MM DD weekday hh mm ss`, opcode `03`, with the same `A5 00` header and CCITT-FALSE CRCs as the app. It is **21 bytes**, sent in one acknowledged public Bleak write (not arbitrary 20+1 application fragments). The app requests MTU247. BlueZ's reported `mtu_size=23` may be a placeholder; the HA integration cannot portably force Android's MTU request. We rely on the public acknowledged-write/OS path and preserve any ATT error in the log, not a private MTU helper that could select an unrelated/OTA characteristic.

A matching CRC/sequence/command reply with explicit clock success `01 00` is required before reads or a user command. Missing/rejected/malformed clock replies cannot make setup succeed. Clock sync is idempotently retried on fresh sessions for transient transport failures; rejected ACKs are not retried. No uncertain fan/setting write is replayed. Sequence allocation is deferred until initialization completes, so clock0 precedes query1 on every reconnect.

**Confidence:** high in the APK-derived clock format/order on the dashboard path; **unproven** that the T6 requires clock sync before replying. Enrollment paths may send other preference/read commands first; those are not evidence of mandatory authentication and are not replayed. No type-6 bonding, AES/key/password step, alternative control UUID pair, GAP/revision subscription, signed write, or normal-control priority request was found in the traced path. No OTA access or speculative frame variants were added.

## Capture the next log

Developer Tools → Actions → YAML:

```yaml
action: logger.set_level
data:
  custom_components.ac_infinity_airtap_ble: debug
  bleak_retry_connector: debug
  habluetooth: debug
```

Retry once, then download the **full text log** from Settings → System → Logs. On each fresh type-6 connection expect:

1. `BLE selected` notify **70d51002** / write **70d51001**, CCCD **0100**, revision/PnP reads and the **1.5s readiness wait**.
2. `BLE clock sync start ... sequence=0 local_time=... zone=...`, then **`single acknowledged write length=21`** with `response=True`. Clock TX starts **`a5000009000032a80003`**, followed by `0107`, the current calendar fields and CRC.
3. Real raw `BLE RX ... diagnostic_read=None`, then **`BLE clock sync reply ... payload=0100`** and **`BLE clock sync acknowledged`**. A successful GATT write alone is not a clock acknowledgement.
4. Only then model query **`a5000004000160d8000110020320a0b7`** (sequence1). If needed, speeds GET `1112` follows at sequence2. Setup still requires valid mode and both speeds, **`BLE protocol variant validated and selected for config entry`**, and **`BLE setup validated`**.

Failure distinctions:

- **`clock sync write` / `write_failed`** with ATT invalid length/unsupported operation: investigate actual MTU exchange and long-write support. Send the complete error, not only the timeout line.
- **`clock sync response wait` / `no_response`**: clock was written but no valid reply arrived; no model/fan setting is sent afterward. Expect at most three fresh-session attempts, with 0.25s/0.5s backoff.
- **`clock sync acknowledgement` / `initialization_failed`**: a frame arrived but its ACK payload is rejected/unrecognized. Raw RX bytes will show what to decode next. The implementation intentionally validates more strictly than the app's permissive result checker.
- Clock acknowledged **but model query silent**: initialization is no longer the first unanswered step. Capture both clock and query RX/TX.

Read-only revision/PnP/CCCD diagnostics remain. **Control-value READ snapshots remain disabled for type6**: BlueZ can produce notification callbacks on READ value changes, which are not proof of on-air notifications. A stored value or advertisement never substitutes for a command reply.

If still silent, the decisive next evidence is a **payload-bearing HCI/ATT capture of the official app connecting to this exact T6/software2.2.14**, compared with HA: MTU request/response, CCCD writes, SMP/security if any, first control write and first device notification. Android's filtered/empty payload logs are not sufficient. No bonding requirement has been established, so there is no justified instruction to pair the vent yet.

To disable verbose logs, set these levels to `info`, or restart HA unless levels are in configuration.yaml.

## Failure handling and verification limits

Connect, discovery, subscribe, write, clock/model response wait, unsubscribe and disconnect have named errors and debug tracebacks. Config-flow errors are translated, including clock rejection. Transient transport failures get bounded reconnect/backoff; explicit rejection and uncertain user settings are not replayed. Cancellation propagates, pending futures are released, and failed initialization disconnects before returning. Clock ACK alone does not mark entities available or reset the failure-warning streak; a validated normal response does. There is no background reconnect loop.

Connect deadline45s, GATT operations10s, response waits8s, unsubscribe/disconnect5s; optional metadata reads2s. Connector connection retries remain bounded too. Tests exercise fake BLE and stubbed HA on Python3.13; actual HA/Python3.14 and vent behavior still require David's verification.

See [the investigation and exact APK class/line citations](docs/AIRTAP_INVESTIGATION.md). The APK and decompiled source stay outside this repository; only independently written code and protocol facts are included.

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
