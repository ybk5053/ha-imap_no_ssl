"""The imap integration."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aioimaplib import IMAP4, AioImapException, Response
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    ServiceValidationError,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_ENABLE_PUSH, DOMAIN
from .coordinator import (
    ImapMessage,
    ImapPollingDataUpdateCoordinator,
    ImapPushDataUpdateCoordinator,
    connect_to_server,
)
from .errors import InvalidAuth, InvalidFolder

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONF_ENTRY = "entry"
CONF_SEEN = "seen"
CONF_UID = "uid"
CONF_TAG = "tag"
CONF_UNTAG = "untag"
CONF_TARGET_FOLDER = "target_folder"
CONF_ATTACHMENT = "attachment"
CONF_ATTACHMENT_FILTER = "attachment_filter"

_LOGGER = logging.getLogger(__name__)


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_SERVICE_UID_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENTRY): cv.string,
        vol.Required(CONF_UID): cv.string,
    }
)

SERVICE_SEEN_SCHEMA = _SERVICE_UID_SCHEMA
SERVICE_TAG_SCHEMA = _SERVICE_UID_SCHEMA.extend(
    {
        vol.Required(CONF_TAG): cv.string,
        vol.Optional(CONF_UNTAG): cv.boolean,
    }
)
SERVICE_MOVE_SCHEMA = _SERVICE_UID_SCHEMA.extend(
    {
        vol.Optional(CONF_SEEN): cv.boolean,
        vol.Required(CONF_TARGET_FOLDER): cv.string,
    }
)
SERVICE_DELETE_SCHEMA = _SERVICE_UID_SCHEMA
SERVICE_FETCH_TEXT_SCHEMA = _SERVICE_UID_SCHEMA


async def async_get_imap_client(hass: HomeAssistant, entry_id: str) -> IMAP4:
    """Get IMAP client and connect."""
    if hass.data[DOMAIN].get(entry_id) is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_entry",
        )
    entry = hass.config_entries.async_get_entry(entry_id)
    if TYPE_CHECKING:
        assert entry is not None
    try:
        client = await connect_to_server(entry.data)
    except InvalidAuth as exc:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from exc
    except InvalidFolder as exc:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_folder"
        ) from exc
    except (TimeoutError, AioImapException) as exc:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="imap_server_fail",
            translation_placeholders={"error": str(exc)},
        ) from exc
    return client


@callback
def raise_on_error(response: Response, translation_key: str) -> None:
    """Get error message from response."""
    if response.result != "OK":
        error: str = response.lines[0].decode("utf-8")
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=translation_key,
            translation_placeholders={"error": error},
        )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up imap services."""

    async def async_tag(call: ServiceCall) -> None:
        """Process mark as seen service call."""
        entry_id: str = call.data[CONF_ENTRY]
        uid: str = call.data[CONF_UID]
        untag = "+"
        if bool(call.data[CONF_UNTAG]):
            untag = "-"
        _LOGGER.debug(
            "Mark message %s as seen. Entry: %s",
            uid,
            entry_id,
        )
        client = await async_get_imap_client(hass, entry_id)
        try:
            response = await client.store(uid, "%sFLAGS (%s)" % (untag, call.data[CONF_TAG]))
        except (TimeoutError, AioImapException) as exc:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="imap_server_fail",
                translation_placeholders={"error": str(exc)},
            ) from exc
        raise_on_error(response, "tag_failed")
        await client.close()

    hass.services.async_register(DOMAIN, "tag", async_tag, SERVICE_TAG_SCHEMA)

    async def async_seen(call: ServiceCall) -> None:
        """Process mark as seen service call."""
        entry_id: str = call.data[CONF_ENTRY]
        uid: str = call.data[CONF_UID]
        _LOGGER.debug(
            "Mark message %s as seen. Entry: %s",
            uid,
            entry_id,
        )
        client = await async_get_imap_client(hass, entry_id)
        try:
            response = await client.store(uid, "+FLAGS (\\Seen)")
        except (TimeoutError, AioImapException) as exc:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="imap_server_fail",
                translation_placeholders={"error": str(exc)},
            ) from exc
        raise_on_error(response, "seen_failed")
        await client.close()

    hass.services.async_register(DOMAIN, "seen", async_seen, SERVICE_SEEN_SCHEMA)

    async def async_move(call: ServiceCall) -> None:
        """Process move email service call."""
        entry_id: str = call.data[CONF_ENTRY]
        uid: str = call.data[CONF_UID]
        seen = bool(call.data.get(CONF_SEEN))
        target_folder: str = call.data[CONF_TARGET_FOLDER]
        _LOGGER.debug(
            "Move message %s to folder %s. Mark as seen: %s. Entry: %s",
            uid,
            target_folder,
            seen,
            entry_id,
        )
        client = await async_get_imap_client(hass, entry_id)
        try:
            if seen:
                response = await client.store(uid, "+FLAGS (\\Seen)")
                raise_on_error(response, "seen_failed")
            response = await client.copy(uid, target_folder)
            raise_on_error(response, "copy_failed")
            response = await client.store(uid, "+FLAGS (\\Deleted)")
            raise_on_error(response, "delete_failed")
            response = await asyncio.wait_for(
                client.protocol.expunge(uid, by_uid=True), client.timeout
            )
            raise_on_error(response, "expunge_failed")
        except (TimeoutError, AioImapException) as exc:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="imap_server_fail",
                translation_placeholders={"error": str(exc)},
            ) from exc
        await client.close()

    hass.services.async_register(DOMAIN, "move", async_move, SERVICE_MOVE_SCHEMA)

    async def async_delete(call: ServiceCall) -> None:
        """Process deleting email service call."""
        entry_id: str = call.data[CONF_ENTRY]
        uid: str = call.data[CONF_UID]
        _LOGGER.debug(
            "Delete message %s. Entry: %s",
            uid,
            entry_id,
        )
        client = await async_get_imap_client(hass, entry_id)
        try:
            response = await client.store(uid, "+FLAGS (\\Deleted)")
            raise_on_error(response, "delete_failed")
            response = await asyncio.wait_for(
                client.protocol.expunge(uid, by_uid=True), client.timeout
            )
            raise_on_error(response, "expunge_failed")
        except (TimeoutError, AioImapException) as exc:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="imap_server_fail",
                translation_placeholders={"error": str(exc)},
            ) from exc
        await client.close()

    hass.services.async_register(DOMAIN, "delete", async_delete, SERVICE_DELETE_SCHEMA)

    async def async_fetch(call: ServiceCall) -> ServiceResponse:
        """Process fetch email service and return content."""
        entry_id: str = call.data[CONF_ENTRY]
        uid: str = call.data[CONF_UID]
        _LOGGER.debug(
            "Fetch text for message %s. Entry: %s",
            uid,
            entry_id,
        )
        client = await async_get_imap_client(hass, entry_id)
        try:
            response = await client.fetch(uid, "BODY.PEEK[]")
        except (TimeoutError, AioImapException) as exc:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="imap_server_fail",
                translation_placeholders={"error": str(exc)},
            ) from exc
        raise_on_error(response, "fetch_failed")
        message = ImapMessage(response.lines[1])
        await client.close()
        if call.data[CONF_ATTACHMENT]:
            if call.data[CONF_ATTACHMENT_FILTER]:
                attachments = []
                for attachment in message.attachments:
                    if call.data[CONF_ATTACHMENT_FILTER] in attachment["filename"]:
                        attachments.append(attachment)
            else:
                attachments = message.attachments
            return {
                "text": message.text,
                "sender": message.sender,
                "subject": message.subject,
                "uid": uid,
                "attachments": attachments,
            }
        return {
            "text": message.text,
            "sender": message.sender,
            "subject": message.subject,
            "uid": uid,
            "attachments": [],
        }

    hass.services.async_register(
        DOMAIN,
        "fetch",
        async_fetch,
        SERVICE_FETCH_TEXT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up imap from a config entry."""
    try:
        imap_client: IMAP4 = await connect_to_server(dict(entry.data))
    except InvalidAuth as err:
        raise ConfigEntryAuthFailed from err
    except InvalidFolder as err:
        raise ConfigEntryError("Selected mailbox folder is invalid.") from err
    except (TimeoutError, AioImapException) as err:
        raise ConfigEntryNotReady from err

    coordinator_class: type[
        ImapPushDataUpdateCoordinator | ImapPollingDataUpdateCoordinator
    ]
    enable_push: bool = entry.data.get(CONF_ENABLE_PUSH, True)
    if enable_push and imap_client.has_capability("IDLE"):
        coordinator_class = ImapPushDataUpdateCoordinator
    else:
        coordinator_class = ImapPollingDataUpdateCoordinator

    coordinator: ImapPushDataUpdateCoordinator | ImapPollingDataUpdateCoordinator = (
        coordinator_class(hass, imap_client, entry)
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.shutdown)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: (
            ImapPushDataUpdateCoordinator | ImapPollingDataUpdateCoordinator
        ) = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.shutdown()
    return unload_ok
