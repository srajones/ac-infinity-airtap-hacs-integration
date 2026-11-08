# AirTap Clock Initialization Implementation Plan

> **For agentic workers:** Execute inline as the already-dispatched executor; use test-driven-development and verification-before-completion.

**Goal:** Match the official app's type-6 clock initialization before HA queries or user commands.

**Architecture:** Keep the existing serialized transport and cleanup. Initialize each new type-6 session under the connect lock, then lazily construct the pending command so clock sequence 0 precedes query sequence 1. Require a validated clock acknowledgement; do not treat it as a successful model query.

**Tech Stack:** Python, asyncio, Bleak, pytest; private JADX references used only as protocol evidence.

## Global Constraints
- Only srajones/ac-infinity-airtap-ble-fix main; version 2.2.0-fix.4.
- No release, upstream changes, OTA access, fan/mode changes during initialization, APK code/assets in git, or printed credentials.
- Clock synchronization is the only newly authorized setting write; use HA local timezone.

### Task 1: Protocol and per-connection initialization
**Files:** vendor/ac_infinity_ble/protocol.py (calendar frame); vendor/ac_infinity_ble/device.py (session initialization, lazy requests); integration device.py (HA timezone and deferred sequence allocation); tests/test_clock_init.py and tests/test_app_protocol.py.
**Interfaces:** Protocol.set_clock_data(now: datetime, sequence: int) -> bytes; controller._get_clock_time() -> datetime; controller._initialize_telink_session() -> None; _send_command accepts bytes or Callable[[], bytes].
- [x] Write and run failing tests for exact calendar payload/CRC, timezone/weekdays, clock-before-model ordering, one acknowledged 21-byte write, reconnection sequence, explicit ACK rejection, bounded timeout retry/backoff, cancellation cleanup and no accidental setting replay.
- [x] Add the calendar builder using payload [1,7,year%100,month,day,isoweekday,hour,minute,second], opcode 3 and existing A500/CCITT frame builder; reject naive/out-of-range dates.
- [x] After subscribe/revisions/1.5-second settle, reset session sequence to -1 and send the calendar at sequence 0 through the existing operation lock. Require validated payload 0100. Transport errors retain no_response/write_failed; malformed or rejected ACK uses initialization_failed. Fail closed before querying.
- [x] Defer building query/setting frames until connection initialization completes, rebuilding read requests on fresh-session retry. Do not replay uncertain user writes. Send clock as one public acknowledged GATT write, not 20+1 application chunks. Never call private MTU acquisition helpers.
- [x] Run the new tests and entire existing suite; adjust only mocks isolating earlier readiness tests.

### Task 2: Diagnostics, documentation and delivery
**Files:** strings.json, translations/en.json, manifest.json, README.md, docs/AIRTAP_INVESTIGATION.md.
**Interfaces:** initialization_failed translated config error; logs clock local time/zone, sequence, bytes, single-write length, result and failure stage.
- [x] Add translated clock-rejection error; change no_response wording from query to command.
- [x] Document complete scan-to-control APK trace with class/line citations, conditional clock-first path, MTU247 request vs BlueZ placeholder23, no type-6 bonding/encryption/other-notify evidence, enrollment-only writes deliberately excluded, and hardware uncertainty.
- [x] Run pytest, ruff, compileall, JSON parsing, git diff --check and staged-file safety review.
- [ ] Commit only integration/tests/docs, push main via the authorized fork token with redacted git output, restore plain origin, verify remote SHA and clean tree; no release or tag.

## Verification checkpoint

93 tests passed on Python3.13; Ruff, compileall, manifest/translation JSON and git diff --check passed. No live T6/HA Python3.14 test or independent reviewer was available. Delivery is the final post-verification gate; its outcome is recorded in the executor report.
