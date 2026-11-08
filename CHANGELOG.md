## 2.2.0-fix.5 — Broad Vent Compatibility, Protocol Realignment, and Guaranteed Ambient Temperature

- **Feature 16 (Ambient Temperature Guarantee):** Decoupled config flow setup validation so active GATT connection or query timeouts never abort integration setup; gracefully falls back to BLE advertisement manufacturer data (Company ID 2306) guaranteeing the ambient air temperature sensor is always exposed in Home Assistant for HVAC heat pump monitoring.
- **Feature 19 (Broad Vent Compatibility):** Expanded `DEVICE_MODEL` and protocol family resolution to support all AC Infinity Telink chip devices (`{4, 5, 6, 14, 15, 24, 25}`), legacy models (`{1, 2, 6}`), newer AirTap models (Type 48 `Air_Tap`), and all controller models (`{1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 20, 21, 22, 23, 24, 25, 33, 39, 40, 48, 49, 50, 64}`). Unknown model types gracefully fall back to generic naming instead of raising `unsupported_model`.
- **Maximum Entities & Sensors:** Added Operating Mode sensor (`0x10`), On Speed number entity (`0x12`), Signal Strength (RSSI) diagnostic sensor, Protocol Version and Hardware/Software/Firmware Revision diagnostic sensors, Mode selector, and rich fan state attributes.
- **Self-Healing BLE & Multi-Proxy Resilience:** Restored sensors on HA startup via `RestoreSensor`/`RestoreEntity`; eliminated stale advertisement discard bug on proxy handover; refreshed BLEDevice handles dynamically; strict per-step async timeouts preventing event-loop hangs.
- **Protocol Realignment (Official App v2.1.2):** Implemented MTU 247 negotiation, 1500ms post-CCCD ACK settle delay, single-PDU 21-byte calendar clock sync at Seq 0, separate write types (acknowledged 0x12 for setup/clock/GET, unacknowledged 0x52 for fan speed/mode control), and strict Telink OTA characteristic guard (ValueError on handle 28).
- **Tools Preservation:** Tracked and documented `tools/btsnoop_decoder.py` and `tools/capture_airtap_snoop.py` alongside test suite.

## 2.2.0-fix.4 — Official-app calendar initialization

- Full APK type-6 connect-path audit: normal dashboard clock-before-GET flow, control UUIDs, acknowledged write type, MTU247 request, no type-6 bond/key/password or extra-notification prerequisite found.
- Add authorized clock-only initialization to every new type-6 session using HA-local calendar fields; require explicit validated acknowledgement before model reads or user commands.
- Send the 21-byte clock in one public acknowledged write; log the BlueZ MTU23 placeholder caveat without unsafe private MTU/OTA access.
- Defer/rebuild request sequence allocation: clock0 then query1, including reconnects; no uncertain user-setting replay.
- Clock-specific write/response errors, rejection translations, bounded retry/backoff, cancellation cleanup, and warning-once availability behavior.
- Readiness delay alone disproven by fix.3 hardware log; clock requirement remains hardware-unverified. No release/tag.

## 2.2.0-fix.3 — Official APK Telink startup compatibility

- Analyze David's supplied official Android 2.1.2 app locally. Its connect pipeline explicitly waits 1500ms for Telink types including AirTap type 6. Add that wait after subscription/revision reads on every new type-6 setup/runtime connection, and detect/clean up link loss or cancellation during it. Previous hardware attempts wrote after only 91–178ms.
- Use the app's exact legacy model GET (`10 02 03 20`) and separate off/on-speed GET (`11 12`); initialize the per-controller sequence at 0. Keep APK-confirmed A5 00 requests, command 01 and existing CCITT-FALSE CRCs. No speculative fallback/probe variants: the APK shows one applicable wire format.
- Retain strict setup validation and save/restore the single app profile in config data. Reuse its model query during runtime readback. Do not send the app's command-03 clock sync because setup must remain read-only.
- Disable control-reply READ snapshots during type-6 setup to prevent BlueZ read-induced Value callbacks/stale values being confused with notifications. Retain revision/PnP/CCCD diagnostics and annotate diagnostic-read callbacks.
- Match the full reply command word as well as framing/CRC/sequence. Retain specific errors, warning-once streaks, tracebacks, bounded read retries and finally cleanup, including backend disconnect after an already-lost link.
- Hardware-unverified: the missing wait is a confirmed app mismatch and plausible cause, not proof of a successful T6 fix. No APK/app code/assets, OTA/security changes, setting probes, GitHub release or tag.

## 2.2.0-fix.2 — Read-only diagnosis after the first hardware test

- Hardware test of fix.1 confirmed acknowledged writes still receive zero notifications; do not present that theory as the root cause.
- During setup, log revision/PnP reads, cached security context, descriptor handles, CCCD readbacks before/after subscription and after timeout, positive write completion, and pre-query/post-timeout control-value snapshots. Reads are allowlisted, bounded and optional; cancellation propagates. Stored values never substitute for a live reply.
- Accept the APK-verified A5 00–1F reply-header family while retaining CRC/sequence/command validation. Outgoing query framing is unchanged; this proven parser correction does not explain zero raw RX notifications.
- No pairing, firmware/OTA access, additional notification subscriptions, setting writes during diagnosis, or speculative handshake/query variants.

