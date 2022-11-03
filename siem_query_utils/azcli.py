"""
Azure CLI helpers and core functions
"""
# pylint: disable=logging-fstring-interpolation, unspecified-encoding, line-too-long

import base64
import hashlib
import importlib
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from string import Template
from typing import Any

import httpx_cache
from azure.cli.core import get_default_cli
from azure.storage.blob import BlobServiceClient
from cacheout import Cache
from cloudpathlib import AzureBlobClient
from dotenv import load_dotenv
from fastapi import HTTPException
from pathvalidate import sanitize_filepath
from uvicorn.config import Config

load_dotenv()


# Steal uvicorns logger config
logger = logging.getLogger("uvicorn.error")
Config(f"{__package__}:app").configure_logging()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

cache = Cache(maxsize=25600, ttl=300)


app_state = {
    "logged_in": False,
    "login_time": datetime.utcnow() - timedelta(days=1),
}  # last login 1 day ago to force relogin


default_session = json.dumps(
    {
        "proxy_httpbin": {
            "base_url": "https://httpbin.org",
            # "params": {"get": "params"},
            # "headers": {"http": "headers"},
            # "cookies": {"cookie": "jar"},
        },
        "proxy_jupyter": {"base_url": "https://wagov.github.io/wasoc-jupyterlite"},
        "main_path": "/jupyter/lab/index.html",  # this is redirected to when index is loaded
    }
)


@cache.memoize(ttl=60 * 60)
def httpx_client(proxy: dict) -> httpx_cache.Client:
    """
    Create a httpx client with caching

    Args:
        proxy (dict): proxy config

    Returns:
        httpx_cache.Client: httpx client
    """
    # cache client objects for an hour
    proxy_client = httpx_cache.Client(**proxy, timeout=None)
    proxy_client.headers["host"] = proxy_client.base_url.host
    return proxy_client


@cache.memoize(ttl=60 * 60)
def boot(secret: str) -> str:
    """
    Connect to keyvault and get the session data

    Args:
        secret (str): keyvault secret URL

    Raises:
        HTTPException: 403 if secret is not found

    Returns:
        str: session data as a base64 encoded string
    """
    # cache session creds for an hour
    secret = azcli(["keyvault", "secret", "show", "--id", secret])
    if "error" in secret:
        logger.warning(secret["error"])
        raise HTTPException(403, "KEYVAULT_SESSION_SECRET not available")
    return secret["value"]


def encode_session(session: dict) -> str:
    """
    Encode a session as a base64 string

    Args:
        session (dict): session data

    Returns:
        str: base64 encoded string
    """
    return base64.b64encode(json.dumps(session, sort_keys=True).encode("utf8"))


def load_session(data: str = None, config: dict = json.loads(default_session)):
    """
    Decode and return a session as a json object and the base64 string for easy editing
    """
    if data is None:  # for internal python use only
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


def clean_path(path: str) -> str:
    """
    Remove any disallowed characters from a path

    Args:
        path (str): path to sanitize

    Returns:
        str: sanitized path
    """
    return sanitize_filepath(path.replace("..", ""), platform="auto")


def bootstrap(_app_state: dict):
    """
    Load app state from env vars or dotenv

    Args:
        _app_state (dict): app state

    Raises:
        Exception: if essential env vars are not set
    """
    try:
        prefix, subscription = (
            os.environ["DATALAKE_BLOB_PREFIX"],
            os.environ["DATALAKE_SUBSCRIPTION"],
        )
    except Exception as exc:
        raise Exception(
            "Please set DATALAKE_BLOB_PREFIX and DATALAKE_SUBSCRIPTION env vars"
        ) from exc
    account, container = prefix.split("/")[2:]
    _app_state.update(
        {
            "datalake_blob_prefix": prefix,  # e.g. "https://{datalake_account}.blob.core.windows.net/{datalake_container}"
            "datalake_subscription": subscription,
            "datalake_account": account,
            "datalake_container": container,
            "email_template": Template(
                importlib.resources.read_text(f"{__package__}.templates", "email-template.html")
            ),
            "datalake_path": get_blob_path(prefix, subscription),
            "email_footer": os.environ.get(
                "FOOTER_HTML", "Set FOOTER_HTML env var to configure this..."
            ),
            "max_threads": int(os.environ.get("MAX_THREADS", "20")),
            "data_collector_connstring": os.environ.get(
                "AZMONITOR_DATA_COLLECTOR"
            ),  # kinda optional
            "keyvault_session": load_session(boot(os.environ["KEYVAULT_SESSION_SECRET"]))
            if "KEYVAULT_SESSION_SECRET" in os.environ
            else None,
            "sessions": {},
        }
    )


