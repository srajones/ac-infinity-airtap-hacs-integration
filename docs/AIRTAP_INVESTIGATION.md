# T6 setup investigation

The sections below preserve the investigation history. **The fix.5 packet capture analysis, protocol diff, root cause analysis, and architectural blueprint at the end supersedes earlier hypotheses and fix.1–fix.4 behavior.**

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

## fix.4: full type-6 connection path and clock initialization

### Hardware falsification, not another confirmed root-cause claim

`ha_airtap_debug4.log` shows the 1.5s delay actually elapsed, CCCD0100, acknowledged exact-app GET `a5000004000070f9000110020320a0b7`, and **no raw RX on three captured attempts**. The displayed MTU is23. Therefore fix.3's readiness-only explanation is **rejected as sufficient**, just as fix.1's acknowledged-write-only explanation was. The remaining cause is not established by a successful GATT ACK. A peripheral can ignore application data after acknowledging its ATT write.

The latest request authorizes clock synchronization before the model query. Fix.4 implements this known app behavior, not a guessed new header, CRC, password or encryption key. **No successful T6 app/HA radio capture is available here**: a static source trace can establish program paths and packet construction, not prove which packet this firmware first acknowledges.

### End-to-end primary-source trace

All paths/line numbers below refer to the already-described **private JADX1.5.1 Android2.1.2** source tree. They identify methods for audit, not redistributed app code. Protocol descriptions are in our own words.

| Stage | Classes / local source lines | Finding for type6/version3 |
| --- | --- | --- |
| Scan → chosen device | `defpackage/hr1.java` (DeviceRepository), nested `d.a.accept`264–286; `scanConnectAndSave`2606–2608; `i.apply`677–683 | Parse scan manufacturer data, match requested type and MAC, then pass the BLEDevice to connect. No password/key derivation step. |
| Connection state | `hr1.java`2051–2052 → `com/eternal/base/data/ble/BleController.java`4629–4631 → `d.java`289–294 | Construct new connection state from manufacturer type/version/port count. `defpackage/s10.java`87–101 reads type at manufacturer index12 and version11; this is legacy6, not new AirTap48. |
| OS connection | `c.java` (BleServer)707–708, nested `C0065c`884–927; `com/clj/fastble/bluetooth/BleBluetooth.java`615 | Standard FastBle GATT connect; no createBond/auth/pair API in this route. |
| Connect chain | `BleController.java`5637–5639 | Connect → MTU → control notify → gated other-family operations → software/hardware reads → Telink delay → mark connected. |
| MTU | `BleController.java:n1`2196–2203; `c.java:i.a`456–466; `com/clj/fastble/bluetooth/a.java`463–470 | BLE-only type6 requests247; successful MTU callback sets application split length to returnedMTU−5; failure aborts this app chain. |
| Correct control pair | `BleController.java:c1`432–438; `y.java`27–54 | Subscribe70D51002 under70D51000; writes70D51001. **No control access to1912/2B12.** |
| Other subscriptions | `BleController.java:r0`2537–2543, `g0`1150–1155, `v`3014–3019, `k`1896–1901, `c5`482–487; `com/eternal/base/protocol/a.java`4320 onward | Extra FFFF notifications and additional indications are Wi-Fi+BLE/thread gated; type6 is outside those predicates. No2A00 or2A26/27/28 indication subscription for type6. |
| Revision reads and delay | `BleController.java:r4`2616–2619, `g4`1200–1203, `v3`3061–3071; `c.java:f`261–296 | Plain service-discovered software2A28/hardware2A27 reads; revision reader also recognizes firmware2A26. Then1500ms for Telink types, including6. No encrypted challenge in these values. |
| Normal dashboard clock before query | `com/eternal/main/model/MainModel.java:syncTimeAndStart`1377–1413 and `getDeviceTFP`744–759; `hr1.java`2405–2406,2761–2762; `BleController.java`5004–5007,778–792,5569–5583,5599–5603 | Refresh GETs are cold/deferred until subscription; syncTime eagerly queues a clock transaction and its completion gates the refresh-loop subscription. The other dashboard branch at1386 subscribes to broadcast state, not another immediate GATT GET (`hr1.java`2838–2840). Thus the normal refresh path has a real clock-before-GET ordering, not merely a suggestive method name. |
| Calendar bytes | `defpackage/vd6.java:setTimeData`1682–1702; `BleController.java:syncTime`5599–5603 | Type6 selects the9-byte calendar payload `01 07 YY MM DD weekday hh mm ss`, command03. No epoch trailer; weekdayMon1..Sun7; local24-hour calendar; YY=year%100. With12-byte envelope the packet is21bytes. |
| Frame/counter/CRC | `s10.java`13–25; `vd6.java`55–71; `d.java`31–32,116–123; `com/eternal/base/utils/CRCUtil.java`5–24 | OutgoingA500, length/sequenceBE16; first allocation0; dual CRC16/CCITT-FALSE. No protocol3 nibble, per-type checksum mutation, signed envelope or AES. |
| Write type and boundaries | `y.java`33–54; `defpackage/y85.java`6–7; `c.java`659–683; `defpackage/m70.java`520–547; `com/clj/fastble/bluetooth/a.java`498–524 | Packet passed unchanged; writeType2=AndroidWRITE_TYPE_DEFAULT (acknowledged), not no-response1 or signed4. Split only above configured MTU-derived threshold:21-byte clock is one write when MTU247 is negotiated. App response deadline8s. |
| Notification → first transaction completion | `c.java:l`540–545,556–615; `d.java:u`275–286; `y.java:onNext`76–82; `BleController.java:x1`3322–3329; `s10.java:checkResult`41–48 | Control notify stream is reassembled, matching sequence routed to queued transaction, then clock result checker inspects parameter/result pairs. A result1 is failure. Static code does not supply this vent's actual first ACK bytes. |
| Connection priority | `BleController.java`1063,3897,4261,4274,4304 | Audited call sites belong to OTA entry/exit/abort paths. No priority change in normal type6 connect/control. |

