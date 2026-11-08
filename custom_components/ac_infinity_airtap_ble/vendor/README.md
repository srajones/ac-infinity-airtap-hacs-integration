# Vendored dependency

`ac_infinity_ble/` is based on the PyPI `ac-infinity-ble==0.4.3` sdist from https://github.com/hunterjm/ac-infinity-ble. Its MIT license is preserved as `ac_infinity_ble/LICENSE`. Imports are integration-relative; there is no global `sys.modules` replacement.

Local changes: staged BLE errors/logging, acknowledged writes according to properties, 20-byte transport chunks, frame validation/reassembly, paired characteristic resolution, read-only reconnect retries with backoff, bounded cleanup, availability callbacks, and advertisement bounds validation. See docs/AIRTAP_INVESTIGATION.md in the repository root.

fix.2 adds setup-only allowlisted metadata/control/CCCD reads, optional diagnostic warnings, and the APK-verified A5 00–1F response-header family. There is no OTA access, bond/pair operation, extra subscription, or new outgoing control protocol.

fix.3 follows the user-provided Android 2.1.2 app's type-6 Telink 1500ms post-subscribe/revision readiness delay, validates the full reply command word, labels diagnostic-read callbacks, suppresses control-value READ snapshots during type-6 setup, and releases backend resources even after link loss. App-derived GET key selection and the initial sequence live in the integration wrapper. No APK, decompiled source, assets, clock-sync writes or speculative wire variants are bundled.
