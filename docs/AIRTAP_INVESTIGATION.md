# T6 setup investigation

The sections below preserve the investigation history. **fix.3 APK findings at the end supersede earlier hypotheses and snapshot behavior.**

## Confirmed findings

The supplied HA log shows a successful BLE connection via hci0 at approximately RSSI -72, notification subscription completing, and the 20-byte query `a5000008000225da00011011121314151617713e` timing out waiting for a notification. This is **not evidence of a range failure**, and a MAC prefix cannot establish protocol compatibility.

The manufacturer payload for `BLE_FAN` / `8DAQJ` decodes under 0.4.3 to **device type 6, protocol version 3, name D-8DAQJ**. It is not type 48. Bytes 11 and 12 after the five-byte identifier contain version/type. The existing library names type 6 family D. Retail T6 versus T4 cannot be distinguished from that family byte alone.

Integration 2.2.0 pins `ac-infinity-ble==0.4.3`. Its transport always calls `write_gatt_char(..., False)` without checking advertised properties, waits only five seconds, independently picks notify and write UUIDs, and leaves the pending future behind on timeout or write failure. Its subclass also accepts any A5 notification without complete-frame/CRC/sequence validation, parses setup state at fixed offsets, and tests state conversion with `is DeviceInfo` instead of `isinstance`. Discovered setup saves an un-serialized advertisement dataclass instead of the queried state. These are demonstrable code defects; not all necessarily caused this device's silence.

## Initial hypothesis, superseded by the fix.1 hardware test

The initial write-without-response assumption was an actionable transport mismatch. Independent app protocol notes document acknowledged writes when supported. We select acknowledged writes when the characteristic advertises `write`, otherwise write-without-response, and log the choice. The fix.1 hardware log now shows `70d51001` advertises read+write, our acknowledged write completes, and still no raw RX notification arrives. **Acknowledged writes alone do not resolve the silence; that initial root-cause hypothesis is rejected.**

The fix.1/fix.2 minimum setup query requested **only 0x10/0x11/0x12**, not unused timers/schedules. It requires a CRC/sequence-matched reply with valid mode and both speeds, decoded as TLVs. Removing unrelated keys avoids demanding non-AirTap features; whether the old broad query was rejected by this firmware remains unproven. A longer deadline and bounded read reconnects improve tolerance but do not bypass validation or invent a reply.

## Reference comparisons

