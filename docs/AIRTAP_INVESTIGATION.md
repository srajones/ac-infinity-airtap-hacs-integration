# T6 setup investigation

## Confirmed findings

The supplied HA log shows a successful BLE connection via hci0 at approximately RSSI -72, notification subscription completing, and the 20-byte query `a5000008000225da00011011121314151617713e` timing out waiting for a notification. This is **not evidence of a range failure**, and a MAC prefix cannot establish protocol compatibility.

The manufacturer payload for `BLE_FAN` / `8DAQJ` decodes under 0.4.3 to **device type 6, protocol version 3, name D-8DAQJ**. It is not type 48. Bytes 11 and 12 after the five-byte identifier contain version/type. The existing library names type 6 family D. Retail T6 versus T4 cannot be distinguished from that family byte alone.

Integration 2.2.0 pins `ac-infinity-ble==0.4.3`. Its transport always calls `write_gatt_char(..., False)` without checking advertised properties, waits only five seconds, independently picks notify and write UUIDs, and leaves the pending future behind on timeout or write failure. Its subclass also accepts any A5 notification without complete-frame/CRC/sequence validation, parses setup state at fixed offsets, and tests state conversion with `is DeviceInfo` instead of `isinstance`. Discovered setup saves an un-serialized advertisement dataclass instead of the queried state. These are demonstrable code defects; not all necessarily caused this device's silence.

## Initial hypothesis, superseded by the fix.1 hardware test

The initial write-without-response assumption was an actionable transport mismatch. Independent app protocol notes document acknowledged writes when supported. We select acknowledged writes when the characteristic advertises `write`, otherwise write-without-response, and log the choice. The fix.1 hardware log now shows `70d51001` advertises read+write, our acknowledged write completes, and still no raw RX notification arrives. **Acknowledged writes alone do not resolve the silence; that initial root-cause hypothesis is rejected.**

The minimum setup query now requests **only 0x10/0x11/0x12**, not unused timers/schedules. It requires a CRC/sequence-matched reply with valid mode and both speeds, decoded as TLVs. Removing unrelated keys avoids demanding non-AirTap features; whether the old broad query was rejected by this firmware remains unproven. A longer deadline and bounded read reconnects improve tolerance but do not bypass validation or invent a reply.

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
