"""Tests for the Tuya BLE device protocol layer (no Home Assistant needed).

Run with:  python -m pytest tests -q
"""
from __future__ import annotations

import asyncio
import enum
import hashlib
import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "tuya_ble"


def _load_device_module():
    """Import tuya_ble/tuya_ble.py with tiny stand-ins for Home Assistant."""
    ha = types.ModuleType("homeassistant")
    comps = types.ModuleType("homeassistant.components")
    tuya = types.ModuleType("homeassistant.components.tuya")
    tuya_const = types.ModuleType("homeassistant.components.tuya.const")
    tuya_const.DPCode = str
    for name, mod in {
        "homeassistant": ha,
        "homeassistant.components": comps,
        "homeassistant.components.tuya": tuya,
        "homeassistant.components.tuya.const": tuya_const,
    }.items():
        sys.modules.setdefault(name, mod)

    pkg = types.ModuleType("tb")
    pkg.__path__ = [str(ROOT)]
    sys.modules["tb"] = pkg
    const = types.ModuleType("tb.const")

    class DPType(enum.StrEnum):
        BOOLEAN = "Boolean"
        INTEGER = "Integer"

    const.DPType = DPType
    sys.modules["tb.const"] = const

    sub = types.ModuleType("tb.tuya_ble")
    sub.__path__ = [str(ROOT / "tuya_ble")]
    sys.modules["tb.tuya_ble"] = sub
    for name in ("const", "exceptions", "manager", "tuya_ble"):
        spec = importlib.util.spec_from_file_location(
            f"tb.tuya_ble.{name}", ROOT / "tuya_ble" / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"tb.tuya_ble.{name}"] = module
        spec.loader.exec_module(module)
    return sys.modules["tb.tuya_ble.tuya_ble"], sys.modules["tb.tuya_ble.const"]


tb, tb_const = _load_device_module()


class FakeBLEDevice:
    address = "DC:23:4D:AE:13:7C"
    name = "Fingerbot"


class FakeCredentials:
    uuid = "uuid0123456789ab"
    local_key = "abcdef0123456789"
    device_id = "dev0123456789abc"
    category = "szjqr"
    product_id = "yiihr7zh"
    device_name = "TV Down"
    product_model = "ADFB0301"
    product_name = "Fingerbot Plus"
    functions = []
    status_range = []


def make_device(category: str = "szjqr") -> tb.TuyaBLEDevice:
    device = tb.TuyaBLEDevice(None, FakeBLEDevice())
    creds = FakeCredentials()
    creds.category = category
    device._device_info = creds
    device._local_key = creds.local_key[:6].encode()
    device._login_key = hashlib.md5(device._local_key).digest()
    device._session_key = hashlib.md5(device._local_key + b"123456").digest()
    device._protocol_version = 3
    return device


def device_reply(device, code, data: bytes, seq: int = 7, response_to: int = 0):
    """Build the fragments a device would send for one reply."""
    return [bytes(p) for p in device._build_packets(seq, code, data, response_to)]


def bool_dp(dp_id: int, value: bool) -> bytes:
    return bytes([dp_id, 1, 1, 1 if value else 0])


# --- Reassembly -----------------------------------------------------------


def test_reply_is_split_into_several_fragments():
    device = make_device()
    frags = device_reply(device, tb_const.TuyaBLECode.FUN_RECEIVE_DP, bool_dp(2, True) * 4)
    assert len(frags) > 2


def test_normal_reply_updates_datapoint(monkeypatch):
    device = make_device()
    monkeypatch.setattr(device, "_send_response", _noop_async)
    frags = device_reply(device, tb_const.TuyaBLECode.FUN_RECEIVE_DP, bool_dp(2, True))
    with _loop():
        for f in frags:
            device._notification_handler(0, bytearray(f))
    assert device.datapoints[2].value is True


def test_duplicated_fragments_are_ignored(monkeypatch, caplog):
    """The failure seen in the field: every notification delivered twice."""
    device = make_device()
    monkeypatch.setattr(device, "_send_response", _noop_async)
    frags = device_reply(
        device, tb_const.TuyaBLECode.FUN_RECEIVE_DP, bool_dp(2, True) * 3
    )
    with _loop():
        for f in frags:
            device._notification_handler(0, bytearray(f))
            device._notification_handler(0, bytearray(f))
    assert device.datapoints[2].value is True
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]


