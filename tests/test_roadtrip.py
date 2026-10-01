"""End-to-end tests for the Road Trip integration using a real backup."""

from pathlib import Path

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from custom_components.roadtrip.const import CONF_ALLOW_REMOTE, CONF_WEBHOOK_ID, DOMAIN

import os

# Real backups contain personal data, so none ship with the repo.
# Run with: ROADTRIP_TEST_BACKUP=/path/to/Road_Trip_Backup.zip pytest
BACKUP = Path(os.environ.get("ROADTRIP_TEST_BACKUP", "/nonexistent"))
pytestmark = pytest.mark.skipif(not BACKUP.is_file(), reason="set ROADTRIP_TEST_BACKUP to a Road Trip backup .zip")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def _setup(hass: HomeAssistant, tmp_path) -> MockConfigEntry:
    hass.config.config_dir = str(tmp_path)
    await hass.config.async_set_time_zone("Africa/Johannesburg")
    await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_WEBHOOK_ID: "rt_test_hook"}, options={CONF_ALLOW_REMOTE: False}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow(hass: HomeAssistant, tmp_path) -> None:
    hass.config.config_dir = str(tmp_path)
    await async_setup_component(hass, "http", {})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ALLOW_REMOTE: False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_WEBHOOK_ID]
    assert "/api/webhook/" in result["description_placeholders"]["internal_url"]
    print("\nSUCCESS DIALOG URL:", result["description_placeholders"])
    # single instance
    result2 = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result2["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()


async def test_webhook_raw_and_multipart(hass: HomeAssistant, tmp_path, hass_client_no_auth, hass_client) -> None:
    entry = await _setup(hass, tmp_path)
    # nothing yet: only the import sensor exists
    assert hass.states.get("sensor.road_trip_last_import").state == "unknown"

    client = await hass_client_no_auth()
    # raw body (Shortcuts "Request Body: File")
    resp = await client.post("/api/webhook/rt_test_hook", data=BACKUP.read_bytes())
    body = await resp.json()
    assert resp.status == 200, body
    assert sorted(body["imported"]) == ["Mini Cooper S", "Territory", "Wildtrack"]
    await hass.async_block_till_done()

    states = {s.entity_id: s for s in hass.states.async_all("sensor")}
    for eid in sorted(states):
        s = states[eid]
        print(f"{eid:55} {s.state:>28} {s.attributes.get('unit_of_measurement', '')}")
    wt = hass.states.get("sensor.wildtrack_fuel_economy_last_5_tanks")
    assert float(wt.state) == pytest.approx(11.37, abs=0.01)
    assert hass.states.get("sensor.wildtrack_odometer_last_fill_up").state == "149889.0"
    assert hass.states.get("sensor.territory_fuel_price_last_fill_up").attributes["unit_of_measurement"] == "ZAR/L"
    assert len(hass.states.get("sensor.mini_cooper_s_last_fill_up").attributes["recent"]) == 10
    assert hass.states.get("sensor.road_trip_last_import").attributes["fillups"] == 475

    # multipart with a single vehicle file (Shortcuts "Form")
    form = aiohttp.FormData()
    from custom_components.roadtrip.parser import extract_vehicle_files
    territory = dict(extract_vehicle_files(BACKUP.read_bytes()))["Territory"]
    form.add_field("file", territory, filename="Territory.roadtrip")
    resp = await client.post("/api/webhook/rt_test_hook", data=form)
    assert resp.status == 200, await resp.text()
    assert (await resp.json())["imported"] == ["Territory"]

    # junk is rejected and nothing breaks
    resp = await client.post("/api/webhook/rt_test_hook", data=b"not a backup")
    assert resp.status == 400
    resp = await client.post("/api/webhook/rt_test_hook", data=b"")
    assert resp.status == 400
    assert hass.states.get("sensor.road_trip_last_import").attributes["fillups"] == 475

    # authenticated data API
    authed = await hass_client()
    resp = await authed.get("/api/roadtrip/data")
    data = await resp.json()
    assert resp.status == 200 and len(data["vehicles"]) == 3
    assert len(data["vehicles"][0]["series"]["economy"]) > 100
    resp = await authed.get("/api/roadtrip/data?vehicle=territory")
    assert (await resp.json())["name"] == "Territory"
    resp = await client.get("/api/roadtrip/data")
    assert resp.status == 401

    # survives a reload (data persisted to /config/roadtrip)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get("sensor.wildtrack_fuel_economy_last_5_tanks").state) == pytest.approx(11.37, abs=0.01)
    assert hass.states.get("sensor.road_trip_last_import").state not in ("unknown", "unavailable")


async def test_import_action(hass: HomeAssistant, tmp_path) -> None:
    await _setup(hass, tmp_path)
    inbox = tmp_path / "roadtrip" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "Road_Trip_Backup.zip").write_bytes(BACKUP.read_bytes())
    events = []
    hass.bus.async_listen("roadtrip_imported", events.append)
    resp = await hass.services.async_call(DOMAIN, "import_file", {}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert len(resp["vehicles"]) == 3
    assert events and events[0].data["source"] == "file:Road_Trip_Backup.zip"
    assert hass.states.get("sensor.mini_cooper_s_fill_ups_logged").state == "186"
