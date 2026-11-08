# T6 setup investigation

## Confirmed findings

The supplied HA log shows a successful BLE connection via hci0 at approximately RSSI -72, notification subscription completing, and the 20-byte query `a5000008000225da00011011121314151617713e` timing out waiting for a notification. This is **not evidence of a range failure**, and a MAC prefix cannot establish protocol compatibility.

The manufacturer payload for `BLE_FAN` / `8DAQJ` decodes under 0.4.3 to **device type 6, protocol version 3, name D-8DAQJ**. It is not type 48. Bytes 11 and 12 after the five-byte identifier contain version/type. The existing library names type 6 family D. Retail T6 versus T4 cannot be distinguished from that family byte alone.

Integration 2.2.0 pins `ac-infinity-ble==0.4.3`. Its transport always calls `write_gatt_char(..., False)` without checking advertised properties, waits only five seconds, independently picks notify and write UUIDs, and leaves the pending future behind on timeout or write failure. Its subclass also accepts any A5 notification without complete-frame/CRC/sequence validation, parses setup state at fixed offsets, and tests state conversion with `is DeviceInfo` instead of `isinstance`. Discovered setup saves an un-serialized advertisement dataclass instead of the queried state. These are demonstrable code defects; not all necessarily caused this device's silence.

## Strongest hardware-failure hypothesis (moderate confidence, not proven)

The write-without-response assumption is the strongest actionable transport mismatch. Independent app protocol notes document acknowledged writes when supported. We select acknowledged writes when the characteristic advertises `write`, otherwise write-without-response, and log the choice. The actual characteristics/properties were absent from the original log, so this cannot yet be called the proven root cause.

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
