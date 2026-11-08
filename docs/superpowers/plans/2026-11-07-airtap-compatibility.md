# AirTap compatibility implementation plan

Goal: repair the verified transport defects and produce a hardware-testable HACS build, without claiming an unverified hardware fix.

Architecture: vendor the MIT-licensed 0.4.3 baseline to preserve the integration API. Patch the BLE boundaries and setup validation; keep command-driven runtime behavior, never poll aggressively.

Constraints: only srajones/ac-infinity-airtap-ble-fix; no upstream writes; no credential output; no writes to unidentified GATT services.

1. Record failing regressions for acknowledged writes, cleanup, matching GATT pairs, fragmented/validated replies, extended state, and setup reads (tests/test_transport.py).
2. Patch vendor/ac_infinity_ble/device.py with step-specific errors, bounded read retries/backoff, capability-aware writes, an 8-second response deadline, complete service logging, and bounded cleanup. Do not replay setting writes after ambiguous failures.
3. Patch integration device.py to read only mode/on/off keys, require real valid settings, preserve extended state, and propagate availability changes. Patch config_flow.py to serialize queried state and map stage-specific errors; update strings and translations.
4. Add negative/failure-stage regressions before the corresponding code. Run pytest, ruff on changed code, compileall, JSON checks, and inspect the final diff for secrets and unrelated files.
5. Document evidence, confidence limits, safe install/replacement steps and logging recipe. Bump version to 2.2.0-fix.1. Commit and push only the authorized fork; publication must remain within the user's authorization.

6. fix.2 follow-up: reject the acknowledged-write-only root-cause theory using the second hardware log; add setup-only allowlisted revision/PnP/CCCD/control snapshot diagnostics with 2s read deadlines and no pairing, OTA access, extra subscriptions or speculative writes. Correct the independently evidenced A5 00–1F receive-header family while retaining CRC/sequence checks. Validate cancellation, stale snapshot rejection, optional-backend failures, and warning throttling. Root-cause closure is deferred until the new device-side evidence is available.
