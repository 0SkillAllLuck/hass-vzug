from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
