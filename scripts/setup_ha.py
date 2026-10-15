"""Runs inside the Home Assistant pod (scripts/setup feeds it in), where
aiohttp is at hand and HA is on 127.0.0.1:8123. An administrator's
long-lived token comes on stdin, never in argv. One step per run:

    http-configure  stage the trusted proxies as a pending HTTP config (HA
                    restarts to try it; scripts/setup checks it, then promotes)
    http-promote    confirm the pending config, so it isn't reverted
    entities        rename the ecobee's climate entity to climate.thermostat
    users           create the Panel and Monitoring users (not administrators,
                    local only) with a long-lived token each, printed as JSON
                    on stdout for scripts/setup to put in place; skipped for a
                    user that already exists
"""
import asyncio
import json
import secrets
import sys

import aiohttp

BASE = "http://127.0.0.1:8123"
CLIENT_ID = "http://127.0.0.1:8123/"

# Traefik reaches this host-network pod from the nodes' addresses (it's
# masqueraded across nodes) or the pod network; brick9000's Caddy is the
# proxy before it, so the real client is the address before Caddy's.
TRUSTED_PROXIES = ["10.42.0.0/16", "192.168.1.170/32", "192.168.1.171/32",
                   "192.168.1.183/32", "192.168.1.221/32"]
THERMOSTAT = "climate.thermostat"
USERS = {"Panel": "lcars-panel", "Monitoring": "prometheus"}
TOKEN_DAYS = 3650


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class WS:
    def __init__(self, session, token):
        self.session, self.token, self.n = session, token, 0

    async def __aenter__(self):
        self.ws = await self.session.ws_connect(f"{BASE}/api/websocket")
        await self.ws.receive_json()  # auth_required
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        msg = await self.ws.receive_json()
        if msg["type"] != "auth_ok":
            raise SystemExit(f"auth failed: {msg.get('message', msg['type'])}")
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def call(self, type_, **data):
        self.n += 1
        await self.ws.send_json({"id": self.n, "type": type_, **data})
        while True:
            msg = await self.ws.receive_json()
            if msg.get("id") == self.n and msg["type"] == "result":
                if not msg["success"]:
                    raise SystemExit(f"{type_}: {msg['error']['message']}")
                return msg["result"]


async def http_configure(ws):
    current = await ws.call("http/config")
    conf = {k: v for k, v in current["stable"].items()
            if k not in ("created_at", "error", "error_message")}
    conf.update(use_x_forwarded_for=True, trusted_proxies=TRUSTED_PROXIES)
    result = await ws.call("http/config/configure", config=conf)
    log("http: pending config staged;", "restarting" if result["restart"] else "unchanged")
    print(json.dumps(result))


async def http_promote(ws):
    current = await ws.call("http/config")
    if current["pending"] is None:
        log("http: nothing pending; the stable config is",
            current["stable"].get("trusted_proxies"))
        return
    await ws.call("http/config/promote")
    log("http: pending config promoted to stable")


async def entities(ws):
    registry = await ws.call("config/entity_registry/list")
    if any(e["entity_id"] == THERMOSTAT for e in registry):
        log(f"entities: {THERMOSTAT} already exists")
        return
    climates = [e for e in registry if e["entity_id"].startswith("climate.")
                and e["platform"] == "homekit_controller"]
    if len(climates) != 1:
        raise SystemExit(f"entities: expected one HomeKit thermostat, found "
                         f"{[e['entity_id'] for e in climates]}; pair the ecobee first")
    old = climates[0]["entity_id"]
    await ws.call("config/entity_registry/update", entity_id=old, new_entity_id=THERMOSTAT)
    log(f"entities: {old} -> {THERMOSTAT}")


async def user_token(session, username, password, client_name):
    """Sign in as the new user and make its long-lived token."""
    async with session.post(f"{BASE}/auth/login_flow", json={
            "client_id": CLIENT_ID, "handler": ["homeassistant", None],
            "redirect_uri": CLIENT_ID}) as r:
        flow = await r.json()
    async with session.post(f"{BASE}/auth/login_flow/{flow['flow_id']}", json={
            "client_id": CLIENT_ID, "username": username, "password": password}) as r:
        step = await r.json()
    if step.get("type") != "create_entry":
        raise SystemExit(f"users: sign-in as {username} failed: {step.get('errors')}")
    async with session.post(f"{BASE}/auth/token", data={
            "grant_type": "authorization_code", "code": step["result"],
            "client_id": CLIENT_ID}) as r:
        access = (await r.json())["access_token"]
    async with WS(session, access) as user_ws:
        return await user_ws.call("auth/long_lived_access_token",
                                  client_name=client_name, lifespan=TOKEN_DAYS)


async def users(session, ws):
    existing = {u["name"] for u in await ws.call("config/auth/list")}
    tokens = {}
    for name, client_name in USERS.items():
        if name in existing:
            log(f"users: {name} exists; its token is left as it is")
            continue
        user = (await ws.call("config/auth/create", name=name,
                              group_ids=["system-users"], local_only=True))["user"]
        # The password is never kept: the token is all the user is for.
        password = secrets.token_urlsafe(32)
        await ws.call("config/auth_provider/homeassistant/create",
                      user_id=user["id"], username=name.lower(), password=password)
        tokens[name] = await user_token(session, name.lower(), password, client_name)
        log(f"users: {name} created, with a {TOKEN_DAYS}-day token")
    print(json.dumps(tokens))


async def main(step):
    token = sys.stdin.readline().strip()
    if not token:
        raise SystemExit("no administrator token on stdin")
    async with aiohttp.ClientSession() as session, WS(session, token) as ws:
        if step == "http-configure":
            await http_configure(ws)
        elif step == "http-promote":
            await http_promote(ws)
        elif step == "entities":
            await entities(ws)
        elif step == "users":
            await users(session, ws)
        else:
            raise SystemExit(f"unknown step {step!r}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
