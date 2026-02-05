import azure.functions as func
import logging
import json
import os
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
            WHERE CONTAINS(LOWER(r), @q)
        )
        OR (IS_DEFINED(c.test) AND CONTAINS(LOWER(c.test), @q))
    """

    params = [{"name": "@q", "value": q_norm}]

    items = container.query_items(
        query=query,
        parameters=params,
        enable_cross_partition_query=True,
    )
    return list(items)


def _require_admin(req: func.HttpRequest) -> Optional[func.HttpResponse]:
    admin_key = req.headers.get("x-admin-key", "")
    if admin_key != REGISTRY_ADMIN_KEY:
        return func.HttpResponse("Unauthorized", status_code=401)
    return None


# ----------------------------
# Routes
# ----------------------------

# GET /api/registry/discover?q=...
@app.route(route="registry/discover", methods=["GET"])
def registry_discover(req: func.HttpRequest) -> func.HttpResponse:
    try:
        q = req.params.get("q")
        agents = _agents_search(q)

        return func.HttpResponse(
            json.dumps({"count": len(agents), "q": q, "agents": agents}),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.exception("Discovery failed")
        return func.HttpResponse(f"Discovery failed: {str(e)}", status_code=500)


# POST /api/registry/register  (create/update)
@app.route(route="registry/register", methods=["POST"])
def registry_register(req: func.HttpRequest) -> func.HttpResponse:
    unauthorized = _require_admin(req)
    if unauthorized:
        return unauthorized

    try:
        payload = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    try:
        doc = _agent_upsert(payload)
        return func.HttpResponse(
            json.dumps({"status": "ok", "agent": doc}),
            mimetype="application/json",
            status_code=200,
        )
    except ValueError as ve:
        return func.HttpResponse(str(ve), status_code=400)
    except Exception as e:
        logging.exception("Register failed")
        return func.HttpResponse(f"Register failed: {str(e)}", status_code=500)


# GET /api/registry/agents/{agent_id}
@app.route(route="registry/agents/{agent_id}", methods=["GET"])
def registry_get_agent(req: func.HttpRequest) -> func.HttpResponse:
    agent_id = req.route_params.get("agent_id")
    if not agent_id:
        return func.HttpResponse("Missing agent_id", status_code=400)

    try:
        doc = _agent_get(agent_id)
        if not doc:
            return func.HttpResponse("Not found", status_code=404)
        return func.HttpResponse(
            json.dumps(doc),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.exception("Get agent failed")
        return func.HttpResponse(f"Get failed: {str(e)}", status_code=500)


# PATCH /api/registry/agents/{agent_id}  (partial update)
@app.route(route="registry/agents/{agent_id}", methods=["PATCH"])
def registry_patch_agent(req: func.HttpRequest) -> func.HttpResponse:
    unauthorized = _require_admin(req)
    if unauthorized:
        return unauthorized

    agent_id = req.route_params.get("agent_id")
    if not agent_id:
        return func.HttpResponse("Missing agent_id", status_code=400)

    try:
        patch = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    try:
        existing = _agent_get(agent_id)
        if not existing:
            return func.HttpResponse("Not found", status_code=404)

        # merge allowed fields
        for field in ["name", "enabled", "appid", "test"]:
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

        return func.HttpResponse(
            json.dumps({"status": "ok", "agent": existing}),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.exception("Patch failed")
        return func.HttpResponse(f"Patch failed: {str(e)}", status_code=500)


# DELETE /api/registry/agents/{agent_id}
@app.route(route="registry/agents/{agent_id}", methods=["DELETE"])
def registry_delete_agent(req: func.HttpRequest) -> func.HttpResponse:
    unauthorized = _require_admin(req)
    if unauthorized:
        return unauthorized

    agent_id = req.route_params.get("agent_id")
    if not agent_id:
        return func.HttpResponse("Missing agent_id", status_code=400)

    try:
        container = _cosmos_container()
        container.delete_item(item=agent_id, partition_key=agent_id)
        return func.HttpResponse(
            json.dumps({"status": "ok", "deleted": agent_id}),
            mimetype="application/json",
            status_code=200,
        )
    except cosmos_exceptions.CosmosResourceNotFoundError:
        return func.HttpResponse("Not found", status_code=404)
    except Exception as e:
        logging.exception("Delete failed")
        return func.HttpResponse(f"Delete failed: {str(e)}", status_code=500)
