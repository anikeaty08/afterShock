# Aftershock — Core: Graph access
#
# Thin layer over graphrag_sdk's FalkorDBConnection plus the few raw admin
# operations the SDK doesn't wrap (GRAPH.COPY, GRAPH.LIST): every module in
# this package reads/writes FalkorDB through here rather than opening its
# own client, so there's exactly one place that knows the connection pool
# and the named-Cypher convention (§8's catalog, loaded by name).

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from falkordb.asyncio import FalkorDB
from graphrag_sdk.core.connection import ConnectionConfig, FalkorDBConnection

from core.config import Settings, get_settings

logger = logging.getLogger(__name__)

CYPHER_DIR = Path(__file__).parent / "cypher"

ONTOLOGY_GRAPH_SUFFIX = "__ontology"
"""graphrag_sdk persists each data graph's ontology in a paired graph named
``<data_graph>__ontology`` (storage/ontology_store.py). Copy and delete must
treat the pair as one unit, or a deleted graph leaves a stale ontology behind
that rejects the next run's (legitimately changed) ontology outright."""


@lru_cache
def _load_cypher_file(name: str) -> str:
    """Read one named .cypher file from core/cypher/, cached for process life.

    ``name`` is the file stem (e.g. ``"q1_scope_entities"``), never a path —
    keeps callers from ever formatting a filesystem path together with
    user-influenced input.
    """
    path = CYPHER_DIR / f"{name}.cypher"
    if not path.exists():
        raise FileNotFoundError(f"No such Cypher catalog entry: {name} ({path})")
    return path.read_text(encoding="utf-8")


def cypher_text(name: str) -> str:
    """Public accessor for a catalog query's raw text (tests, /how page)."""
    return _load_cypher_file(name)


class GraphHandle:
    """One open connection to one named FalkorDB graph.

    Wraps ``FalkorDBConnection`` (retries, circuit breaker — see the SDK's
    ``core/connection.py``) for Cypher, and a plain ``falkordb.asyncio``
    client for the handful of admin commands the SDK doesn't expose.
    """

    def __init__(self, graph_name: str, settings: Settings | None = None) -> None:
        self.graph_name = graph_name
        self._settings = settings or get_settings()
        self._config = ConnectionConfig(
            host=self._settings.falkordb_host,
            port=self._settings.falkordb_port,
            username=self._settings.falkordb_username,
            password=self._settings.falkordb_password,
            graph_name=graph_name,
        )
        self._conn = FalkorDBConnection(self._config)
        self._admin: FalkorDB | None = None

    async def _ensure_admin(self) -> FalkorDB:
        if self._admin is None:
            self._admin = FalkorDB(
                host=self._settings.falkordb_host,
                port=self._settings.falkordb_port,
                username=self._settings.falkordb_username,
                password=self._settings.falkordb_password,
            )
        return self._admin

    # ── Cypher ──────────────────────────────────────────────────

    async def run(self, cypher: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        """Execute raw Cypher text against this graph. Returns the driver's
        ``QueryResult`` (has ``.result_set``, ``.header``, stats)."""
        return await self._conn.query(cypher, params, **kwargs)

    async def run_named(
        self, name: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        """Execute a named entry from the Cypher catalog (core/cypher/*.cypher)."""
        return await self.run(_load_cypher_file(name), params, **kwargs)

    async def rows(self, cypher: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        """Convenience: run and return `.result_set` rows, or `[]` on no match."""
        result = await self.run(cypher, params)
        return list(getattr(result, "result_set", None) or [])

    async def rows_named(self, name: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        result = await self.run_named(name, params)
        return list(getattr(result, "result_set", None) or [])

    # ── Admin ───────────────────────────────────────────────────

    async def copy_to(self, dest_graph_name: str) -> GraphHandle:
        """GRAPH.COPY this graph to ``dest_graph_name`` (§7.3 step 2).

        The source stays fully readable/writable during the copy (FalkorDB
        guarantee — see docs `commands/graph.copy`); the destination is a
        consistent snapshot including indexes.
        """
        admin = await self._ensure_admin()
        graph = admin.select_graph(self.graph_name)
        await graph.copy(dest_graph_name)
        # Copy the paired ontology graph too, so the scratch graph evolves
        # from exactly docs_main's registered ontology, not a fresh default.
        existing = set(await admin.list_graphs())
        src_onto = self.graph_name + ONTOLOGY_GRAPH_SUFFIX
        if src_onto in existing:
            await admin.select_graph(src_onto).copy(dest_graph_name + ONTOLOGY_GRAPH_SUFFIX)
        logger.info("GRAPH.COPY %s -> %s", self.graph_name, dest_graph_name)
        return GraphHandle(dest_graph_name, self._settings)

    async def delete(self) -> None:
        """GRAPH.DELETE this graph and its paired ``__ontology`` graph (fast;
        safe to call on a graph that may not exist — the SDK's
        ``delete_graph`` swallows the empty/invalid case)."""
        await self._conn.delete_graph()
        admin = await self._ensure_admin()
        onto = self.graph_name + ONTOLOGY_GRAPH_SUFFIX
        if onto in set(await admin.list_graphs()):
            await admin.select_graph(onto).delete()

    async def exists(self) -> bool:
        admin = await self._ensure_admin()
        names = await admin.list_graphs()
        return self.graph_name in names

    async def ping(self) -> bool:
        return await self._conn.ping()

    async def close(self) -> None:
        await self._conn.close()
        if self._admin is not None:
            await self._admin.aclose()


async def list_graphs(settings: Settings | None = None) -> list[str]:
    """GRAPH.LIST — every graph currently on the server (docs_main plus any
    live docs_pr_* scratch graphs)."""
    settings = settings or get_settings()
    admin = FalkorDB(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        username=settings.falkordb_username,
        password=settings.falkordb_password,
    )
    try:
        return list(await admin.list_graphs())
    finally:
        await admin.aclose()


def scratch_graph_name(pr: int, head_sha: str) -> str:
    """Deterministic scratch-graph name for a PR's working copy (§7.3 step 2).

    ``head_sha`` is truncated to 7 chars purely for a readable graph name —
    collisions are not a practical concern (GitHub's own abbreviated-SHA
    convention uses the same length).
    """
    return f"docs_pr_{pr}_{head_sha[:7]}"