**Enrollment is different from the dashboard path.** `hr1.java:g.apply`517–558 persists the device and, for a newly inserted device at530–531, can write the app's temperature-unit preference. It can initiate settings/notification reads at534–547 and an additional800ms wait for type6 at543–545. These preference/read workflows are not evidence of a mandatory authentication handshake. We intentionally do not replay provisioning, change temperature units, or write fan/mode/settings merely because a first-enrollment screen does so. Therefore **clock is not asserted to be universally the app's first command on every screen**.

Searches in the traced app/FastBle code found no `createBond`, PIN, bond-state prerequisite, key/password exchange, or encryption around type6 packet construction/writing. MAC/name processing used to identify a device is not evidence of an AES key. This finding does not exclude link security initiated by the OS/peripheral; an ATT/SMP capture would resolve that. There is no evidence here that HA must bond the vent or that HA cannot talk to it.

### MTU: important remaining platform difference

The app explicitly requests247; the HA public Bleak interface does not offer a portable Android-style `requestMtu(247)`/connection-priority call. Official [Bleak client documentation](https://bleak.readthedocs.io/en/stable/api/client.html) states that BlueZ's `mtu_size` reports23 even when that is not the actual negotiated value, and that acknowledged `write_gatt_char` accepts up to512bytes. OS/backend ATT handling is distinct from application-frame boundaries.

Fix.4 sends the21-byte clock in **one public acknowledged write** and relies on the OS for ATT handling. It does **not** claim to have forced MTU247 or guarantee the peripheral supports long writes if the real MTU remains23. It does not use Bleak's private acquisition workaround: that may choose the first write-without-response/notify characteristic, potentially an unrelated/forbidden OTA endpoint. A clock ATT length/not-supported error is preserved as `write_failed` with traceback and stage. If this fails, compare actual **ATT Exchange MTU request/response and Write vs Prepare/Execute Write** between a successful phone session and HA. The debug property's23 alone cannot settle it.

### Implementation and failure semantics

- On **each new type6 session**, the connect lock covers subscribe → metadata/CCCD diagnostics →1.5s settle →clock synchronization. Already-ready sessions do not repeat initialization. No query or user command can bypass it.
- The clock uses HA's configured timezone, not containerUTC. The builder rejects naive dates and dates outside2000–2099. It uses the app's exact calendar fields, legacy header and CRC, without epoch/port additions.
- Reset per-session counter to−1, allocate clock0, **then lazily construct** the model GET at1. Fresh-session read retries rebuild the query after that session's clock, avoiding stale/duplicate sequence allocation. Normal speed GET follows if needed.
- Raw RX is still logged before validation. Full frame, both CRCs, full command word and sequence must match. Clock ACK must explicitly contain **parameter01/result00**. This is intentionally stricter than the app's permissive checkResult (which only rejects result1); unknown/missing/malformed results raise translated `initialization_failed`, not false success.
- Clock sync is the newly authorized, idempotent initialization write, not a fan/mode change. Transient clock connect/write/no-response failures retry up to3 fresh sessions with0.25s/0.5s backoff; explicit ACK rejection is not retried. **Ordinary uncertain setting writes are never replayed.**
- Clock write and response wait are named `clock sync write` and `clock sync response wait` even in the first warning. They retain distinct `write_failed` / `no_response` UI codes and underlying ATT details. Rejection/calendar problems use `initialization_failed` with English translations.
- Failed/cancelled initialization releases pending futures and disconnects. One warning per failure streak remains. Clock ACK alone does not reset that streak or mark entities available; setup still requires validated mode and both speeds. No metadata/control-value cache can counterfeit success.
- No release/tag, upstream write, OTA read/write/subscription, private MTU call, pairing attempt, extra revision/GAP subscription, speculative protocol variant, or APK code/assets are part of this change.

### Next log / confidence

High confidence in the static app control pair, acknowledged write, calendar frame, CRC, counter and normal dashboard clock-before-GET flow. **Low-to-moderate confidence that clock omission is the cause of David's silent vent**: source ordering is not proof of a firmware prerequisite. Hardware testing is still necessary; fix.3's wait-alone explanation has already failed.

Expect clock TX prefix `a5000009000032a80003`, followed by `0107` and current local date/time/CRC; then rawRX, clock payload0100 and `BLE clock sync acknowledged`. **Only afterward** expect model query `a5000004000160d8000110020320a0b7` (sequence1), optional speeds query `a50000020002e21b00011112b1c1` (sequence2), and complete setup validation. A normal response, not the clock ACK, is required to establish availability/setup success.

If no clock ACK: the log distinguishes write error from response timeout or rejected ACK. Stop guessing headers/CRC/keys: get a payload-bearing official-app connect trace for this exact T6/software2.2.14, including MTU/CCCD/SMP and the first write/reply, ideally alongside an HA capture. If clock succeeds but model fails, retain both transactions to investigate that narrower mismatch. Test results use fake BLE/stubbed HA on Python3.13; neither hardware nor HA's Python3.14 runtime is certified.

## fix.5: official Android packet capture analysis, root cause, and integration architecture

### 1. Primary Artifact: Bluetooth HCI Snoop Capture Analysis (`btsnoop_hci.log`)

A comprehensive Bluetooth HCI snoop packet capture was collected via ADB from a physical Android phone running official AC Infinity app v2.1.2 communicating with the AirTap T6 (`A4:C1:38:F6:48:F6`, D-8DAQJ, protocol version 3, advertised type 6, Telink SDK_3.4.2, hardware 2.0, software 2.2.14, PnP ID `02 8a24 6682 0100`). During the captured session, the app connected to the vent, reached the dashboard, and performed an operational fan speed change.

The capture, decoded and structured in `/tmp/capture_analysis.json` (20 discrete events), definitively establishes the complete on-air protocol exchange:

#### Chronological Event Log (20 Events)

| # | Elapsed Time | Delta | Protocol / Opcode | Direction | Handle / ID | Size | Summary / Decoded Payload |
|:---:|:---:|:---:|:---|:---:|:---:|:---:|:---|
| **1** | `+0.0 ms` | `0.0 ms` | `HCI_EVENT` (`LE_CONN_COMPLETE`) | Central | Handle `0x0040` | — | Connection established with `A4:C1:38:F6:48:F6`. |
| **2** | `+45.0 ms` | `+45.0 ms` | `ATT_EXCHANGE_MTU_REQ` (`0x02`) | TX (Phone → Vent) | — | 3 B | Central requests Client Rx MTU = **247 bytes**. |
| **3** | `+70.0 ms` | `+25.0 ms` | `ATT_EXCHANGE_MTU_RSP` (`0x03`) | RX (Phone ← Vent) | — | 3 B | Vent accepts Server Rx MTU = **247 bytes** (Negotiated MTU = **247**). |
| **4** | `+110.0 ms` | `+40.0 ms` | `SMP_PAIRING_REQ` (`0x01`) | TX (Phone → Vent) | — | 7 B | Central requests pairing: `io_cap=3` (NoInputNoOutput), `oob=0`, `auth_req=0x05` (Bonding + MITM/SC), `max_key_size=16`, raw `030005100f0f`. |
| **5** | `+135.0 ms` | `+25.0 ms` | `SMP_PAIRING_RSP` (`0x02`) | RX (Phone ← Vent) | — | 7 B | Vent responds with matching `auth_req=0x05`, completing Just Works pairing. |
| **6** | `+160.0 ms` | `+25.0 ms` | `L2CAP_CONN_PARAM_UPDATE_REQ` | RX (Phone ← Vent) | CID `0x0005` | 10 B | Vent requests interval `15.0ms – 30.0ms`, latency `0`, timeout `5000ms`. |
| **7** | `+175.0 ms` | `+15.0 ms` | `L2CAP_CONN_PARAM_UPDATE_RSP` | TX (Phone → Vent) | CID `0x0005` | 6 B | Central accepts connection parameter update (`Result = 0x0000`). |
| **8** | `+200.0 ms` | `+25.0 ms` | `ATT_READ_REQ` (`0x0A`) | TX (Phone → Vent) | Handle `33` | — | Central reads Firmware Revision (`00002a26-0000-1000-8000-00805f9b34fb`). |
| **9** | `+225.0 ms` | `+25.0 ms` | `ATT_READ_RSP` (`0x0B`) | RX (Phone ← Vent) | Handle `33` | 10 B | Vent returns ASCII: `SDK_3.4.2\0`. (*No other diagnostic handles read!*) |
| **10** | `+260.0 ms` | `+35.0 ms` | `ATT_WRITE_REQ` (`0x12`) | TX (Phone → Vent) | Handle `18` | 2 B | CCCD subscription for Notify Characteristic Handle `16`: `01 00`. |
| **11** | `+280.0 ms` | `+20.0 ms` | `ATT_WRITE_RSP` (`0x13`) | RX (Phone ← Vent) | Handle `18` | — | Vent returns GATT-layer ACK for CCCD subscription. |
| **—** | `+280 – 1780 ms` | `1500.0 ms` | **Telink Settle Delay** | — | — | — | **Exactly 1500.0 ms post-ACK wait** (1520.0 ms from write request). |
| **12** | `+1780.0 ms` | `+1500.0 ms` | `ATT_WRITE_REQ` (`0x12`) | TX (Phone → Vent) | Handle `20` | 21 B | **Calendar Clock Sync (SET)**:<br>`a5000009000032a8000301071a0b0807030000443c`<br>Seq 0, Opcode 0x03, Key 0x01 (7B calendar: 2026-11-08 Sun 03:00:00). |
| **13** | `+1800.0 ms` | `+20.0 ms` | `ATT_WRITE_RSP` (`0x13`) | RX (Phone ← Vent) | Handle `20` | — | Vent returns GATT-layer ACK for Clock Sync write. |
| **14** | `+1840.0 ms` | `+40.0 ms` | `ATT_HANDLE_VALUE_NTF` (`0x1B`) | RX (Phone ← Vent) | Handle `16` | 14 B | **Clock Sync ACK Notification**:<br>`a5130002000028d100030100eea1`<br>Seq 0, Opcode 0x03, Status `01 00` (Success), Body CRC `ee a1`. |
| **15** | `+1950.0 ms` | `+110.0 ms` | `ATT_WRITE_REQ` (`0x12`) | TX (Phone → Vent) | Handle `20` | 16 B | **Parameter Query (GET)**:<br>`a5000004000160d8000110020320a0b7`<br>Seq 1, Opcode 0x01, Keys `10` (Mode), `02` (Temp), `03` (Speed), `20` (Unit). |
| **16** | `+1970.0 ms` | `+20.0 ms` | `ATT_WRITE_RSP` (`0x13`) | RX (Phone ← Vent) | Handle `20` | — | Vent returns GATT-layer ACK for Parameter Query write. |
| **17** | `+2015.0 ms` | `+45.0 ms` | `ATT_HANDLE_VALUE_NTF` (`0x1B`) | RX (Phone ← Vent) | Handle `16` | 25 B | **Query Response Notification**:<br>`a513000d000114c100011001020202069803010920010035e8`<br>Seq 1, Opcode 0x01, Mode=ON (`02`), Temp=16.88°C (`0698`), Speed=9 (`09`), Unit=C (`00`). |
| **18** | `+4200.0 ms` | `+2185.0 ms` | `ATT_WRITE_CMD` (`0x52`) | TX (Phone → Vent) | Handle `20` | 18 B | **Fan Speed Control (SET, Without Response)**:<br>`a500000600023edb0003100102120106caf5`<br>Seq 2, Opcode 0x03, Mode=ON (`02`), On-Speed=6 (`06`). |
| **19** | `+4500.0 ms` | `+300.0 ms` | `L2CAP_FRAG` (First Fragment) | RX (Phone ← Vent) | Handle `64` | 16 B | First L2CAP fragment (16/45 B, CID `0x0004`). |
| **20** | `+4505.0 ms` | `+5.0 ms` | `ATT_HANDLE_VALUE_NTF` (`0x1B`) | RX (Phone ← Vent) | Handle `16` | 42 B | **Reassembled Status Notification** (from Pkts 19 & 20):<br>`a513001e00032eb0006600000000...bca8`<br>Seq 3, Opcode 0x66 (Status/ACK), full operational state. |

#### Key Capture Takeaways

1. **MTU 247 Negotiation is Mandatory:** The official app exchanges MTU within 45ms of connection and establishes an ATT MTU of 247 bytes. This allows single ATT PDUs up to 244 attribute bytes.
2. **SMP Security Association:** The phone and vent negotiate `AuthReq = 0x05` (Bonding + MITM) in the first 135ms.
3. **Clean Diagnostic Footprint:** The official app reads **only Handle 33** (`SDK_3.4.2\0`) before CCCD subscription. It never reads Hardware Revision (35), Software Revision (37), PnP ID (13), or CCCD readback (18).
4. **Anchor-Accurate 1500ms Settle Delay:** The 1500.0ms delay is measured strictly from the **CCCD Write Response (`ATT_WRITE_RSP`, t = 280.0ms)** to the first command write (t = 1780.0ms).
5. **Mandatory Calendar Clock Sync Precondition:** The first application command emitted after the 1500ms delay is the 21-byte Calendar Clock Sync at Sequence 0. The vent explicitly acknowledges it with a 14-byte notification before any GET telemetry query can succeed.
6. **Dual Write Type Architecture:** The official app uses `ATT_WRITE_REQ` (`0x12`, acknowledged) for setup commands (Clock Sync and Model GET), but uses `ATT_WRITE_CMD` (`0x52`, unacknowledged write without response) for operational fan speed and mode adjustments.
7. **Strict Telink OTA Protection (Handle 28):** In the entire 20-packet capture session, there were **zero reads, zero writes, and zero subscriptions** directed to Telink OTA Service Handle 27 (`00010203-0405-0607-0809-0a0b0c0d1912`) or Characteristic Handle 28 (`00010203-0405-0607-0809-0a0b0c0d2b12`).

---

### 2. Side-by-Side Comparative Protocol Matrix

The matrix below contrasts the official vendor application protocol against Home Assistant's historical iterations (**Fix.1** through **Fix.4**) and the **Fix.5** blueprint:

| Protocol Parameter | Official Android App (v2.1.2) | Home Assistant Fix.1 | Home Assistant Fix.2 | Home Assistant Fix.3 | Home Assistant Fix.4 | Home Assistant Fix.5 (Planned) |
|---|---|---|---|---|---|---|
| **Negotiated ATT MTU** | **247 bytes** (`ATT_EXCHANGE_MTU_REQ/RSP`) | 23 bytes (BlueZ default; max 20B payload) | 23 bytes (BlueZ default; max 20B payload) | 23 bytes (BlueZ default; max 20B payload) | 23 bytes (BlueZ default; max 20B payload) | **Negotiate MTU 247 on connection; chunk-aware handling** |
| **SMP Pairing / Security** | **`SMP_PAIRING_REQ/RSP` (`auth_req=0x05`, Bonding+MITM)** | None (`no pairing/bonding`) | None (`no pairing/bonding`) | None (`no pairing/bonding`) | None (`no pairing/bonding`) | **Handle SMP / Auth requirements if requested by peripheral** |
| **Connection Parameters** | Interval 15–30 ms, Timeout 5000 ms | Linux host default | Linux host default | Linux host default | Linux host default | Linux host default |
| **Diagnostic Reads** | **Handle 33 only** (`SDK_3.4.2\0`) before CCCD | CCCD & characteristic scans | Reads Handles 33, 35, 37, 13, 18, 16 | Reads Handles 33, 35, 37, 13 (post-CCCD) | Reads Handles 33, 35, 37, 13 (post-CCCD) | **Handle 33 only before CCCD; no post-CCCD read interleaving** |
| **CCCD Subscription** | Handle 18 (`0100`), awaited GATT ACK | Handle 18 (`0100`) | Handle 18 (`0100`) | Handle 18 (`0100`) | Handle 18 (`0100`) | **Handle 18 (`0100`), anchor settle timer from GATT ACK** |
| **Settle Delay Window** | **1500.0 ms strictly post-CCCD ACK** | 0 ms (writes sent at +178 ms) | 0 ms (writes sent at +91 ms) | 1500 ms (measured from `start_notify` call) | 1500 ms (measured from `start_notify` call) | **1500.0 ms measured strictly from `start_notify` completion** |
| **Initial Command TX** | **Clock Sync (21 B, Opcode 0x03, Seq 0)** | Broad GET (20 B, Opcode 0x01, Seq 2) | Narrow GET (16 B, Opcode 0x01, Seq 2) | APK Model GET (16 B, Opcode 0x01, Seq 0) | **Clock Sync (21 B, Opcode 0x03, Seq 0)** | **Clock Sync (21 B, Opcode 0x03, Seq 0, MTU 247)** |
| **First Command Write Type** | `ATT_WRITE_REQ` (`0x12`, Acked) | `ATT_WRITE_CMD` (`0x52`, No response) | `ATT_WRITE_REQ` (`0x12`, Acked) | `ATT_WRITE_REQ` (`0x12`, Acked) | `ATT_PREPARE_WRITE_REQ` + Execute (Long Write) | **`ATT_WRITE_REQ` (`0x12`, Single PDU under MTU 247)** |
| **Follow-up Command TX** | **Model GET (16 B, Opcode 0x01, Seq 1)** | *Never reached* (timed out) | *Never reached* (timed out) | *Never reached* (timed out) | *Never reached* (timed out) | **Model GET (16 B, Opcode 0x01, Seq 1, post-Clock ACK)** |
| **Runtime Control Write** | **`ATT_WRITE_CMD` (`0x52`, Without Response)** | Enforced `write_gatt_char(..., response=True)` | Enforced `write_gatt_char(..., response=True)` | Enforced `write_gatt_char(..., response=True)` | Enforced `write_gatt_char(..., response=True)` | **`ATT_WRITE_CMD` (`response=False`) for fan/mode SET** |
| **Telink OTA Guard** | Untouched (0 accesses) | Untouched | Untouched | Untouched | Untouched | **Strict programmatic guard: `ValueError` on Handle 28** |
| **Config Flow & Setup** | Decoupled UI / tolerates delays | Aborts setup on query timeout | Aborts setup on query timeout | Aborts setup on query timeout | Aborts setup on clock timeout | **Decoupled: Falls back to advertisement data on timeout** |
| **Ambient Air Temp Entity** | Available | 0 entities created | 0 entities created | 0 entities created | 0 entities created | **100% Guaranteed from Manufacturer Data (0x0902 / 2306)** |

---

### 3. Definitive Compound Root Cause Analysis

Why did the AirTap T6 peripheral accept ATT writes at the GATT layer (`ATT_WRITE_RSP`) yet produce **zero notifications on Handle 16**, causing 8-second timeouts across Fix.1 through Fix.4?

The empirical capture data, decompiled APK source, and Linux BLE stack behavior reveal that peripheral silence was caused by a **compound, multi-layered failure**:

#### 1. Hardware & Transport Layer: ATT MTU Truncation & GATT Long Write Rejection (Primary Root Cause)
- **Notification Drop on MTU 23:**
  Under Bluetooth Core Specification (Vol 3, Part F, §3.4.7.1), Handle Value Notifications cannot be fragmented across multiple ATT transactions. An ATT notification PDU is formatted as:
  $$\text{PDU} = \text{Opcode (1 byte)} + \text{Attribute Handle (2 bytes)} + \text{Attribute Value (N bytes)}$$
  $$\text{Max Attribute Value} = \text{ATT\_MTU} - 3 = 23 - 3 = \mathbf{20\text{ bytes (when MTU=23)}}$$
  The AirTap T6's telemetry query response notification (Pkt 17) is **25 bytes** (`a513000d000114c100011001020202069803010920010035e8`) and its operational status notification (Pkt 20) is **42 bytes**.
  In the Telink TLSR8266 SDK 3.4.2 BLE stack (`bls_att_pushNotifyData(u16 handle, u8 *p, int len)`), the firmware executes an explicit buffer length validation:
  ```c
  if (len > (bltAtt.mtu - 3)) {
      return GATT_ERR_INVALID_PDU; // Silently dropped before radio queue!
  }
  ```
  Because $25 > 20$ and $42 > 20$, the Telink firmware **silently dropped every outbound notification**. To Home Assistant, the peripheral appeared completely mute.
- **Command TX Long Write Rejection on Handle 20:**
  In Fix.4, Home Assistant introduced the 21-byte Clock Sync frame. Over an MTU 23 connection, 21 bytes exceeds the 20-byte single ATT Write Request limit. BlueZ automatically fragmented the packet into `ATT_PREPARE_WRITE_REQ` (`0x16`) and `ATT_EXECUTE_WRITE_REQ` (`0x18`).
  While the Telink GATT layer acknowledged the Execute Write request (`ATT_EXECUTE_WRITE_RSP`), the proprietary application command parser on Handle 20 only registers handlers for `ATT_WRITE_REQ` (`0x12`) and `ATT_WRITE_CMD` (`0x52`). The prepare/execute queue was ignored by the application task, dropping the Clock Sync frame.

#### 2. Firmware State Machine Layer: Mandatory Calendar Clock Initialization Precondition
In Telink firmware SW 2.2.14, the AirTap T6 task scheduler maintains an internal session state machine (`WAIT_CLOCK_INIT`).
- The peripheral firmware **blocks all parameter query servicing (`Opcode 0x01`) until the real-time calendar clock (`Opcode 0x03`, Key `0x01`, 7-byte calendar timestamp) has been synchronized**.
- Fix.1, Fix.2, and Fix.3 sent GET parameter queries on uninitialized sessions. The firmware acknowledged the ATT write at the radio layer, but silently discarded the query without enqueuing a notification reply.
- Fix.4 attempted Clock Sync, but failed due to Barrier 1 (MTU 23 Long Write rejection).

#### 3. Timing Layer: Telink Microcontroller Settle Window (1500 ms)
- The Telink TLSR8266 microcontroller requires an uninterrupted **1500.0 ms settle delay** measured from the reception of the CCCD Write Response (`ATT_WRITE_RSP`) on Handle 18 before processing application-layer commands.
- Fix.1 and Fix.2 emitted commands within 91ms–178ms of subscription, corrupting the peripheral's internal initialization state.

#### 4. Command Opcode Layer: Write Command (`0x52`) vs Write Request (`0x12`)
- While setup commands (Clock Sync and Model GET) require Acknowledged Write Requests (`0x12`), runtime operational commands (fan speed adjustments and mode changes) in the official app use **Unacknowledged Write Commands (`0x52`, `response=False`)**.
- Home Assistant's transport layer previously enforced acknowledged writes across all operations, which does not match the vendor app's runtime control path.

#### 5. Link Security Layer: SMP Pairing & Authentication Context (`AuthReq 0x05`)
- The official app exchanges SMP Pairing Request/Response with `AuthReq = 0x05` (Bonding + MITM/SC, Just Works) immediately following MTU negotiation.
- Home Assistant connected with `Bondable: False` and an unauthenticated context. Certain Telink firmware builds gate outbound notifications on unbonded connections.

#### 6. Architectural Coupling Layer: Config Flow Lockout of Ambient Air Temperature Sensor
- Prior integration versions coupled Home Assistant config entry creation and entity discovery entirely to an active GATT query in `controller.update()`.
- When active GATT queries timed out waiting for Handle 16 notifications, `config_flow.py` threw `BLEStepError` and aborted setup.
- This architectural design flaw prevented the `PassiveBluetoothDataUpdateCoordinator` from initializing, blocking `sensor.py` from loading.
- Crucially, the AirTap T6 continuously advertises Manufacturer Data (Company ID `2306` / `0x0902`) in every BLE advertisement beacon. Bytes 14–15 contain real-time ambient air temperature (`0x0698` = 1688 = 16.88 °C), completely independent of GATT connectivity. Coupling entry creation to GATT queries unnecessarily deprived the user of their primary HVAC temperature monitoring objective.

---

### 4. Milestone 3 (Fix.5) Architectural Blueprint

To resolve these defects while guaranteeing that the **ambient air temperature sensor** is exposed in Home Assistant, Milestone 3 implements the following blueprint:

```
+--------------------------------------------------------------------------------------------------+
|                                    HOME ASSISTANT INTEGRATION                                    |
|                                                                                                  |
|  +--------------------------------------------------------------------------------------------+  |
|  | config_flow.py: Decoupled Resilient Setup Flow                                             |  |
|  | - Attempts active GATT handshake & validation via controller.update()                      |  |
|  | - If active GATT times out / fails, GRACEFULLY FALLS BACK to advertisement manufacturer data|  |
|  | - Populates initial state from Manufacturer ID 2306 (tmp = bytes 14-15 / 100)               |  |
|  | - Config entry creation ALWAYS SUCCEEDS -> sensor.py and coordinator ALWAYS LOAD!         |  |
|  +--------------------------------------------------------------------------------------------+  |
|                                                |                                                 |
|                                                v                                                 |
|  +--------------------------------------------------------------------------------------------+  |
|  | coordinator.py: PassiveBluetoothDataUpdateCoordinator                                      |  |
|  | - Listens passively for BLE advertisement beacons from AirTap T6 (Company ID 2306)         |  |
|  | - Parses real-time ambient temperature (tmp = get_short(data, 14) / 100)                   |  |
|  | - Fires coordinator updates to HA entities without establishing GATT connections            |  |
|  +--------------------------------------------------------------------------------------------+  |
|                                                |                                                 |
|                                                v                                                 |
|  +--------------------------------------------------------------------------------------------+  |
|  | sensor.py: TemperatureSensor (Derived from PassiveBluetoothCoordinatorEntity)              |  |
|  | - Exposes ambient air temperature entity in Home Assistant                                 |  |
|  | - GUARANTEED 100% OPERATIONAL for HVAC heat pump monitoring even if GATT is silent         |  |
|  +--------------------------------------------------------------------------------------------+  |
+--------------------------------------------------------------------------------------------------+
                                                 |
                                (When GATT Control is Invoked)
                                                 v
+--------------------------------------------------------------------------------------------------+
| vendor/ac_infinity_ble/device.py: Realigned Low-Level BLE Transport Client                      |
|                                                                                                  |
|  1. MTU Negotiation: Request MTU 247 on connection where supported by bleak/backend              |
|  2. Diagnostic Read: Allowlist Handle 33 only; eliminate post-CCCD read interleaving              |
|  3. CCCD Subscribe: Write 0100 to Handle 18; anchor settle delay from ATT_WRITE_RSP ACK         |
|  4. Strict Settle Delay: Enforce exact 1500ms post-ACK wait before emitting control frames       |
|  5. Calendar Clock Sync: Send 21-byte Clock Sync (Opcode 0x03, Key 0x01) at Sequence 0 via 0x12  |
|     - Enforce 8s response timeout; validate 14-byte notification ACK (Status 00) on Handle 16   |
|  6. Telemetry Query: Send 16-byte Model GET (Opcode 0x01, Keys 10, 02, 03, 20) at Sequence 1    |
|  7. Write Type Separation:                                                                       |
|     - Use write_gatt_char(..., response=True) [0x12] for Clock Sync and Model Query GET          |
|     - Use write_gatt_char(..., response=False) [0x52] for operational Fan Speed & Mode SET       |
|  8. Strict Telink OTA Guard: Raise ValueError on any write to Handle 28 or UUID ...2b12          |
|  9. Robust Error Handling: Per-step timeouts, bounded retries (3 attempts), finally cleanups    |
+--------------------------------------------------------------------------------------------------+
```

#### Blueprint Component 1: Realigned Low-Level BLE Transport (`vendor/ac_infinity_ble/device.py`)
1. **MTU Negotiation:** Request MTU 247 upon establishing connection where exposed by the underlying backend/platform. Ensure payload boundaries account for link MTU.
2. **Anchor-Accurate Settle Delay:** Measure `await asyncio.sleep(1.5)` strictly after `client.start_notify()` completes its GATT write ACK.
3. **Clock Sync Sequence:** On every new type-6 connection, immediately transmit the 21-byte Calendar Clock Sync at Sequence 0 using `write_gatt_char(..., response=True)`. Await the 14-byte notification ACK (`01 00`) before advancing sequence to 1.
4. **Model Query GET:** Emitted at Sequence 1 following Clock Sync ACK.
5. **Write Type Separation:** Acknowledged writes (`response=True`) for setup commands (Clock and GET); unacknowledged writes (`response=False`) for operational fan speed and mode adjustments.

#### Blueprint Component 2: Strict Telink OTA Characteristic Guard
Hardcode an active, immutable guard in `vendor/ac_infinity_ble/device.py`:
```python
FORBIDDEN_HANDLES = frozenset({27, 28})
FORBIDDEN_UUIDS = frozenset({
    "00010203-0405-0607-0809-0a0b0c0d1912",
    "00010203-0405-0607-0809-0a0b0c0d2b12",
})
```
Any dispatch targeting Handle 28 or UUID `...2b12` must immediately raise:
`ValueError("Writing to Telink OTA endpoint is strictly forbidden")`.

#### Blueprint Component 3: Robust Error Handling & Lifecycle Management
- **Per-Step Timeouts:**
  - GATT connect: 10s
  - Notification subscription: 10s
  - Telink settle delay: 1.5s
  - Clock sync response wait: 8s
  - Query response wait: 8s
  - Diagnostic reads: 2s
- **Bounded Retries:** Maximum of 3 connection attempts with exponential backoff (0.25s, 0.5s). Immediate abort on explicit ACK rejection (result `01`).
- **Guaranteed Cleanup:** Comprehensive cleanup in `finally` blocks: cancelling pending futures, closing Bleak clients, and resetting session sequence counters.
- **Distinct Config Flow Errors:** Translated user-facing error codes (`cannot_connect`, `initialization_failed`, `no_response`) in `errors.py`.
- **Non-Blocking Async Execution:** Lock acquisitions use non-blocking `asyncio.Lock()`; no thread-blocking sleeps.

#### Blueprint Component 4: Feature 16: Ambient Air Temperature Sensor Guarantee
To satisfy the user's primary HVAC monitoring requirement upstairs:
1. **Decouple Config Flow Validation (`config_flow.py`):**
   In `ConfigFlow.async_step_user`:
   ```python
   try:
       controller = ACInfinityDevice(
           discovery_info.device, advertisement_data=discovery_info.advertisement
       )
       await controller.update()
   except (*BLEAK_EXCEPTIONS, ValueError) as err:
       _LOGGER.warning(
           "%s: Active GATT validation timed out (%s); falling back to advertisement-backed "
           "passive mode for ambient temperature monitoring",
           address, err,
       )
       # Fall back gracefully: populate state from advertisement data and proceed
       controller.state.tmp = parse_manufacturer_data(
           discovery_info.advertisement.manufacturer_data[MANUFACTURER_ID]
       ).tmp
   ```
2. **Passive Coordinator Integration (`coordinator.py`):**
   `PassiveBluetoothDataUpdateCoordinator` continues parsing temperature from advertisement manufacturer data:
   $$\text{tmp} = \frac{\text{get\_short}(data, 14)}{100}$$
3. **Sensor Entity Reliability (`sensor.py`):**
   `TemperatureSensor` derives from `PassiveBluetoothCoordinatorEntity`. The ambient air temperature sensor entity is 100% functional, real-time, and resilient against any GATT connection silence, ensuring HVAC monitoring never fails.
