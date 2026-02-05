import azure.functions as func
import logging
import json
import os
import time
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# ----------------------------
# Config (env vars)
# ----------------------------
REGISTRY_ADMIN_KEY = os.getenv("REGISTRY_ADMIN_KEY", "dev-admin-key")

COSMOS_ENDPOINT = os.getenv("COSMOS_ENDPOINT")
COSMOS_KEY = os.getenv("COSMOS_KEY")
COSMOS_DATABASE = os.getenv("COSMOS_DATABASE", "agent-registry")
COSMOS_CONTAINER = os.getenv("COSMOS_CONTAINER", "agents")

# ----------------------------
# Cosmos helpers
# ----------------------------
_cosmos_client: Optional[CosmosClient] = None


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing environment variable: {name}")
    return val


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cosmos_container():
    """
    Lazily create Cosmos client and return container client.
    Uses Cosmos keys (POC).
    Container partition key must be: /agent_id
    """
    global _cosmos_client

    endpoint = _require_env("COSMOS_ENDPOINT")
    key = _require_env("COSMOS_KEY")

    db_name = os.getenv("COSMOS_DATABASE", "agent-registry")
    container_name = os.getenv("COSMOS_CONTAINER", "agents")

    if _cosmos_client is None:
        _cosmos_client = CosmosClient(endpoint, credential=key)

    db = _cosmos_client.get_database_client(db_name)
    return db.get_container_client(container_name)


def _normalize_roles(value: Any) -> List[str]:
    """
    Accept roles as:
      - string "a,b,c"
      - list ["a","b"]
    Normalize to list[str].
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [r.strip() for r in value.split(",") if r.strip()]
    v = str(value).strip()
    return [v] if v else []


def _safe_lower(v: Any) -> str:
    return str(v).lower() if v is not None else ""


def _agent_upsert(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Upsert agent doc. Partition key is /agent_id.
    Uses id = agent_id to simplify.
    """
    container = _cosmos_container()

    agent_id = payload.get("agent_id") or payload.get("appid")
    name = payload.get("name")

    if not agent_id or not name:
        raise ValueError("Missing required fields: agent_id (or appid), name")

    now = _utc_now_iso()

    doc = dict(payload)
    doc["id"] = agent_id
    doc["agent_id"] = agent_id

    # optional: keep appid if supplied; default same as agent_id
    doc.setdefault("appid", agent_id)

    doc["name"] = name
    doc["roles"] = _normalize_roles(payload.get("roles"))
    doc.setdefault("enabled", True)

    # timestamps
    doc.setdefault("createdAt", now)
    doc["updatedAt"] = now

    container.upsert_item(doc)
    return doc


def _agent_get(agent_id: str) -> Optional[Dict[str, Any]]:
    container = _cosmos_container()
    try:
        return container.read_item(item=agent_id, partition_key=agent_id)
    except cosmos_exceptions.CosmosResourceNotFoundError:
        return None


def _agents_search(q: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List all agents, or filter by query string across:
      - agent_id
      - name
      - appid
      - roles[] (array)
      - optional 'test' field (if present in doc)
    """
    container = _cosmos_container()

    if not q or not q.strip():
        items = container.query_items(
            query="SELECT * FROM c",
            enable_cross_partition_query=True,
        )
        return list(items)

    q_norm = q.strip().lower()

    query = """
    SELECT * FROM c
    WHERE
        CONTAINS(LOWER(c.agent_id), @q)
        OR CONTAINS(LOWER(c.name), @q)
        OR CONTAINS(LOWER(c.appid), @q)
        OR EXISTS(
            SELECT VALUE r FROM r IN c.roles