def login(refresh: bool = False):
    """
    login to azure cli and setup app state

    Args:
        refresh (bool, optional): force relogin. Defaults to False.
    """
    cli = get_default_cli()
    if os.environ.get("IDENTITY_HEADER"):
        if refresh:
            cli.invoke(
                ["logout", "--only-show-errors", "-o", "json"], out_file=open(os.devnull, "w")
            )
        # Use managed service identity to login
        loginstatus = cli.invoke(
            ["login", "--identity", "--only-show-errors", "-o", "json"],
            out_file=open(os.devnull, "w"),
        )
        if cli.result.error:
            # bail as we aren't able to login
            logger.error(cli.result.error)
            exit(loginstatus)
        app_state["logged_in"] = True
        app_state["login_time"] = datetime.utcnow()
    else:
        loginstatus = cli.invoke(["account", "show", "-o", "json"], out_file=open(os.devnull, "w"))
        try:
            assert "environmentName" in cli.result.result
            app_state["logged_in"] = True
            app_state["login_time"] = datetime.utcnow()
        except AssertionError as exc:
            # bail as we aren't able to login
            logger.error(exc)
            exit()
    # setup all other env vars
    bootstrap(app_state)


def settings(key: str):
    """
    Get a setting from the app state

    Args:
        key (str): setting key

    Returns:
        setting value
    """
    if datetime.utcnow() - app_state["login_time"] > timedelta(hours=1):
        login(refresh=True)
    elif not app_state["logged_in"]:
        login()
    return app_state[key]


@cache.memoize(ttl=60)
def azcli(cmd: list, error_result: Any = None):
    "Run a general azure cli cmd, if as_df True return as dataframe"
    assert settings("logged_in")
    cmd += ["--only-show-errors", "-o", "json"]
    cli = get_default_cli()
    logger.debug(" ".join(["az"] + cmd).replace("\n", " ").strip()[:160])
    cli.invoke(cmd, out_file=open(os.devnull, "w"))
    if cli.result.error:
        logger.warning(cli.result.error)
        if error_result is None:
            raise cli.result.error
        else:
            return error_result
    return cli.result.result


@cache.memoize(ttl=60 * 60 * 24)  # cache sas tokens 1 day
def generatesas(
    account: str = None,
    container: str = None,
    subscription: str = None,
    permissions="racwdlt",
    expiry_days=3,
) -> str:
    """
    Generate a SAS token for a storage account

    Args:
        account (str, optional): storage account name. Defaults to None.
        container (str, optional): container name. Defaults to None.
        subscription (str, optional): subscription id. Defaults to None.
        permissions (str, optional): SAS permissions. Defaults to "racwdlt".
        expiry_days (int, optional): SAS expiry in days. Defaults to 3.

    Returns:
        str: SAS token
    """
    expiry = str(datetime.today().date() + timedelta(days=expiry_days))
    result = azcli(
        [
            "storage",
            "container",
            "generate-sas",
            "--auth-mode",
            "login",
            "--as-user",
            "--account-name",
            account or settings("datalake_account"),
            "-n",
            container or settings("datalake_container"),
            "--subscription",
            subscription or settings("datalake_subscription"),
            "--permissions",
            permissions,
            "--expiry",
            expiry,
        ]
    )
    logger.debug(result)
    return result


def get_blob_path(url: str, subscription: str = ""):
    """
    Mounts a blob url using azure cli
    If called with no subscription, just returns a pathlib.Path pointing to url (for testing)
    """
    if subscription == "":
        return Path(clean_path(url))
    account, container = url.split("/")[2:]
    account = account.split(".")[0]
    sas = generatesas(account, container, subscription)
    blobclient = AzureBlobClient(
        blob_service_client=BlobServiceClient(
            account_url=url.replace(f"/{container}", ""), credential=sas
        )
    )
    return blobclient.CloudPath(f"az://{container}")
