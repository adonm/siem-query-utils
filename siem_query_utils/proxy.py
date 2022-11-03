# pylint: disable=line-too-long
import base64
import hashlib
import json
import logging
import os

import httpx
import httpx_cache
from fastapi import Depends, APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from .api import azcli, cache

logger = logging.getLogger("uvicorn.error")

router = APIRouter()
sessions = {}  # global cache of sessions and async clients


@cache.memoize(ttl=60 * 60)
def httpx_client(proxy):
    # cache client objects for an hour
    proxy_client = httpx_cache.Client(**proxy, timeout=None)
    proxy_client.headers["host"] = proxy_client.base_url.host
    return proxy_client


@cache.memoize(ttl=60 * 60)
def boot(secret):
    # cache session creds for an hour
    secret = azcli(["keyvault", "secret", "show", "--id", secret])
    if "error" in secret:
        logger.warning(secret["error"])
        raise HTTPException(403, "KEYVAULT_SESSION_SECRET not available")
    return secret["value"]


def _session(request: Request, key="session") -> dict:
    # Create or retrieve a session
    if not request.session.get("key") or "main_path" not in sessions.get(request.session["key"], {}):
        if "KEYVAULT_SESSION_SECRET" not in os.environ:
            raise HTTPException(403, "KEYVAULT_SESSION_SECRET not available")
        session_data = load_session(boot(os.environ["KEYVAULT_SESSION_SECRET"]))
        if session_data["key"] not in sessions:  # keep existing session if config the same
            sessions[session_data["key"]] = session_data
        request.session["key"] = session_data["key"]  # save ref to session in users cookie
    return sessions[request.session["key"]][key]


default_session = json.dumps({
    "proxy_httpbin": {
        "base_url": "https://httpbin.org",
        # "params": {"get": "params"},
        # "headers": {"http": "headers"},
        # "cookies": {"cookie": "jar"},
    },
    "proxy_jupyter": {"base_url": "https://wagov.github.io/wasoc-jupyterlite"},
    "main_path": "/jupyter/lab/index.html",  # this is redirected to when index is loaded
})


@router.get("/main_path")
def main_path(request: Request):
    return RedirectResponse(request.scope.get("root_path") + _session(request)["main_path"])


def encode_session(session: dict):
    return base64.b64encode(json.dumps(session, sort_keys=True).encode("utf8"))


@router.post("/config_base64")
def config_base64(session: str = encode_session(json.loads(default_session))):
    """
    Basic validation for session confi in base64 format, to save place the
    `base64` string into the keyvault secret defined with `KEYVAULT_SESSION_SECRET`
    """
    return load_session(session)


@router.post("/config")
def config_dict(session: dict = json.loads(default_session)):
    """
    Basic validation for session config in json format, to save place the
    `base64` string into the keyvault secret defined with `KEYVAULT_SESSION_SECRET`
    """
    return load_session(encode_session(session))


def load_session(data: str = None, config: dict = json.loads(default_session)):
    """
    Decode and return a session as a json object and the base64 string for easy editing
    """
    if data is None: # for internal python use only
        data = boot(os.environ["KEYVAULT_SESSION_SECRET"])
    try:
        config.update(json.loads(base64.b64decode(data)))
    except Exception as exc:
        logger.warning(exc)
        raise HTTPException(500, "Failed to load session data") from exc
    session = {"session": config, "base64": encode_session(config), "apis": {}}
    session["key"] = hashlib.sha256(session["base64"]).hexdigest()
    for item, data in config.items():
        if item.startswith("proxy_"):
            # Validate proxy parameters
            assert httpx_client(data)
            session["apis"][item.replace("proxy_", "", 1)] = data["base_url"]
    return session


@router.get("/apis")
def apis(request: Request) -> dict:
    # Returns configured origins
    return _session(request, key="apis")


def client(request: Request, prefix: str):
    # Returns a client cached up to an hour for sending requests to an origin
    if prefix not in apis(request):
        raise HTTPException(404, f"{prefix} does not have a valid configuration, see /proxy/apis for valid prefixes.")
    return httpx_client(_session(request)[f"proxy_{prefix}"])


def filter_headers(headers: dict, filtered_prefixes=["host", "cookie", "x-ms-", "x-arr-", "disguised-host", "referer"]): # pylint: disable=dangerous-default-value
    clean_headers = {}
    for key, value in headers.items():
        for prefix in filtered_prefixes:
            if key.lower().startswith(prefix):
                break
        else:
            clean_headers[key] = value
    return clean_headers


async def get_body(request: Request):
    # wrapper to allow sync access of body
    return await request.body()


@router.get("/{prefix}/{path:path}", response_class=Response)
def upstream(request: Request, prefix: str, path: str, body=Depends(get_body)):
    # Proxies a request to a defined upstream as defined in session
    headers = filter_headers(request.headers)
    url = httpx.URL(path=path, query=request.url.query.encode("utf-8"))
    upstream_client = client(request, prefix)
    with upstream_client.stream(request.method, url, content=body, headers=headers) as origin:
        if "location" in origin.headers:
            base_url = f"{upstream_client.base_url}/"
            if origin.headers["location"].startswith(base_url):
                redir_path = origin.headers["location"].replace(base_url, "", 1)
                origin.headers["location"] = request.scope.get("root_path") + f"/{prefix}/{redir_path}"
            elif origin.headers["location"].startswith("http"):
                raise HTTPException(403, f"Redirect to {origin.headers['location']} not allowed.")
        response = Response(status_code=origin.status_code)
        response.body = b"".join(origin.iter_raw())
        strip_output_headers = ["set-cookie", "transfer-encoding", "content-length", "server", "date", "connection"]
        headers = filter_headers(origin.headers, filtered_prefixes=strip_output_headers)
        response.init_headers(headers=headers)
        return response
