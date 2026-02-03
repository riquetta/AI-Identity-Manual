import azure.functions as func
import logging
import json
import os
from typing import Any, Dict
from openai import AzureOpenAI

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


# ----------------------------
# Config (env vars)
# ----------------------------
AOAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AOAI_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AOAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT")
AOAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")

# POC registry settings (Agent metadata)
REGISTRY_PATH = os.getenv("AGENT_REGISTRY_PATH", "agent_registry.json")
REGISTRY_ADMIN_KEY = os.getenv("REGISTRY_ADMIN_KEY", "dev-admin-key")


# ----------------------------
# Helpers
# ----------------------------
def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing environment variable: {name}")
    return val


def _load_registry() -> Dict[str, Any]:
    if not os.path.exists(REGISTRY_PATH):
        return {"agents": {}}
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_registry(data: Dict[str, Any]) -> None:
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _aoai_client() -> AzureOpenAI:
    # Fail fast if not set
    endpoint = _require_env("AZURE_OPENAI_ENDPOINT")
    key = _require_env("AZURE_OPENAI_API_KEY")
    _require_env("AZURE_OPENAI_DEPLOYMENT")

    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=key,
        api_version=AOAI_API_VERSION,
    )


# ----------------------------
# Existing route (unchanged)
# ----------------------------
@app.route(route="defaultroute", methods=["GET", "POST"])
def fastapientra(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Python HTTP trigger function processed a request.")

    name = req.params.get("name")
    if not name:
        try:
            req_body = req.get_json()
            name = req_body.get("name")
        except Exception:
            name = None

    if name:
        return func.HttpResponse(f"Hello, {name}. This HTTP triggered function executed successfully.")
    else:
        return func.HttpResponse(
            "This HTTP triggered function executed successfully. Pass a name in the query string or in the request body for a personalized response.",
            status_code=200,
        )


# ----------------------------
# Agent Registry: discover
# GET /api/registry/discover
# ----------------------------
@app.route(route="registry/discover", methods=["GET"])
def registry_discover(req: func.HttpRequest) -> func.HttpResponse:
    reg = _load_registry()
    return func.HttpResponse(
        json.dumps({"agents": list(reg["agents"].values())}),
        mimetype="application/json",
        status_code=200,
    )


# ----------------------------
# Agent Registry: register (POC)
# POST /api/registry/register
# Body: { "appid": "...", "name": "...", ... }
# Header: x-admin-key: <REGISTRY_ADMIN_KEY>
# ----------------------------
@app.route(route="registry/register", methods=["POST"])
def registry_register(req: func.HttpRequest) -> func.HttpResponse:
    admin_key = req.headers.get("x-admin-key", "")
    if admin_key != REGISTRY_ADMIN_KEY:
        return func.HttpResponse("Unauthorized", status_code=401)

    try:
        payload = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    appid = payload.get("appid")
    name = payload.get("name")
    if not appid or not name:
        return func.HttpResponse("Missing required fields: appid, name", status_code=400)

    reg = _load_registry()
    reg["agents"][appid] = payload
    _save_registry(reg)

    return func.HttpResponse(
        json.dumps({"status": "ok", "agent": reg["agents"][appid]}),
        mimetype="application/json",
        status_code=200,
    )


# ----------------------------
# Agent Chat
# POST /api/chat
# Body: { "message": "..." }
# Headers injected by APIM:
#   x-agent-appid: <JWT appid claim>
#   x-agent-roles: comma-separated roles
# ----------------------------
@app.route(route="chat", methods=["POST"])
def chat(req: func.HttpRequest) -> func.HttpResponse:
    agent_appid = req.headers.get("x-agent-appid")
    agent_roles = req.headers.get("x-agent-roles", "")

    if not agent_appid:
        # If you call this directly (bypassing APIM), you’ll see this.
        return func.HttpResponse("Missing x-agent-appid (expected APIM to set it)", status_code=401)

    reg = _load_registry()
    agent_meta = reg["agents"].get(agent_appid)
    if not agent_meta:
        return func.HttpResponse("Agent not registered", status_code=403)

    try:
        body = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    message = (body.get("message") or "").strip()
    if not message:
        return func.HttpResponse("Missing 'message' in body", status_code=400)

    # Optional: simple policy in backend (defense in depth)
    # Example: require role string to include "agent.chat.invoke"
    # (APIM should already enforce; this is extra.)
    if "agent.chat.invoke" not in agent_roles.split(","):
        return func.HttpResponse("Missing required role", status_code=403)

    client = _aoai_client()
    deployment = _require_env("AZURE_OPENAI_DEPLOYMENT")

    try:
        resp = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": f"You are agent '{agent_meta.get('name')}'. Be concise."},
                {"role": "user", "content": message},
            ],
        )
        answer = resp.choices[0].message.content or ""
    except Exception as e:
        logging.exception("Azure OpenAI call failed")
        return func.HttpResponse(f"Azure OpenAI call failed: {str(e)}", status_code=502)

    out = {
        "agent_appid": agent_appid,
        "agent_roles": agent_roles,
        "agent_name": agent_meta.get("name"),
        "answer": answer,
    }
    return func.HttpResponse(json.dumps(out), mimetype="application/json", status_code=200)
