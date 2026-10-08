# Code review: ha_tuya_ble (pascalgoedeke fork @ 34a5eea)

Reviewed 2026-10-08. Branch `fix/fingerbot-connection-stability` (also on `main` of this fork) fixes the items marked **Fixed**. Tests are in `tests/` (`python -m pytest tests -q`). 11 of the 15 tests fail on the original code and all pass on the fix.

## Root cause of the Fingerbot Plus failures

1. **Leaked connections caused duplicated notifications. Fixed.** When the device-info or pairing step failed, `_ensure_connected` set `self._client = None` without disconnecting. The abandoned link stayed open and subscribed to notifications, so the next connection received every fragment twice. The log signature matches this exactly: "Unexpected packet (number 0), expected 1" followed by "Missing packet (number 0), received 2, 3, 4…". Reassembly then failed, the handshake timed out, and the cycle repeated.
2. **Reconnect storm. Fixed.** After every unexpected disconnect, a device was reconnected immediately unless its category was `kg`. Fingerbot Plus is `szjqr`, so each time it fell asleep it was woken again. `_reconnect` respawned itself recursively after a 0.25 s backoff, and `_ensure_connected` made up to 100 back-to-back attempts.
3. **Stale disconnect callbacks clobbered the live connection. Fixed.** A disconnect from an abandoned client reset `_client` and `_is_paired` on the current one.
4. **Reassembly was not robust. Fixed.** One lost or duplicated fragment logged an error for every remaining fragment of the reply, and the reply was lost with no recovery.
5. **The response timeout was 60 s. Fixed (now 10 s).** Every lost reply blocked commands for a minute. Pending requests now fail immediately when the link drops.

## Other bugs

| Severity | Where | Issue | Status |
|---|---|---|---|
| Medium | `tuya_ble.py` `FUN_RECEIVE_SIGN_DP` | Datapoints were parsed from offset 2, which reads the flags byte as a DP id. They start at offset 3. | Fixed |
| Low | `tuya_ble.py` `FUN_RECEIVE_SIGN_TIME_DP` | Ignored the device's timestamp and used the current time. | Fixed |
| Medium | `tuya_ble.py` `update_description` | Read `values_overrides.values` (the dict method) instead of each key's value. The walrus in the defaults branch bound a bool. Overrides and defaults never worked. | Fixed |
| Low | `tuya_ble.py` `TuyaBLEDataPoint.__str__` | `f"{self}"` recursed forever. | Fixed |
| Low | `tuya_ble.py` `_get_key` | Returned None for an unknown security flag, so AES failed later with a confusing error. | Fixed (raises a format error) |
| Low | `tuya_ble.py` notification handler | Parse and CRC exceptions escaped into the Bluetooth callback. | Fixed |
| Medium | `base.py` `IntegerTypeData.from_dict` | Used the builtin `dict` instead of `data`, so it always raised. | Fixed |
| Medium | `number.py`, `sensor.py` | `CONCENTRATION_PARTS_PER_MILLION` is removed in HA 2027.8. | Fixed |
| Low | `button.py`, `manager.py` | `Awaitable` and `List` were used without imports (annotations only). | Fixed |
| Medium | `number.py` key `hfgdqhho` | Duplicate dict key: the SGW08 irrigation mapping is silently overwritten by SGW02. | Not changed (needs device knowledge) |
| Low | `select.py` `znhsb`, `sensor.py` `zwjcy` | Duplicate category keys; the first definition is dead code. Behavior already uses the second. | Not changed |
| Low | `devices.py` `_send_command` | An enum value missing from the range sends `None`, which then fails in `pack`. | Not changed |
| Low | `tuya_ble.py` `get_or_create_datapoint` | Stub that returns None (unused). | Not changed |

## Risks and improvements not yet made

- **Dependency on core Tuya internals.** The integration imports `DPCode` from `homeassistant.components.tuya.const`. If core moves or renames it, the integration breaks on that HA update. Vendoring the few codes it needs would remove the risk.
- **Pinned `tuya-iot-py-sdk==0.6.6`.** This is an old SDK used only for cloud credential lookup. Watch for conflicts with core's Tuya requirements.
- **No automatic retry of commands.** A command whose reply is lost is not resent. This is deliberate: resending a Fingerbot press whose acknowledgement was lost could press twice, for example toggling a TV off and back on. Only the idempotent handshake retries.
- **Global connect lock.** All devices connect one at a time. That is safe with few devices but slow with many.
- **Logging volume.** Routine sleep and disconnect events are now DEBUG. Real failures stay at WARNING, and a failed connect produces one line instead of hundreds.
