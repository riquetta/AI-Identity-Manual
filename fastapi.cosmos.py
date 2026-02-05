# app.py
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from azure.cosmos import CosmosClient, exceptions as cosmos_exceptions
from fastapi import Depends, FastAPI, Header, HTTPException, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Agent Registry API", version="1.0.0")

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


def _agent_upsert(payload: Dict[str, Any]) -> Dict[str, Any]:
    container = _cosmos_container()

    agent_id = payload.get("agent_id") or payload.get("appid")
    name = payload.get("name")

    if not agent_id or not name:
        raise ValueError("Missing required fields: agent_id (or appid), name")

    now = _utc_now_iso()

    doc = dict(payload)
    doc["id"] = agent_id
    doc["agent_id"] = agent_id
    doc.setdefault("appid", agent_id)
    doc["name"] = name
    doc["roles"] = _normalize_roles(payload.get("roles"))
    doc.setdefault("enabled", True)

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


def _agents_list() -> List[Dict[str, Any]]:
    container = _cosmos_container()
    items = container.query_items(
        query="SELECT * FROM c",
        enable_cross_partition_query=True,
    )
    return list(items)


# ----------------------------
# Auth dependency
# ----------------------------
def require_admin(x_admin_key: Optional[str] = Header(default=None)):
    if x_admin_key != REGISTRY_ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ----------------------------
# Request models
# ----------------------------
class RegisterRequest(BaseModel):
    agent_id: Optional[str] = None
    appid: Optional[str] = None
    name: str
    roles: Optional[Any] = None  # accepts list or comma string
    enabled: Optional[bool] = True


class PatchRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    appid: Optional[str] = None
    roles: Optional[Any] = None  # accepts list or comma string


# ----------------------------
# Routes
# ----------------------------

# GET /registry/discover
@app.get("/registry/discover")
def registry_discover():
    try:
        agents = _agents_list()
        return {"agents": agents}
    except Exception as e:
        logging.exception("Discovery failed")
        raise HTTPException(status_code=500, detail=f"Discovery failed: {str(e)}")


# POST /registry/register (create/update)
@app.post("/registry/register", dependencies=[Depends(require_admin)])
def registry_register(payload: RegisterRequest):
    try:
        doc = _agent_upsert(payload.model_dump(exclude_none=True))
        return {"status": "ok", "agent": doc}
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logging.exception("Register failed")
        raise HTTPException(status_code=500, detail=f"Register failed: {str(e)}")


# GET /registry/agents/{agent_id}
@app.get("/registry/agents/{agent_id}")
def registry_get_agent(agent_id: str = Path(..., min_length=1)):
    try:
        doc = _agent_get(agent_id)
        if not doc:
            raise HTTPException(status_code=404, detail="Not found")
        return doc
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("Get agent failed")
        raise HTTPException(status_code=500, detail=f"Get failed: {str(e)}")


# PATCH /registry/agents/{agent_id} (partial update)
@app.patch("/registry/agents/{agent_id}", dependencies=[Depends(require_admin)])
def registry_patch_agent(
    payload: PatchRequest,
    agent_id: str = Path(..., min_length=1),
):
    try:
        existing = _agent_get(agent_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Not found")

        patch = payload.model_dump(exclude_unset=True)

        for field in ["name", "enabled", "appid"]:
            if field in patch:
                existing[field] = patch[field]

        if "roles" in patch:
            existing["roles"] = _normalize_roles(patch["roles"])

        # prevent changing partition keys / ids
        existing["id"] = agent_id
        existing["agent_id"] = agent_id
        existing["updatedAt"] = _utc_now_iso()

        container = _cosmos_container()
        container.replace_item(item=existing["id"], body=existing)

        return {"status": "ok", "agent": existing}
    except HTTPException:
        raise
    except Exception as e:
        logging.exception("Patch failed")
        raise HTTPException(status_code=500, detail=f"Patch failed: {str(e)}")


# DELETE /registry/agents/{agent_id}
@app.delete("/registry/agents/{agent_id}", dependencies=[Depends(require_admin)])
def registry_delete_agent(agent_id: str = Path(..., min_length=1)):
    try:
        container = _cosmos_container()
        container.delete_item(item=agent_id, partition_key=agent_id)
        return {"status": "ok", "deleted": agent_id}
    except cosmos_exceptions.CosmosResourceNotFoundError:
        raise HTTPException(status_code=404, detail="Not found")
    except Exception as e:
        logging.exception("Delete failed")
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")
