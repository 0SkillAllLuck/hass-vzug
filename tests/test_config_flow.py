from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.helpers.typing import UNDEFINED
from yarl import URL

from custom_components.vzug import api
from custom_components.vzug.config_flow import ConfigFlow
from custom_components.vzug.const import DOMAIN


@pytest.mark.asyncio
async def test_every_attempt_closes_its_client():
    flow = ConfigFlow()
    flow.hass = MagicMock()
    flow.flow_id = "flow"
    flow.handler = DOMAIN
    meta = MagicMock()
    answers = [api.AuthenticationFailed(), api.AuthenticationFailed(), OSError(), meta]
    clients: list[MagicMock] = []

    def new_client(*args, **kwargs) -> MagicMock:
        client = MagicMock(aclose=AsyncMock())
        client.aggregate_meta = AsyncMock(side_effect=[answers.pop(0)])
        clients.append(client)
        return client

    credentials = {"username": "admin", "password": "wrong"}
    with patch("custom_components.vzug.config_flow.api.VZugApi", new_client):
        result = await flow.async_step_manual({"host": "appliance"})
        assert result["step_id"] == "auth"
        result = await flow.async_step_auth(credentials)
        assert result["errors"] == {"base": "auth_failed"}
        result = await flow.async_step_auth(credentials)
        assert result["errors"] == {"base": "cannot_connect"}
        assert await flow._fetch_meta() is meta

    assert flow._base_url == URL("http://appliance")
    assert len(clients) == 4
    for client in clients:
        client.aclose.assert_awaited_once()


MAC = "fc:1b:ff:00:00:01"


def _meta(mac: str = "FC1BFF000001") -> api.AggMeta:
    return api.AggMeta(
        mac_address=mac,
        model_id="",
        model_name="",
        device_name="Dishwasher",
        serial_number="123",
        api_version=(1,),
    )


class FakeEntries:
    """Just enough of HA's config entries manager for the flow helpers."""

    def __init__(self, *entries: SimpleNamespace) -> None:
        self.entries = list(entries)
        self.flow = MagicMock()
        self.flow.async_progress_by_handler.return_value = []
        self.async_schedule_reload = MagicMock()

    def async_entries(self, *args, **kwargs) -> list[SimpleNamespace]:
        return self.entries

    def async_entry_for_domain_unique_id(self, domain, unique_id):
        return next((e for e in self.entries if e.unique_id == unique_id), None)

    def async_get_known_entry(self, entry_id):
        return next(e for e in self.entries if e.entry_id == entry_id)

    def async_update_entry(self, entry, *, data=UNDEFINED, **kwargs) -> bool:
        if data is UNDEFINED or data == entry.data:
            return False
        entry.data = data
        return True


def _entry(**data) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="entry",
        unique_id=MAC,
        title="Dishwasher (123)",
        source="user",
        state=ConfigEntryState.LOADED,
        options={},
        data={
            "username": None,
            "password": None,
            "base_url": "http://192.0.2.1",
            **data,
        },
    )


def _flow(source: str, *entries: SimpleNamespace, **context) -> ConfigFlow:
    flow = ConfigFlow()
    flow.hass = MagicMock(config_entries=FakeEntries(*entries))
    flow.flow_id = "flow"
    flow.handler = DOMAIN
    flow.context = {"source": source, **context}
    return flow


async def _step(step):
    """Run a step, returning an abort the way HA's flow manager does."""
    try:
        return await step
    except AbortFlow as err:
        return {"type": FlowResultType.ABORT, "reason": err.reason}


def _appliance(*answers, credentials: list | None = None):
    """Patch the API client: every attempt gets the next answer."""
    answers_left = list(answers)

    def new_client(base_url, **kwargs):
        if credentials is not None:
            credentials.append(kwargs.get("credentials"))
        return MagicMock(
            aclose=AsyncMock(),
            aggregate_meta=AsyncMock(side_effect=[answers_left.pop(0)]),
        )

    return patch("custom_components.vzug.config_flow.api.VZugApi", new_client)


@pytest.mark.asyncio
async def test_manual_creates_entry():
    flow = _flow("user")
    with _appliance(_meta()):
        result = await _step(flow.async_step_manual({"host": "192.0.2.1"}))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["base_url"] == "http://192.0.2.1"
    assert flow.unique_id == MAC


@pytest.mark.asyncio
async def test_manual_aborts_for_configured_appliance():
    entry = _entry(username="admin", password="secret")
    flow = _flow("user", entry)
    with _appliance(_meta()):
        result = await _step(flow.async_step_manual({"host": "192.0.2.2"}))

    assert result["reason"] == "already_configured"
    assert entry.data["base_url"] == "http://192.0.2.1"
    assert entry.data["password"] == "secret"
    flow.hass.config_entries.async_schedule_reload.assert_not_called()


@pytest.mark.asyncio
async def test_discovery_creates_entry_after_confirmation():
    flow = _flow("discovery")
    with _appliance(_meta()):
        result = await _step(
            flow.async_step_discovery(api.discovery.DiscoveryInfo(host="192.0.2.1"))
        )
    assert result["step_id"] == "confirm"

    result = await _step(flow.async_step_confirm({}))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert flow.unique_id == MAC


@pytest.mark.asyncio
async def test_discovery_of_configured_host_aborts_without_a_request():
    flow = _flow("discovery", _entry())
    with _appliance():
        result = await _step(
            flow.async_step_discovery(api.discovery.DiscoveryInfo(host="192.0.2.1"))
        )
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
async def test_discovery_of_configured_appliance_updates_its_host():
    entry = _entry(username="admin", password="secret")
    flow = _flow("discovery", entry)
    with _appliance(_meta()):
        result = await _step(
            flow.async_step_discovery(api.discovery.DiscoveryInfo(host="192.0.2.2"))
        )

    assert result["reason"] == "already_configured"
    assert entry.data == {
        "username": "admin",
        "password": "secret",
        "base_url": "http://192.0.2.2",
    }
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry")


@pytest.mark.asyncio
async def test_reauth_updates_the_credentials():
    entry = _entry(username="admin", password="old")
    flow = _flow("reauth", entry, entry_id="entry")
    with _appliance(api.AuthenticationFailed(), _meta()):
        result = await _step(flow.async_step_reauth(entry.data))
        assert result["step_id"] == "auth"
        # the username is prefilled
        assert result["data_schema"]({"password": "x"})["username"] == "admin"

        result = await _step(
            flow.async_step_auth({"username": "admin", "password": "x"})
        )
        assert result["errors"] == {"base": "auth_failed"}
        result = await _step(
            flow.async_step_auth({"username": "admin", "password": "new"})
        )

    assert result["reason"] == "reauth_successful"
    assert entry.data == {
        "username": "admin",
        "password": "new",
        "base_url": "http://192.0.2.1",
    }
    flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry")


@pytest.mark.asyncio
async def test_reauth_aborts_for_another_appliance():
    entry = _entry(username="admin", password="old")
    flow = _flow("reauth", entry, entry_id="entry")
    with _appliance(_meta("FC1BFF000002")):
        await _step(flow.async_step_reauth(entry.data))
        result = await _step(
            flow.async_step_auth({"username": "admin", "password": "new"})
        )

    assert result["reason"] == "unique_id_mismatch"
    assert entry.data["password"] == "old"
    flow.hass.config_entries.async_schedule_reload.assert_not_called()
