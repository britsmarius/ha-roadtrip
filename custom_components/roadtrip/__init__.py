"""Road Trip (iOS fuel log) integration.

Receives Road Trip backups (a .zip, or individual .roadtrip files) via a
webhook or the ``roadtrip.import_file`` action, decodes them, and exposes
per-vehicle sensors plus a full dataset at ``/api/roadtrip/data`` for charts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aiohttp import hdrs, web
import voluptuous as vol

from homeassistant.components import webhook
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.http import KEY_HASS
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .const import (
    API_URL,
    ATTR_PATH,
    CONF_ALLOW_REMOTE,
    CONF_WEBHOOK_ID,
    DOMAIN,
    EVENT_IMPORTED,
    INBOX_DIR,
    NAME,
    SERVICE_IMPORT_FILE,
    SIGNAL_UPDATED,
    STORAGE_DIR,
)
from .parser import (
    RoadTripParseError,
    build_dataset,
    extract_vehicle_files,
    parse_vehicle_file,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

IMPORT_SCHEMA = vol.Schema({vol.Optional(ATTR_PATH): cv.string})
META_FILE = "last_import.json"


@dataclass
class RoadTripData:
    """Runtime state for the config entry."""

    dataset: dict | None = None
    last_import: datetime | None = None
    last_source: str | None = None


type RoadTripConfigEntry = ConfigEntry[RoadTripData]


def _storage(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(STORAGE_DIR))


# --------------------------------------------------------------------------
# Import / rebuild
# --------------------------------------------------------------------------


def _store_files(root: Path, uploads: list[tuple[str | None, bytes]]) -> list[str]:
    """Parse uploads, save each vehicle file under its stable id. Executor."""
    vdir = root / "vehicles"
    vdir.mkdir(parents=True, exist_ok=True)
    pending: list[tuple[str, bytes, str]] = []
    for filename, data in uploads:
        for stem, raw in extract_vehicle_files(data, filename):
            vehicle = parse_vehicle_file(raw, stem)  # validates before anything is written
            pending.append((vehicle.vehicle_id, raw, vehicle.name))
    for vid, raw, _name in pending:
        tmp = vdir / f"{vid}.roadtrip.tmp"
        tmp.write_bytes(raw)
        tmp.replace(vdir / f"{vid}.roadtrip")
    return [name for _vid, _raw, name in pending]


def _load_dataset(root: Path, now: datetime, tz) -> tuple[dict | None, dict]:
    """Rebuild the dataset from stored vehicle files. Executor."""
    vdir = root / "vehicles"
    vehicles = []
    if vdir.is_dir():
        for path in sorted(vdir.glob("*.roadtrip")):
            try:
                vehicles.append(parse_vehicle_file(path.read_bytes(), path.stem))
            except RoadTripParseError as err:
                _LOGGER.warning("Skipping unreadable stored file %s: %s", path.name, err)
    meta = {}
    meta_path = root / META_FILE
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError:
            meta = {}
    if not vehicles:
        return None, meta
    return build_dataset(vehicles, now, tz), meta


def _write_meta(root: Path, meta: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / META_FILE).write_text(json.dumps(meta))


async def async_rebuild(hass: HomeAssistant, entry: RoadTripConfigEntry) -> None:
    """Recompute stats from stored files (also rolls the 12-month window)."""
    tz = dt_util.get_default_time_zone()
    now = dt_util.now().replace(tzinfo=None)
    dataset, meta = await hass.async_add_executor_job(_load_dataset, _storage(hass), now, tz)
    data = entry.runtime_data
    data.dataset = dataset
    if meta.get("time"):
        data.last_import = dt_util.parse_datetime(meta["time"])
        data.last_source = meta.get("source")
    async_dispatcher_send(hass, SIGNAL_UPDATED.format(entry_id=entry.entry_id))


async def async_import_uploads(
    hass: HomeAssistant,
    entry: RoadTripConfigEntry,
    uploads: list[tuple[str | None, bytes]],
    source: str,
) -> dict:
    """Validate, store and apply uploaded backup files. Returns a summary."""
    names = await hass.async_add_executor_job(_store_files, _storage(hass), uploads)
    now = dt_util.now()
    await hass.async_add_executor_job(
        _write_meta, _storage(hass), {"time": now.isoformat(), "source": source}
    )
    await async_rebuild(hass, entry)
    dataset = entry.runtime_data.dataset or {"vehicles": []}
    summary = {
        "imported": names,
        "source": source,
        "time": now.isoformat(),
        "vehicles": [
            {
                "name": v["name"],
                "fillups": v["fillups"],
                "last_fill": (v.get("last_fill") or {}).get("date"),
                "economy_recent": v.get("economy_recent"),
            }
            for v in dataset["vehicles"]
        ],
    }
    hass.bus.async_fire(EVENT_IMPORTED, summary)
    _LOGGER.info("Road Trip import from %s: %s", source, ", ".join(names))
    return summary


# --------------------------------------------------------------------------
# Webhook
# --------------------------------------------------------------------------


def _entry_for_webhook(hass: HomeAssistant, webhook_id: str) -> RoadTripConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_WEBHOOK_ID) == webhook_id and entry.state is ConfigEntryState.LOADED:
            return entry
    return None


async def _read_uploads(request: web.Request) -> list[tuple[str | None, bytes]]:
    """Accept a raw file body (Shortcuts 'File') or multipart form (Shortcuts 'Form')."""
    uploads: list[tuple[str | None, bytes]] = []
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        while (part := await reader.next()) is not None:
            if not hasattr(part, "read"):
                continue  # nested multipart: not expected from Shortcuts
            data = bytes(await part.read(decode=False))
            if data:
                uploads.append((part.filename, data))
    else:
        data = await request.read()
        if data:
            uploads.append((request.query.get("filename"), data))
    return uploads


async def handle_webhook(
    hass: HomeAssistant, webhook_id: str, request: web.Request
) -> web.Response:
    """Receive a backup from the iPhone Shortcut."""
    entry = _entry_for_webhook(hass, webhook_id)
    if entry is None:
        return web.json_response({"error": "not ready"}, status=503)
    uploads = await _read_uploads(request)
    if not uploads:
        return web.json_response({"error": "empty body — send the backup file"}, status=400)
    try:
        summary = await async_import_uploads(hass, entry, uploads, "webhook")
    except RoadTripParseError as err:
        _LOGGER.warning("Rejected Road Trip upload: %s", err)
        return web.json_response({"error": str(err)}, status=400)
    return web.json_response(summary)


# --------------------------------------------------------------------------
# Data API for dashboards
# --------------------------------------------------------------------------


class RoadTripDataView(HomeAssistantView):
    """Authenticated JSON endpoint: GET /api/roadtrip/data[?vehicle=<slug>]."""

    url = API_URL
    name = "api:roadtrip:data"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app[KEY_HASS]
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED and entry.runtime_data.dataset:
                dataset = entry.runtime_data.dataset
                if slug := request.query.get("vehicle"):
                    match = [v for v in dataset["vehicles"] if v["slug"] == slug or v["id"] == slug]
                    if not match:
                        return self.json_message("unknown vehicle", 404)
                    return self.json(match[0])
                return self.json(dataset)
        return self.json({"generated": None, "vehicles": [], "fleet": {}})


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the action and API view (once, independent of entries)."""
    hass.http.register_view(RoadTripDataView())

    async def _import_file(call: ServiceCall) -> ServiceResponse:
        entries = [
            e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
        ]
        if not entries:
            raise ServiceValidationError("Road Trip is not set up")
        entry = entries[0]
        if path_str := call.data.get(ATTR_PATH):
            path = Path(path_str)
            if not (path.resolve().is_relative_to(_storage(hass).resolve()) or hass.config.is_allowed_path(str(path))):
                raise ServiceValidationError(
                    f"{path} is outside /config/{STORAGE_DIR} and not in allowlist_external_dirs"
                )
        else:
            inbox = _storage(hass) / INBOX_DIR

            def _newest() -> Path | None:
                if not inbox.is_dir():
                    return None
                files = [p for p in inbox.iterdir() if p.suffix in (".zip", ".roadtrip")]
                return max(files, key=lambda p: p.stat().st_mtime, default=None)

            path = await hass.async_add_executor_job(_newest)
            if path is None:
                raise ServiceValidationError(
                    f"No .zip or .roadtrip file found in /config/{STORAGE_DIR}/{INBOX_DIR}"
                )
        try:
            data = await hass.async_add_executor_job(path.read_bytes)
        except OSError as err:
            raise HomeAssistantError(f"Cannot read {path}: {err}") from err
        try:
            summary = await async_import_uploads(hass, entry, [(path.name, data)], f"file:{path.name}")
        except RoadTripParseError as err:
            raise HomeAssistantError(f"{path.name}: {err}") from err
        return summary if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_FILE,
        _import_file,
        schema=IMPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: RoadTripConfigEntry) -> bool:
    """Set up Road Trip from a config entry."""
    entry.runtime_data = RoadTripData()
    await async_rebuild(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    webhook.async_register(
        hass,
        DOMAIN,
        NAME,
        entry.data[CONF_WEBHOOK_ID],
        handle_webhook,
        local_only=not entry.options.get(CONF_ALLOW_REMOTE, False),
        allowed_methods=[hdrs.METH_POST, hdrs.METH_PUT],
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID]))

    @callback
    def _nightly(_now) -> None:
        hass.async_create_task(async_rebuild(hass, entry), "roadtrip nightly rebuild")

    entry.async_on_unload(async_track_time_change(hass, _nightly, hour=0, minute=5, second=0))
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: RoadTripConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: RoadTripConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