## 2.2.0-fix.1 — T6 compatibility and diagnostics

- Vendor the compatible MIT-licensed 0.4.3 protocol library with capability-aware acknowledged writes, matching GATT pairs, bounded retries/deadlines, frame reassembly/validation and complete cleanup.
- Query only required setup mode/speed keys; reject invalid replies instead of masking failures. Serialize queried configuration state correctly.
- Add step-specific setup errors and English translations, per-step/packet diagnostics, failure-streak logging and graceful command availability.
- Hardware-unverified: the supplied log establishes a response timeout after connection, not a proven protocol root cause. See docs/AIRTAP_INVESTIGATION.md.

# Changelog

## 2.2.0

- Add Auto mode: an Auto preset on the fan, high and low temperature triggers with
  enable switches, and an Auto Transition setting. The vent runs Auto itself.
- Add Off Speed, Temperature Calibration and Temperature Unit settings.
- Add a Current Speed sensor and a Refresh Settings button.
- Read the vent's mode, speed and settings while already connected for a command, so
  the display controls and new settings show real values instead of unknown. Settings
  changed on the vent or in the app appear after the next command or refresh.
- Use the advertised speed for the fan. A proxy can replay an old copy of the
  advertisement for minutes, so repeats and a proxy's first copy aren't trusted, which
  also keeps old temperatures out.
- Restore display and settings values after a Home Assistant restart.

## 2.1.0

- Stop polling the vent. It was connecting about 70 times an hour, and a vent can
  stop advertising for good after a failed connection, until it is power cycled.
  Temperature now comes from the advertisement, and the integration only connects
  to send a command.
- The advertised temperature can trail the value read over a connection (up to
  about 1.5 degrees C seen). Changes made on the vent itself or in the app no
  longer show up in Home Assistant.
- Fix a running vent flipping to off. An advertisement with a fan byte of 0 was
  overwriting the commanded speed, so this release ignores that byte. This entry
  first said the byte is always 0, which was wrong: it appears to report the real
  speed while the vent runs.
- Restore the fan's on/off state and speed after a Home Assistant restart.
- The display switch and brightness show as unknown until you set them, because
  nothing reads them from the vent anymore.
- Fix commands being dropped with a "device unreachable?" error when a second one
  arrived while the first was still connecting. Commands now take turns on one
  connection, and while waiting, a newer command replaces an older one of the same
  kind.

## 2.0.2

- Pass `sw_version` to the device registry as a string. The upstream
  `ac_infinity_ble.DeviceInfo.version` field is an `int`, and handing that
  straight to `DeviceInfo` made Home Assistant log a deprecation warning on
  every startup, once per platform. Non-string values stop being accepted in
  Home Assistant 2026.12.0.

## 2.0.1

Upgrading from 1.x no longer requires deleting and re-adding the integration.

2.0.0 renamed the domain but left users to migrate by hand, because Home
Assistant cannot move a config entry between domains. Entries created by 1.x do
survive in `.storage/core.config_entries` under the old `ac_infinity` domain,
so the config flow now adopts them: the orphaned entry is removed and recreated
under the current domain with the same data, title and unique ID.

The old entry is removed *before* the new one is created, which releases its
entity registry rows, so entity IDs are preserved rather than gaining `_2`
suffixes. Adoption happens on Bluetooth discovery and on manual MAC entry, so
vents that do not advertise reliably are covered too.

Only entries carrying a BLE address are adopted, so a co-installed
[dalinicus/homeassistant-acinfinity](https://github.com/dalinicus/homeassistant-acinfinity)
(cloud, same old domain) is never touched.

## 2.0.0

**Breaking:** the integration domain changed from `ac_infinity` to
`ac_infinity_airtap_ble`, and `custom_components/ac_infinity/` moved to
`custom_components/ac_infinity_airtap_ble/`.

The old domain collided with [dalinicus/homeassistant-acinfinity](https://github.com/dalinicus/homeassistant-acinfinity),
which is already in the HACS default catalogue and installs to the same path.
Both integrations could not be installed side by side, and HACS cannot list two
repositories that claim one domain.

Home Assistant cannot move a config entry between domains, so upgrading from 1.x
requires deleting the existing entry, updating, restarting, and adding the
integration again. Remove any stale `config/custom_components/ac_infinity/`
directory if you installed manually.

- Add brand assets (`brand/icon.png`, `brand/icon@2x.png`).

## 1.3.0

- Rename the project to the correct spelling (**AirTap**); the repository moved to
  `ljmerza/ac-infinity-airtap-ble` (the old `ac-infinity-airtrap-ble` URL redirects).
- Add HACS + hassfest validation and Ruff lint GitHub Actions workflows.
- Declare `integration_type: device` in the manifest.

## 1.2.0

- Initial release: local Bluetooth LE control of AC Infinity AirTap booster-fan
  vents — fan on/off and 10-step speed control, plus a temperature sensor.