- Inspected PyPI sdists **0.4.3 and 1.0.0** (the latter has broader API/model changes, so this fork vendors and minimally patches the compatible 0.4.3 API rather than replacing it wholesale).
- 1.0.0 pairs GATT UUIDs, reassembles/validates replies, chunks writes, and cleans futures, but still uses write-without-response. We backport the relevant transport protections without importing its changed command/state interfaces.
- [Original issue #1](https://github.com/ljmerza/ac-infinity-airtap-ble/issues/1) had **no comments** when fetched. Read the attached `latest-2.txt`: the new device is type **48**, version **1**, advertised `Air_Tap`, unlike this type-6/version-3 BLE_FAN. The attachment shows discovery and separate old-vent reachability failures; it does not contain a complete new-device GATT transcript establishing the same root cause.
- [Nyx000 transport notes](https://github.com/Nyx000/acinfinity-ble/blob/main/docs/protocol/ble-transport.md): documents subscribe-before-write, acknowledged writes, eight-second app timeout, sequence echo and UUIDs. This is reference evidence, not proof about David's particular firmware.
- [Nyx000 wire protocol](https://github.com/Nyx000/acinfinity-ble/blob/main/docs/protocol/wire-protocol.md): framing, CRC and TLV meanings.
- [Viss HA implementation](https://github.com/Viss/ha-ac-infinity-ble): inspected its vendored device implementation; its focus is Controller 69 rather than AirTap and it likewise does not prove T6 behavior.
- [hunterjm library](https://github.com/hunterjm/ac-infinity-ble): 1.0.0 source used as the comparison above.

No verified source established a required handshake or role for `00112233-4455-6677-8899-aabbccdd9922` on this T6. We log its discovered characteristics but deliberately do not write arbitrary bytes to it, change checksums, or probe unrelated services.

## Local verification

Run `python -m pytest -q`, `ruff check custom_components tests`, `python -m compileall -q custom_components tests`, and `git diff --check` using the isolated test environment described by requirements-test.txt. Tests simulate the hardware boundary and stub the HA config-flow interfaces; they are not live HA or radio tests. Hardware validation and a Python 3.14 HA runtime test remain necessary.

## fix.2 investigation after the hardware test

`ha_airtap_debug2.log` shows hci0 connection around RSSI -64; notify characteristic 70D51002 handle 16, write characteristic 70D51001 handle 20; subscription completes; read command `a50000030002d52b000110111226ea` is sent with response=True. The attempts in the captured log have **zero raw RX callbacks**, not merely invalid/filtered replies. The extra revision characteristics are under custom service 00112233-...9922 and expose read+indicate; the Telink-looking 1912/2B12 service is deliberately untouched.

Additional sources inspected:

1. [hunterjm APK 2.0.8 validation](https://github.com/hunterjm/ac-infinity-ble/blob/main/docs/protocol-validation.md): types 1/2/6 share the legacy ProtocolResolution path. Queries use A5 00, command 01 and a list of parameter IDs; no special type-6/version-3 encrypted envelope is established. **Replies**, unlike queries, allow A5 00 through A5 1F; protocol-3 captures use A5 13. This exposed an independent fix.1 parser restriction, now corrected with independent binascii CRC fixtures.
2. [hunterjm current connection code](https://github.com/hunterjm/ac-infinity-ble/blob/main/src/ac_infinity_ble/device.py): connect → subscribe to control characteristic → read optional 2A26/27/28; no pair/bond call or revision/GAP subscription. Rechecked the 1.0.0 sdist against these paths.
3. [Nyx live app captures](https://github.com/Nyx000/acinfinity-ble/blob/main/docs/protocol/app-ble-capture-verified-2026-06.md): outgoing A5 00, incoming A5 17/1C examples, parameter queries, unencrypted frame/CRC protocol. These are Controller 69/powerstrip observations, not T6 certification.
4. [Controller 67 reverse engineering thread](https://community.home-assistant.io/t/ac-infinity-controller-67-bluetooth-temp-humidity-fan-pwm/410673/13): concrete examples show the readable 70D51002 holding an A5 10 reply while its CCCD is 0000 and Notifying=no. This supports read-only snapshots and CCCD diagnosis, **not** blindly accepting stored data. Earlier thread language about app “bonding” is not proof that T6 requires an SMP bond.
5. [Working AirTap type-6 report](https://github.com/hunterjm/ac-infinity-hacs/issues/18): adding type 6 and HA fan feature fixes reportedly made an AirTap work; no new handshake is described. No verified ESPHome-native alternative establishing a T6 handshake was found.

### Changes and confidence

High confidence in the receive-header correction and the safety of standard characteristic/CCCD reads. **Low confidence in any specific remaining root cause** until the next diagnostics return values. No unverified encryption, bonding requirement, extra subscription, outgoing-header mutation, sequence guessing, or command variant is shipped. A wrong incoming header cannot account for zero raw callbacks, because raw logging precedes parsing.

Setup now reads only allowlisted 2A26/27/28 revision values, 2A50 PnP ID and 70D51002 control-value snapshots; it reads only the selected control CCCD 2902. It logs the available cached security context without initiating pairing. Diagnostic reads have individual 2s deadlines, optional failures retain tracebacks with one warning, and cancellation propagates. Reads and descriptor discovery do not bypass Bleak to manipulate CCCDs or BlueZ state. Missing CCCD read support on a proxy is reported as unknown, never treated as evidence of disabled notifications.

### What the next log can settle

- CCCD after-subscribe `0000`: investigate notification enabling/backend behavior. No manual descriptor overwrite is performed.
- CCCD `0100`, no RX, but a new A5 frame in the post-timeout snapshot compared with baseline: strong evidence of a notification delivery path problem. Compare its sequence, command, length and CRC with the query; snapshots are diagnostic only.
- CCCD `0100` with empty/unchanged control data: narrows the question to firmware command acceptance, security/initialization, or device state; it does not prove encryption.
- Explicit ATT authentication/encryption errors on revision/control/CCCD reads: security is a supported next investigation. A cached Paired=False alone does not prove pairing is necessary.
- Revision/PnP strings: compare the exact software/hardware with a working type-6 device or official-app connection capture.
- Still no answer: a full **official app connect-only ATT/HCI capture** showing service discovery, CCCD writes, reads, any SMP security exchange and the first control command/reply is the decisive evidence. No settings need be changed to collect that evidence. Packet payloads may be filtered on some Android phones; collect an actual full HCI capture if available rather than assuming an empty capture proves no traffic.

## fix.3: primary analysis of David's official Android APK

### Primary artifact and method

Read-only Dropbox listing located `/AC/AC-Infinity_2.1.2_apkcube.apks` (131,941,977 bytes), downloaded locally to `/workspace/ac-apk/`. Archive SHA-256:

`19c59c21aa9bde1f3fa1f0c3320fbf4a25bcb6bf98c8d9860634ce85e211bec6`

Extracted `base.apk` SHA-256:

`b22bf4d636ec288905b0a7428641a0ac63d4f630881fbdb9ff2231140d0ee022`

This is a native Java/Kotlin Android app with multiple DEX files, not a Flutter `libapp.so` or React Native bundle. JADX 1.5.1 produced the relevant classes under `/workspace/ac-apk/jadx/sources/`; the overall run reported 49 errors in unrelated UI/media/third-party methods. The BLE builders/connect/state methods cited here decompiled successfully. `com/eternal/acinfinity/BuildConfig.java` confirms the package/version. No app code or assets are redistributed in this repository; below are protocol facts restated in our own words. User-provided APK analysis supersedes earlier public-source speculation.

`assets/DeviceConfig.json` identifies `devType=6` as Airtap / All Bluetooth Models, distinct from Wi-Fi Airtap AI type 48. Software `2.2.14` is a device revision, not the advertisement protocol revision 3 and not the Android app version.

### A concrete startup mismatch: Telink-specific 1500ms wait

`com/eternal/base/data/ble/BleController.java`:

- `connect` at **5637–5639** chains connection, MTU request, control subscription, applicable family-specific operations, software/hardware revision reads, then `v3` before declaring success.
- `v3.apply` at **3061–3071** explicitly waits **1500 milliseconds for `isTelinkChip(type)`**.
- `com/eternal/base/protocol/a.java:isTelinkChip` at **4382–4383** explicitly includes types 4, 5, **6**, 14, 15, 24 and 25.
- Control subscription is **70D51002 under 70D51000** (`c1.apply`, 432–438); the BLE-only MTU request is 247 (`n1`, 2196–2203). The short read commands fit even the minimum ATT MTU, so no unsupported private MTU API is added.
- The extra revision/service subscription blocks are gated by Wi-Fi+BLE/thread device families, which do not include type 6. There is no type-6 GAP/revision-indication or pairing/encryption prerequisite established in this connect path.

The failed fix.2 HA log shows first writes **178ms, 91ms and 113ms after `start_notify` completed**, all much earlier than the official app's post-revision 1500ms wait. Reconnecting on every failed request repeated that early-write behavior. This is a **confirmed behavioral mismatch with the vendor app**, and a plausible explanation for a GATT-acknowledged write being ignored. It is **not yet proof of the vent's root cause**; hardware validation is still required.

Fix.3 applies the app's 1.5s wait to **every new type-6 session**, not just setup or a longer response timeout. Existing ready connections do not wait again. The connection lock covers this readiness phase, so concurrent commands cannot bypass it. A lost link during the delay raises the specific `Telink readiness` / `connect_failed` error; cancellation propagates and cleanup releases the client, including backend resources after `is_connected=False`.

### Exact framing and query facts from this APK

Local source references (paths relative to the private JADX source folder):

- `defpackage/s10.java` (`BaseProtocol`), **13–25**, and `defpackage/vd6.java` (`ProtocolResolution`), **42, 55–71**: request header is **A5 00**, regardless of advertisement protocol version. Length and sequence are unsigned BE16. Header CRC covers bytes 0–5; byte 8 is zero, byte 9 is the opcode; body CRC covers byte 8 through payload end. Total length is payload + 12.
- `com/eternal/base/utils/CRCUtil.java`, **5–24**: **CRC-16/CCITT-FALSE**, initial FFFF, polynomial 1021, non-reflected, no final XOR, output big-endian. Independently compared the vendored algorithm with Python `binascii.crc_hqx(..., 0xffff)` across 10,000 random vectors with no mismatch; tests independently checksum request/reply fixtures too.
- `com/eternal/base/data/ble/d.java` (`BleStatue`), **31–32, 116–123**: command counter starts at **-1**, increments before allocation, so first allocated sequence is **0**; wraps 65535 to 1. Replies echo sequence. The old library started at 1 and first allocated 2. There is no evidence the device enforces a particular initial number rather than echoing it.
- `defpackage/vd6.java:getModelData`, **212–213**: **command 01**, GET parameter IDs **10 02 03 20** (mode, sensor, fan, unit).
- Same class `getOffModelData` / `getOnModelData`, **237–242**: GET IDs **11 12** for off/on speeds.
- `BleController.java:n4.subscribe`, **2232–2245**, selects the legacy `vd6.getModelData` path for non-multiport type 6; modern home-appliance/multiport predicates select other builders. Type 6 is not type 48.
- `com/eternal/base/data/ble/y.java` (`SingleMessage`), **27–54**, gives the 70D51000/01/02 control UUIDs and returns its packet unchanged. `com/eternal/base/data/ble/c.java:write`, around **650–683**, passes packet bytes to the GATT writer and arms an 8s reply deadline. No type-6 encryption/header rewrite appears between builder and writer.

Thus **protocol revision 3 is not inserted into outgoing header byte 1**. A5 13 is a supported reply-family example, not evidence for A5 13 requests. Existing public references ([hunterjm APK notes](https://github.com/hunterjm/ac-infinity-ble/blob/main/docs/protocol-validation.md), [Nyx live app captures](https://github.com/Nyx000/acinfinity-ble/blob/main/docs/protocol/app-ble-capture-verified-2026-06.md)) agree, but the user-supplied current APK is the primary evidence here.

The read-only setup subset now sends:

1. `a5000004000070f9000110020320a0b7`: app model GET, first allocated sequence **0**.
2. If off/on speeds are missing, a command-01 **11 12** query at next allocated sequence **1**. Both speeds and the mode must validate before setup succeeds or saves the single `android_2_1_2_telink` profile in config data.

No speculative header/CRC/opcode/counter fallback scan is retained. The applicable app builder shows **one wire format**, not a protocol negotiation. Normal bounded read-only retries reconnect and repeat the same request after the readiness wait; they do not cycle invented variants. Runtime model readback uses the same app key list. Other model families retain their prior setup behavior.

### Clock/sync initialization — documented, intentionally not emitted

The app's normal main-screen flow `com/eternal/main/model/MainModel.java:syncTimeAndStart`, **1377–1413**, constructs a clock-sync stage before its refresh loop. `defpackage/hr1.java:syncTime`, **2761–2762**, delegates to `BleController.syncConnectedTime`. `BleController`, **5569–5583**, sends `syncTime` for types 1/2/**6**; **5599–5603** uses the legacy clock builder.

`vd6.setTimeData`, **1682–1702**, uses **command 03**, payload **01 07 YY MM DD weekday hh mm ss** for type 6. Weekday is ISO-style 1=Monday through 7=Sunday; year is years since 2000. If that is the first app command it consumes sequence 0, so a following read is sequence 1. Other UI paths and already-connected sessions can allocate different counters. There is no firmware-specific evidence here proving a query is rejected until clock sync occurs.

**Clock sync changes device state. The request explicitly forbids setting writes during probing/setup, so this patch DOES NOT send it, does not implement an automatic clock fallback, and does not consume a fake sequence to pretend it was sent.** This is an intentionally read-only subset of the app workflow, not an assertion that the full initialization is identical. If the timing/model-query corrections are insufficient, a capture of this exact T6's working app connection can establish whether clock sync is essential. Any automatic clock-setting experiment would require explicit authorization.

### Hardware transport facts and the BlueZ caveat

`ha_airtap_debug3.log` establishes CCCD `0100` after subscription and at timeout, acknowledged command writes, unchanged readable control value `00`, firmware `SDK_3.4.2\x00`, hardware `2.0`, software `2.2.14`, PnP `028a2466820100` (USB VID 248A/PID 8266/version 0001, Telink).

However, read-triggered `BLE RX bytes=00` **does not prove on-air ATT notification delivery**. Bleak's [BlueZ client](https://github.com/hbldh/bleak/blob/develop/bleak/backends/bluezdbus/client.py) routes characteristic D-Bus Value changes to its notification callback; the [manager](https://github.com/hbldh/bleak/blob/develop/bleak/backends/bluezdbus/manager.py) dispatches those changes regardless of their origin. The installed source confirms this path. CCCD proves subscription configuration, not by itself an end-to-end on-air notification. Only an ATT/HCI trace settles that distinction.

Fix.3 disables control-characteristic READ snapshots during type-6 setup so cached A5 values/read-induced callbacks cannot counterfeit query success. Metadata/PnP/CCCD diagnostics remain; callbacks during explicit diagnostic reads are labeled, but that label is a timing annotation, not authoritative ATT PDU classification. No OTA endpoint is read, written or subscribed; no new security or revision/GAP subscriptions are added.

### Confidence and next log

**High confidence:** the app's type-6 Telink delay, frame construction, model/speed query facts, sequence allocation, clock-write format and BlueZ callback caveat. **Moderate confidence** that the missing readiness delay explains this vent's silence: the failed writes were demonstrably too early compared with the app, but no successful HA/device reply has yet been captured. Do not call the hardware fixed before testing.

Look for, on every new type-6 connection:

1. `BLE Telink readiness wait start ... delay=1.5s`, then `wait completed elapsed=...` **before the first control TX**.
2. `BLE app protocol profile=android_2_1_2_telink header=a500 command=01 keys=10020320` and the exact first frame above.
3. Raw `BLE RX`, ideally a matching `a513...` or other valid A5-family reply. It must echo sequence/command and pass both CRCs, length and mode TLV checks. The whole command word is validated, not only its low byte.
4. If needed, `BLE app follow-up query off/on speed keys=1112`, then both `BLE protocol variant validated and selected for config entry` and `BLE setup validated`.
5. On failure, the specific step/code and traceback: `Telink readiness`, `connect`, `notification subscription`, `write`, `response wait` or `setup validation`, rather than a generic cannot_connect. Failures/cancellation clean up in finally blocks; warning-once streaks and bounded retries remain.

If still silent after the confirmed wait, collect the full HA log and a payload-bearing **official-app connect capture on this exact T6 / software 2.2.14**, including any first clock command, ATT responses/notifications and SMP security exchanges. Ideally capture HA's attempt too. Confirm the app actually controls this vent, and distinguish real packet payloads from Android's filtered HCI logs. That will identify any genuinely mandatory initialization without guessing or silently changing settings.
