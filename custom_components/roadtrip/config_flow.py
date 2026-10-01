"""Config flow for Road Trip."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError

from .const import CONF_ALLOW_REMOTE, CONF_WEBHOOK_ID, DOMAIN, NAME


def _urls(hass: HomeAssistant, webhook_id: str) -> dict[str, str]:
    """Placeholders showing where the Shortcut should send the file."""
    path = webhook.async_generate_path(webhook_id)
    try:
        internal = webhook.async_generate_url(hass, webhook_id, allow_external=False, allow_ip=True)
    except NoURLAvailableError:
        internal = f"http://<home-assistant-ip>:8123{path}"
    try:
        external = webhook.async_generate_url(hass, webhook_id, allow_internal=False)
    except NoURLAvailableError:
        external = "(no external URL configured)"
    return {"internal_url": internal, "external_url": external, "webhook_path": path}


SCHEMA = vol.Schema({vol.Required(CONF_ALLOW_REMOTE, default=False): bool})


class RoadTripConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the single Road Trip entry and its webhook."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            webhook_id = webhook.async_generate_id()
            return self.async_create_entry(
                title=NAME,
                data={CONF_WEBHOOK_ID: webhook_id},
                options={CONF_ALLOW_REMOTE: user_input[CONF_ALLOW_REMOTE]},
                description_placeholders=_urls(self.hass, webhook_id),
            )
        return self.async_show_form(step_id="user", data_schema=SCHEMA)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return RoadTripOptionsFlow()


class RoadTripOptionsFlow(OptionsFlow):
    """Show the webhook URL again and toggle remote access."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(SCHEMA, self.config_entry.options),
            description_placeholders=_urls(self.hass, self.config_entry.data[CONF_WEBHOOK_ID]),
        )