def test_lost_fragment_discards_reply_quietly_then_recovers(monkeypatch, caplog):
    device = make_device()
    monkeypatch.setattr(device, "_send_response", _noop_async)
    first = device_reply(
        device, tb_const.TuyaBLECode.FUN_RECEIVE_DP, bool_dp(2, True) * 3, seq=1
    )
    second = device_reply(
        device, tb_const.TuyaBLECode.FUN_RECEIVE_DP, bool_dp(8, False), seq=2
    )
    with _loop():
        for i, f in enumerate(first):
            if i == 1:
                continue  # lost on air
            device._notification_handler(0, bytearray(f))
        assert device.datapoints[2] is None
        for f in second:
            device._notification_handler(0, bytearray(f))
    assert device.datapoints[8].value is False
    warnings = [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
    assert len(warnings) == 1  # one line per lost reply, not one per fragment


def test_garbage_notification_does_not_raise():
    device = make_device()
    device._notification_handler(0, bytearray(b"\xff\xff\xff\xff\xff\xff"))
    device._notification_handler(0, bytearray(b"\x00\x05\x30garbage"))


# --- Reconnect policy -----------------------------------------------------


@pytest.mark.parametrize("category", ["szjqr", "kg"])
def test_fingerbots_do_not_auto_reconnect(category):
    device = make_device(category)
    assert device._should_auto_reconnect() is False


def test_other_devices_still_auto_reconnect():
    device = make_device("wsdcg")
    assert device._should_auto_reconnect() is True
    device.auto_reconnect = False
    assert device._should_auto_reconnect() is False


def test_sleeping_fingerbot_disconnect_schedules_nothing(monkeypatch):
    device = make_device("szjqr")
    called = []
    monkeypatch.setattr(device, "_schedule_reconnect", lambda: called.append(1))
    client = FakeClient()
    device._client = client
    device._is_paired = True
    device._disconnected(client)
    assert called == []
    assert device._client is None


def test_disconnect_of_stale_client_keeps_current_connection():
    device = make_device()
    current, stale = FakeClient(), FakeClient()
    device._client = current
    device._is_paired = True
    device._disconnected(stale)
    assert device._client is current
    assert device._is_paired is True


def test_disconnect_fails_pending_requests_fast():
    async def run():
        device = make_device("wsdcg")
        device.auto_reconnect = False
        fut = asyncio.get_running_loop().create_future()
        device._input_expected_responses[5] = fut
        client = FakeClient()
        device._client = client
        device._is_paired = True
        device._disconnected(client)
        assert fut.done() and isinstance(fut.exception(), tb.BleakError)

    asyncio.run(run())


# --- Connection handling --------------------------------------------------


def test_failed_handshake_closes_every_link(monkeypatch):
    """A failed handshake must not leave a connection open behind it."""
    created: list[FakeClient] = []

    async def fake_establish(*_args, **_kwargs):
        client = FakeClient()
        created.append(client)
        return client

    async def no_reply(*_args, **_kwargs):
        return False

    monkeypatch.setattr(tb, "establish_connection", fake_establish)
    monkeypatch.setattr(tb, "CONNECT_RETRY_DELAY", 0)

    async def run():
        device = make_device()
        monkeypatch.setattr(device, "_send_packet_while_connected", no_reply)
        with pytest.raises(tb.BleakNotFoundError):
            await device._ensure_connected()
        return device

    device = asyncio.run(run())
    assert len(created) == tb_const.CONNECT_ATTEMPTS
    assert all(not c.is_connected for c in created)
    assert all(c.notify_stopped for c in created)
    assert device._client is None


def test_successful_handshake(monkeypatch):
    async def fake_establish(*_args, **_kwargs):
        return FakeClient()

    async def run():
        device = make_device()

        async def reply_ok(code, *_args, **_kwargs):
            if code == tb_const.TuyaBLECode.FUN_SENDER_PAIR:
                device._is_paired = True
            return True

        monkeypatch.setattr(device, "_send_packet_while_connected", reply_ok)
        await device._ensure_connected()
        return device

    monkeypatch.setattr(tb, "establish_connection", fake_establish)
    device = asyncio.run(run())
    assert device.connected


# --- Smaller fixes ----------------------------------------------------------


def test_datapoint_str_does_not_recurse():
    device = make_device()
    dp = device.datapoints.get_or_create(2, tb_const.TuyaBLEDataPointType.DT_BOOL, True)
    assert "id:2" in str(dp)


def test_signed_datapoints_start_after_flags(monkeypatch):
    device = make_device()
    monkeypatch.setattr(device, "_send_response", _noop_async)
    payload = b"\x00\x09" + b"\x00" + bool_dp(2, True)  # seq, flags, dp
    with _loop():
        device._handle_command_or_response(
            3, 0, tb_const.TuyaBLECode.FUN_RECEIVE_SIGN_DP, payload
        )
    assert device.datapoints[2].value is True
    assert device.datapoints[0] is None


# --- Helpers ----------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.notify_stopped = False

    async def start_notify(self, *_args):
        return None

    async def stop_notify(self, *_args):
        self.notify_stopped = True

    async def disconnect(self):
        self.is_connected = False


async def _noop_async(*_args, **_kwargs):
    return None


class _loop:
    """Run synchronous handler code with a running event loop (create_task)."""

    def __enter__(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        # Make the loop "running" for the duration of the block.
        asyncio.events._set_running_loop(self.loop)
        return self

    def __exit__(self, *exc):
        asyncio.events._set_running_loop(None)
        pending = asyncio.all_tasks(self.loop)
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending))
        self.loop.close()
        asyncio.set_event_loop(None)
