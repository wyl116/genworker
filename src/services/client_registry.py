"""Shared keyed-client registry helpers for database service clients."""
from __future__ import annotations

from typing import Awaitable, Callable, TypeVar

ClientT = TypeVar("ClientT")
ConfigT = TypeVar("ConfigT")


def build_database_scoped_config(
    database: str,
    *,
    config_cls: type[ConfigT],
) -> ConfigT:
    """Build a config object for one named database using global settings."""
    from src.common.settings import get_settings

    base_config = config_cls.from_settings(get_settings())
    return base_config.with_database(database)


def get_or_create_keyed_client(
    clients: dict[str, ClientT],
    *,
    key: str,
    factory: Callable[[], ClientT],
) -> ClientT:
    """Get an existing client for a key, or create and store one."""
    client = clients.get(key)
    if client is None:
        client = factory()
        clients[key] = client
    return client


async def init_keyed_client(
    clients: dict[str, ClientT],
    *,
    key: str,
    lock,
    factory: Callable[[], ClientT],
    initializer: Callable[[ClientT], Awaitable[None]],
) -> ClientT:
    """Create/replace one keyed client under a lock and eagerly initialize it."""
    async with lock:
        client = factory()
        clients[key] = client
        await initializer(client)
        return client


async def close_keyed_clients(
    clients: dict[str, ClientT],
    *,
    database: str | None,
    close_client: Callable[[ClientT], Awaitable[None]],
) -> None:
    """Close one keyed client or all keyed clients in a registry."""
    if database:
        client = clients.pop(database, None)
        if client is not None:
            await close_client(client)
        return

    all_clients = tuple(clients.values())
    clients.clear()
    for client in all_clients:
        await close_client(client)
