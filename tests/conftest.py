# Shared fixtures for integration tests that need a real, throwaway FalkorDB
# graph. These tests are integration, not unit, tests: they need the
# FalkorDB container from infra/docker-compose.yml running and reachable at
# the settings in .env — skipped automatically if it isn't (see
# `falkordb_or_skip` below), so `pytest` still passes in an environment with
# no database, just with these tests reported as skipped rather than failed.

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from core.config import Settings
from core.graph import GraphHandle


@pytest_asyncio.fixture
async def falkordb_or_skip():
    """Skip the test cleanly if FalkorDB isn't reachable, rather than
    failing every integration test with a connection traceback."""
    settings = Settings()
    probe = GraphHandle("__aftershock_ping__", settings)
    try:
        alive = await probe.ping()
    except Exception:
        alive = False
    finally:
        await probe.close()
    if not alive:
        pytest.skip("FalkorDB is not reachable (see infra/docker-compose.yml)")
    return settings


@pytest_asyncio.fixture
async def graph(falkordb_or_skip: Settings):
    """A fresh, uniquely-named graph, deleted before and after the test so a
    failed prior run can never leak state into this one."""
    name = f"aftershock_test_{uuid.uuid4().hex[:12]}"
    handle = GraphHandle(name, falkordb_or_skip)
    await handle.delete()
    yield handle
    await handle.delete()
    await handle.close()


@pytest_asyncio.fixture
async def graph_factory(falkordb_or_skip: Settings):
    """For tests needing more than one graph at once (e.g. diff_facts' old
    vs new side). Yields a factory; every graph it creates is torn down at
    the end of the test, in creation order."""
    created: list[GraphHandle] = []

    async def _make(suffix: str = "", settings: Settings | None = None) -> GraphHandle:
        name = f"aftershock_test_{uuid.uuid4().hex[:12]}{('_' + suffix) if suffix else ''}"
        handle = GraphHandle(name, settings or falkordb_or_skip)
        await handle.delete()
        created.append(handle)
        return handle

    yield _make

    for handle in created:
        await handle.delete()
        await handle.close()
