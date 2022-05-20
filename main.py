#!/usr/bin/env python3
import json
import os
import sys
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache, wraps
from pathlib import Path
from subprocess import check_output
from typing import Union

from fastapi import FastAPI, HTTPException

secret_api_token = os.environ["API_TOKEN"]
app = FastAPI()

def cache(maxsize=2000, typed=False, ttl=300):
    """Least-recently used cache with time-to-live (ttl) limit."""

    class Result:
        __slots__ = ('value', 'death')

        def __init__(self, value, death):
            self.value = value
            self.death = death

    def decorator(func):
        @lru_cache(maxsize=maxsize, typed=typed)
        def cached_func(*args, **kwargs):
            value = func(*args, **kwargs)
            death = time.monotonic() + ttl
            return Result(value, death)

        @wraps(func)
        def wrapper(*args, **kwargs):
            result = cached_func(*args, **kwargs)
            if result.death < time.monotonic():
                result.value = func(*args, **kwargs)
                result.death = time.monotonic() + ttl
            return result.value

        wrapper.cache_clear = cached_func.cache_clear
        return wrapper

    return decorator

@cache()
def azcli(*cmd):
    # Run a general azure cli cmd
    result = check_output(["az"] + list(cmd) + ["--only-show-errors", "-o", "json"])
    if not result:
        return None
    return json.loads(result)

def analyticsQuery(workspaces, query, timespan="P7D"):
    # workspaces must be a list of customerIds
    chunkSize = 30
    chunks = [sorted(workspaces)[x:x+chunkSize] for x in range(0, len(workspaces), chunkSize)]
    results, output = [], []
    with ThreadPoolExecutor() as executor:
        for chunk in chunks:
            cmd = ["monitor", "log-analytics", "query", "--workspace", chunk[0], "--analytics-query", query, "--timespan", timespan]
            if len(chunk) > 1:
                cmd += ["--workspaces"] + chunk[1:]
            results.append(executor.submit(azcli, *cmd))
    for future in results:
        try:
            output += future.result()
        except Exception as e:
            print(e)
    return output

Workspace = namedtuple("Workspace", ["subscription", "customerId", "resourceGroup", "name"])

@cache(ttl=24*60*60) # cache workspaces for 1 day
def listWorkspaces():
    # Get all workspaces as a list of named tuples
    with ThreadPoolExecutor() as executor:
        subscriptions = azcli("account", "list", "--query", "[].id")
        wsquery = ["monitor", "log-analytics", "workspace", "list", "--query", "[].[customerId,resourceGroup,name]"]
        subscriptions = [(s, executor.submit(azcli, *list(wsquery + ["--subscription", s]))) for s in subscriptions]
    workspaces = set()
    for subscription, future in subscriptions:
        for customerId, resourceGroup, name in future.result():
            workspaces.add(Workspace(subscription, customerId, resourceGroup, name))
    # cross check workspaces to make sure they have SecurityIncident tables
    validated = analyticsQuery([ws.customerId for ws in workspaces], "SecurityIncident | distinct TenantId")
    validated = [item["TenantId"] for item in validated]
    return [ws for ws in workspaces if ws.customerId in validated]

def simpleQuery(query, name):
    # Find first workspace matching name, then run a kusto query against it
    for workspace in listWorkspaces():
        if str(workspace).find(name):
            return analyticsQuery([workspace.customerId], query)

def globalQuery(query):
    return analyticsQuery([ws.customerId for ws in listWorkspaces()], query)

actions = {
    "listWorkspaces": listWorkspaces,
    "globalQuery": globalQuery,
    "simpleQuery": simpleQuery
}

# Check and login to azure with --identity if not authed yet
azcli("config", "set", "extension.use_dynamic_install=yes_without_prompt")
try:
    azcli("account", "show")
except Exception as e:
    azcli("login", "--identity")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        actionName = sys.argv[1]
        args = sys.argv[2:]
        print(actions[actionName](*args))
    else:
        print(f"Run an action from {actions.keys()}")
        print(f"Example: {sys.argv[0]} {list(actions.keys())[0]}")


@app.get("/{actionName}")
def get_action(actionName: str, auth_token: str, args: Union[str, None] = None):
    if secret_api_token != auth_token:
        raise HTTPException(status_code=403, detail="Invalid auth_token")
    if args:
        args = json.loads(args)
    else:
        args = []
    return actions[actionName](*args)