"""Pure mapping from device replies to DB-01 runtime evidence.

Only fields named here are read. Everything else in a reply (including any
credential, network or location blocks a firmware might return despite the
``keys`` filter) is discarded and never retained.

Value-state rules:
* a field that is absent                      -> UNAVAILABLE
* a field that is present but ``null``/wrong type or a known sentinel -> UNKNOWN
* a field present with the expected type       -> KNOWN
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..device_runtime import (
    PreviewAvailability,
    TelemetryItem,
    TelemetrySource,
    ValueState,
)
from .errors import SeestarProtocolError
from .protocol import device_ref_for

PR = TelemetrySource.PROVIDER_REPORTED
_MISSING = object()

# Documented device defaults meaning "no manual value" (not measurements).
EXPOSURE_SENTINELS = (-999000.0,)
GAIN_SENTINELS = (-9990.0,)

PREVIEW_PORTS = {"main": 4554, "wide": 4555}
PREVIEW_VIEW_KEYS = {"main": "View", "wide": "SecondView"}
STOPPED_STATES = ("cancel", "complete", "idle")


def _dig(source: Any, path: tuple[str, ...]) -> Any:
    current = source
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(slots=True, frozen=True)
class _Field:
    name: str
    path: tuple[str, ...]
    check: Callable[[Any], bool]
    sentinels: tuple[Any, ...] = ()


def _is_text(value: Any) -> bool:
    return isinstance(value, str)


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


DEVICE_FIELDS: tuple[_Field, ...] = (
    _Field("device.firmware_version", ("device", "firmware_ver_string"), _is_text),
    _Field("mount.move_type", ("mount", "move_type"), _is_text),
    _Field("mount.arm_closed", ("mount", "close"), _is_bool),
    _Field("mount.tracking", ("mount", "tracking"), _is_bool),
    _Field("mount.equatorial_mode", ("mount", "equ_mode"), _is_bool),
    _Field("pi.temperature_c", ("pi_status", "temp"), _is_number),
    _Field("pi.battery_percent", ("pi_status", "battery_capacity"), _is_number),
    _Field("pi.battery_temperature_c", ("pi_status", "battery_temp"), _is_number),
    _Field("pi.charger_status", ("pi_status", "charger_status"), _is_text),
    _Field("pi.overtemperature", ("pi_status", "is_overtemp"), _is_bool),
    _Field("focuser.state", ("focuser", "state"), _is_text),
    _Field("focuser.step", ("focuser", "step"), _is_number),
    _Field("wide_focuser.step", ("second_focuser", "step"), _is_number),
    _Field("heater.enabled", ("setting", "heater_enable"), _is_bool),
    _Field("setting.manual_exposure", ("setting", "manual_exp"), _is_bool),
    _Field("setting.exposure_ms", ("setting", "isp_exp_ms"), _is_number, EXPOSURE_SENTINELS),
    _Field("setting.gain", ("setting", "isp_gain"), _is_number, GAIN_SENTINELS),
    _Field("setting.wide_cam_flag", ("setting", "wide_cam"), _is_bool),
)

_APP_FIELDS_PER_VIEW = (
    ("mode", ("mode",), _is_text),
    ("state", ("state",), _is_text),
    ("stage", ("stage",), _is_text),
    ("rtsp_state", ("RTSP", "state"), _is_text),
    ("rtsp_port", ("RTSP", "port"), _is_number),
)
APP_FIELDS: tuple[_Field, ...] = (
    _Field("app.selected_camera", ("selected_cam",), _is_text),
    *(
        _Field(f"app.{prefix}.{label}", (view, *path), check)
        for prefix, view in (("main", "View"), ("wide", "SecondView"))
        for label, path, check in _APP_FIELDS_PER_VIEW
    ),
)

TIMESTAMP_ITEM = "rpc.device_timestamp_raw"
DEVICE_ITEM_NAMES = tuple(f.name for f in DEVICE_FIELDS) + (TIMESTAMP_ITEM,)
APP_ITEM_NAMES = tuple(f.name for f in APP_FIELDS)


def _item(spec: _Field, source: Any) -> TelemetryItem:
    value = _dig(source, spec.path)
    if value is _MISSING:
        return TelemetryItem(spec.name, PR, ValueState.UNAVAILABLE)
    if value is None or not spec.check(value) or value in spec.sentinels:
        return TelemetryItem(spec.name, PR, ValueState.UNKNOWN)
    return TelemetryItem(spec.name, PR, ValueState.KNOWN, value)


def device_items(result: Any, device_timestamp: str | None) -> tuple[TelemetryItem, ...]:
    """Map a ``get_device_state`` result. Timestamp semantics are unverified and kept raw."""
    if not isinstance(result, Mapping):
        raise SeestarProtocolError("malformed_state")
    items = [_item(spec, result) for spec in DEVICE_FIELDS]
    if device_timestamp is None:
        items.append(TelemetryItem(TIMESTAMP_ITEM, PR, ValueState.UNAVAILABLE))
    else:
        items.append(TelemetryItem(TIMESTAMP_ITEM, PR, ValueState.KNOWN, device_timestamp))
    return tuple(items)


def app_items(result: Any) -> tuple[TelemetryItem, ...]:
    if not isinstance(result, Mapping):
        raise SeestarProtocolError("malformed_state")
    return tuple(_item(spec, result) for spec in APP_FIELDS)


@dataclass(slots=True, frozen=True)
class DeviceIdentity:
    device_ref: str = field(repr=False)
    sn: str = field(repr=False)
    model: str
    firmware_version: str | None


def parse_identity(result: Any) -> DeviceIdentity:
    """Identity from the ``device`` block. A missing serial means no stable identity."""
    block = result.get("device") if isinstance(result, Mapping) else None
    if not isinstance(block, Mapping):
        raise SeestarProtocolError("identity_unavailable")
    sn = block.get("sn")
    if not isinstance(sn, str) or not sn.strip():
        raise SeestarProtocolError("identity_unavailable")
    model = block.get("product_model")
    model = model if isinstance(model, str) else ""
    firmware = block.get("firmware_ver_string")
    return DeviceIdentity(
        device_ref=device_ref_for(model, sn.strip()),
        sn=sn.strip(),
        model=model,
        firmware_version=firmware if isinstance(firmware, str) else None,
    )


def preview_availability(app_result: Any, camera: str) -> PreviewAvailability:
    """Availability *evidence* that the device reports RTSP working. No stream is opened."""
    if not isinstance(app_result, Mapping):
        return PreviewAvailability.UNKNOWN
    view = app_result.get(PREVIEW_VIEW_KEYS[camera])
    if not isinstance(view, Mapping):
        return PreviewAvailability.UNKNOWN
    rtsp = view.get("RTSP")
    if not isinstance(rtsp, Mapping) or "state" not in rtsp or "state" not in view:
        return PreviewAvailability.UNKNOWN
    ready = (
        view.get("mode") == "scenery"
        and view.get("stage") == "RTSP"
        and view.get("state") == "working"
        and rtsp.get("state") == "working"
        and rtsp.get("port") == PREVIEW_PORTS[camera]
    )
    return PreviewAvailability.AVAILABLE if ready else PreviewAvailability.UNAVAILABLE
