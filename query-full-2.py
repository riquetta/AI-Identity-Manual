import azure.functions as func
import logging
import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple
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

# Discovery tuning
DISCOVERY_TOP_K = int(os.getenv("DISCOVERY_TOP_K", "20"))        # candidates returned by query
DISCOVERY_HYDRATE_MAX = int(os.getenv("DISCOVERY_HYDRATE_MAX", "10"))  # max full docs when include_full=true

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


def _safe_str(v: Any) -> str:
    return str(v) if v is not None else ""


def _safe_lower(v: Any) -> str:
    return _safe_str(v).strip().lower()


def _to_bool(v: Optional[str], default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


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


def _apply_search_fields(doc: Dict[str, Any]) -> None:
    """
    Add denormalized lowercase fields for faster search (avoid LOWER() in SQL).
    """
    roles = doc.get("roles", [])
    if not isinstance(roles, list):
        roles = _normalize_roles(roles)

    doc["name_lc"] = _safe_lower(doc.get("name"))
    doc["agent_id_lc"] = _safe_lower(doc.get("agent_id"))
    doc["appid_lc"] = _safe_lower(doc.get("appid"))
    doc["test_lc"] = _safe_lower(doc.get("test"))
    doc["roles_lc"] = [_safe_lower(r) for r in roles if _safe_lower(r)]


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

    # search fields
    _apply_search_fields(doc)

    container.upsert_item(doc)
    return doc


def _agent_get(agent_id: str) -> Optional[Dict[str, Any]]:
    container = _cosmos_container()
    try:
        return container.read_item(item=agent_id, partition_key=agent_id)
    except cosmos_exceptions.CosmosResourceNotFoundError:
        return None


def _search_projection_fields() -> str:
    """
    Keep discovery payload small for speed.
    """
    return """
    c.id,
    c.agent_id,
    c.name,
    c.appid,
    c.enabled,
    c.roles,
    c.test,
    c.updatedAt,
    c.createdAt,
    c.agent_id_lc,
    c.name_lc,
    c.appid_lc,
    c.test_lc,
    c.roles_lc
    """


def _query_candidates_staged(q: Optional[str], top_k: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Fast staged retrieval:
      1) exact match
      2) prefix match
      3) contains fallback (only if needed)
    Returns candidates + diagnostics.
    """
    container = _cosmos_container()

    if not q or not q.strip():
        # No query: lightweight list (top_k)
        query = f"SELECT TOP @top { _search_projection_fields() } FROM c"
        params = [{"name": "@top", "value": top_k}]
        items = list(
            container.query_items(
                query=query,
                parameters=params,
                enable_cross_partition_query=True,
            )
        )
        return items, {"strategy": "list", "stage_used": "all"}

    qn = _safe_lower(q)

    # Stage 1: exact (fastest)
    stage1_query = f"""
    SELECT TOP @top { _search_projection_fields() } FROM c
    WHERE
        c.agent_id_lc = @q
        OR c.name_lc = @q
        OR c.appid_lc = @q
        OR c.test_lc = @q
        OR ARRAY_CONTAINS(c.roles_lc, @q, true)
    """
    params = [{"name": "@top", "value": top_k}, {"name": "@q", "value": qn}]
    stage1 = list(
        container.query_items(
            query=stage1_query,
            parameters=params,
            enable_cross_partition_query=True,
        )
    )
    if stage1:
        return stage1, {"strategy": "staged", "stage_used": "exact"}

    # Stage 2: prefix
    stage2_query = f"""
    SELECT TOP @top { _search_projection_fields() } FROM c
    WHERE
        STARTSWITH(c.agent_id_lc, @q)
        OR STARTSWITH(c.name_lc, @q)
        OR STARTSWITH(c.appid_lc, @q)
        OR STARTSWITH(c.test_lc, @q)
        OR EXISTS(
            SELECT VALUE r FROM r IN c.roles_lc
            WHERE STARTSWITH(r, @q)
        )
    """
    stage2 = list(
        container.query_items(
            query=stage2_query,
            parameters=params,
            enable_cross_partition_query=True,
        )
    )
    if stage2:
        return stage2, {"strategy": "staged", "stage_used": "prefix"}

    # Stage 3: contains fallback (most expensive)
    stage3_query = f"""
    SELECT TOP @top { _search_projection_fields() } FROM c
    WHERE
        CONTAINS(c.agent_id_lc, @q)
        OR CONTAINS(c.name_lc, @q)
        OR CONTAINS(c.appid_lc, @q)
        OR CONTAINS(c.test_lc, @q)
        OR EXISTS(
            SELECT VALUE r FROM r IN c.roles_lc
            WHERE CONTAINS(r, @q)
        )
    """
    stage3 = list(
        container.query_items(
            query=stage3_query,
            parameters=params,
            enable_cross_partition_query=True,
        )
    )
    return stage3, {"strategy": "staged", "stage_used": "contains"}


def _score_agent_match(agent: Dict[str, Any], q: str) -> Dict[str, Any]:
    """
    Heuristic scoring for best-match selection + justification.
    Uses normalized fields when present.
    """
    qn = _safe_lower(q)
    if not qn:
        return {"score": 0, "reasons": []}

    reasons: List[str] = []
    score = 0

    agent_id = _safe_lower(agent.get("agent_id_lc") or agent.get("agent_id"))
    name = _safe_lower(agent.get("name_lc") or agent.get("name"))
    appid = _safe_lower(agent.get("appid_lc") or agent.get("appid"))
    test_field = _safe_lower(agent.get("test_lc") or agent.get("test"))

    roles_lc = agent.get("roles_lc", None)
    if isinstance(roles_lc, list):
        roles = [_safe_lower(r) for r in roles_lc if _safe_lower(r)]
    else:
        roles = [_safe_lower(r) for r in agent.get("roles", []) if _safe_lower(r)]

    # Strong signals
    if name == qn:
        score += 120
        reasons.append(f"exact name match ('{agent.get('name')}')")
    elif name.startswith(qn):
        score += 85
        reasons.append("name prefix match")
    elif qn in name:
        score += 70
        reasons.append("name contains search term")

    if agent_id == qn:
        score += 115
        reasons.append(f"exact agent_id match ('{agent.get('agent_id')}')")
    elif agent_id.startswith(qn):
        score += 80
        reasons.append("agent_id prefix match")
    elif qn in agent_id:
        score += 65
        reasons.append("agent_id contains search term")

    if appid == qn:
        score += 95
        reasons.append(f"exact appid match ('{agent.get('appid')}')")
    elif appid.startswith(qn):
        score += 70
        reasons.append("appid prefix match")
    elif qn in appid:
        score += 55
        reasons.append("appid contains search term")

    # Role match
    exact_roles = [r for r in roles if r == qn]
    prefix_roles = [r for r in roles if r.startswith(qn)]
    contains_roles = [r for r in roles if qn in r]

    if exact_roles:
        score += 80
        reasons.append(f"exact role match: {exact_roles[0]}")
    elif prefix_roles:
        score += min(len(prefix_roles) * 25, 60)
        reasons.append(f"role prefix match: {prefix_roles[0]}")
    elif contains_roles:
        score += min(len(contains_roles) * 20, 50)
        reasons.append(f"matched role(s): {', '.join(contains_roles[:3])}")

    # Optional test field
    if test_field:
        if test_field == qn:
            score += 85
            reasons.append(f"exact test match ('{agent.get('test')}')")
        elif test_field.startswith(qn):
            score += 55
            reasons.append("test prefix match")
        elif qn in test_field:
            score += 40
            reasons.append("test contains search term")

    # Enabled preference
    if agent.get("enabled", True):
        score += 10
        reasons.append("agent is enabled")
    else:
        reasons.append("agent is disabled")

    return {"score": score, "reasons": reasons}


def _rank_candidates(candidates: List[Dict[str, Any]], q: str) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for c in candidates:
        scored = _score_agent_match(c, q)
        ranked.append(
            {
                "agent": c,
                "score": scored["score"],
                "reasons": scored["reasons"],
            }
        )
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


def _hydrate_full_docs(candidates: List[Dict[str, Any]], max_items: int) -> List[Dict[str, Any]]:
    """
    Optional: read full docs for top N candidates.
    """
    out: List[Dict[str, Any]] = []
    for c in candidates[:max_items]:
        agent_id = c.get("agent_id")
        if not agent_id:
            continue
        full = _agent_get(agent_id)
        if full:
            out.append(full)
    return out


def _require_admin(req: func.HttpRequest) -> Optional[func.HttpResponse]:
    admin_key = req.headers.get("x-admin-key", "")
    if admin_key != REGISTRY_ADMIN_KEY:
        return func.HttpResponse("Unauthorized", status_code=401)
    return None


# ----------------------------
# Routes
# ----------------------------

# GET /api/registry/discover?q=...&debug=true|false&include_full=true|false&top=20
@app.route(route="registry/discover", methods=["GET"])
def registry_discover(req: func.HttpRequest) -> func.HttpResponse:
    total_start = time.perf_counter()

    try:
        q = _safe_str(req.params.get("q")).strip()
        debug = _to_bool(req.params.get("debug"), default=False)
        include_full = _to_bool(req.params.get("include_full"), default=False)

        top_param = req.params.get("top")
        top_k = DISCOVERY_TOP_K
        if top_param and top_param.isdigit():
            top_k = max(1, min(int(top_param), 100))  # hard cap

        # 1) Retrieval
        q_start = time.perf_counter()
        candidates, diagnostics = _query_candidates_staged(q if q else None, top_k)
        cosmos_query_ms = round((time.perf_counter() - q_start) * 1000, 2)

        # No query: lightweight list response
        if not q:
            hydration_ms = 0.0
            agents_payload: Any = candidates

            if include_full:
                h_start = time.perf_counter()
                agents_payload = _hydrate_full_docs(candidates, min(top_k, DISCOVERY_HYDRATE_MAX))
                hydration_ms = round((time.perf_counter() - h_start) * 1000, 2)

            total_ms = round((time.perf_counter() - total_start) * 1000, 2)
            return func.HttpResponse(
                json.dumps(
                    {
                        "count": len(candidates),
                        "q": None,
                        "message": "No query provided. Returned registry list.",
                        "timing_ms": {
                            "cosmos_query_ms": cosmos_query_ms,
                            "ranking_ms": 0.0,
                            "hydration_ms": hydration_ms,
                            "total_ms": total_ms,
                        },
                        "diagnostics": diagnostics,
                        "agents": agents_payload if debug or include_full else candidates,
                    }
                ),
                mimetype="application/json",
                status_code=200,
            )

        # 2) Ranking
        r_start = time.perf_counter()
        ranked = _rank_candidates(candidates, q)
        ranking_ms = round((time.perf_counter() - r_start) * 1000, 2)

        if not ranked:
            total_ms = round((time.perf_counter() - total_start) * 1000, 2)
            return func.HttpResponse(
                json.dumps(
                    {
                        "count": 0,
                        "q": q,
                        "best_match": None,
                        "best_match_score": 0,
                        "best_match_reasons": [],
                        "justification": f"No agents matched '{q}'.",
                        "timing_ms": {
                            "cosmos_query_ms": cosmos_query_ms,
                            "ranking_ms": ranking_ms,
                            "hydration_ms": 0.0,
                            "total_ms": total_ms,
                        },
                        "diagnostics": diagnostics,
                        "candidates": [] if debug else None,
                    }
                ),
                mimetype="application/json",
                status_code=200,
            )

        top = ranked[0]
        best_agent = top["agent"]
        best_score = top["score"]
        best_reasons = top["reasons"]

        # Optional full hydration (best only by default)
        hydration_ms = 0.0
        if include_full:
            h_start = time.perf_counter()
            full_best = _agent_get(best_agent.get("agent_id"))
            if full_best:
                best_agent = full_best
            hydration_ms = round((time.perf_counter() - h_start) * 1000, 2)

        name_or_id = best_agent.get("name") or best_agent.get("agent_id")
        justification = (
            f"Based on your search '{q}', the best agent fit is '{name_or_id}' "
            f"(agent_id={best_agent.get('agent_id')}) with score {best_score}. "
            f"Reason(s): {'; '.join(best_reasons[:4])}."
        )

        total_ms = round((time.perf_counter() - total_start) * 1000, 2)

        response_obj: Dict[str, Any] = {
            "count": len(candidates),
            "q": q,
            "best_match": best_agent,
            "best_match_score": best_score,
            "best_match_reasons": best_reasons,
            "justification": justification,
            "timing_ms": {
                "cosmos_query_ms": cosmos_query_ms,
                "ranking_ms": ranking_ms,
                "hydration_ms": hydration_ms,
                "total_ms": total_ms,
            },
            "diagnostics": diagnostics,
        }

        if debug:
            response_obj["candidates_ranked"] = ranked

        return func.HttpResponse(
            json.dumps(response_obj),
            mimetype="application/json",
            status_code=200,
        )

    except Exception as e:
        logging.exception("Discovery failed")
        total_ms = round((time.perf_counter() - total_start) * 1000, 2)
        return func.HttpResponse(
            json.dumps(
                {
                    "error": f"Discovery failed: {str(e)}",
                    "timing_ms": {
                        "cosmos_query_ms": None,
                        "ranking_ms": None,
                        "hydration_ms": None,
                        "total_ms": total_ms,
                    },
                }
            ),
            mimetype="application/json",
            status_code=500,
        )


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

        # keep normalized search fields fresh
        _apply_search_fields(existing)

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
