"""GPUStack 2.3.1 HTTP client; all state is per logical Worker."""

import asyncio
import json
from collections.abc import AsyncIterator

import aiohttp

from workersim.config import ServerConfig


class APIError(Exception):
    def __init__(self, status: int, method: str, path: str):
        self.status = status
        # Response bodies can contain echoed configuration or credentials.
        super().__init__(f"GPUStack {method} {path.split('?')[0]} returned HTTP {status}")


class Client:
    def __init__(self, config: ServerConfig, token: str):
        self.config = config
        self.token = token
        self.session: aiohttp.ClientSession | None = None

    async def open(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.token}"} if self.token else {},
            timeout=aiohttp.ClientTimeout(
                total=None, connect=self.config.request_timeout,
                sock_read=self.config.request_timeout,
            ),
            connector=aiohttp.TCPConnector(
                ssl=self.config.verify_ssl, limit=100, keepalive_timeout=5
            ),
            # Credentials and cookies must never cross logical Worker identities.
            cookie_jar=aiohttp.DummyCookieJar(),
            trust_env=False,
        )
        return self

    async def close(self):
        if self.session:
            await self.session.close()

    async def request(
        self, method: str, path: str, *, body=None, params=None, root=False,
        timeout=None,  # noqa: ASYNC109 - per-request HTTP timeout, matching the native client
    ):
        assert self.session is not None
        url = self.config.url.rstrip("/") + ("" if root else "/v2") + path
        options = {} if timeout is None else {"timeout": aiohttp.ClientTimeout(total=timeout)}
        async with self.session.request(
            method, url, json=body, params=params, **options
        ) as response:
            if response.status >= 400:
                raise APIError(response.status, method, path)
            if response.status == 204:
                return None
            return await response.json()

    async def list_all(self, path: str, **params) -> list[dict]:
        page, result = 1, []
        while True:
            data = await self.request("GET", path, params={**params, "page": page, "perPage": 100})
            items = data.get("items", [])
            result.extend(items)
            pagination = data.get("pagination", {})
            if not items or page >= pagination.get("totalPage", 1):
                return result
            page += 1

    async def watch(
        self, on_connect=None, *, path="/model-instances", **params
    ) -> AsyncIterator[dict]:
        assert self.session is not None
        url = self.config.url.rstrip("/") + "/v2" + path
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=45)
        async with self.session.get(
            url, params={"watch": "true", **params}, timeout=timeout
        ) as response:
            if response.status >= 400:
                raise APIError(response.status, "GET", path + "?watch=true")
            if on_connect is not None:
                on_connect()
            # The native client decodes NDJSON despite Content-Type: text/event-stream.
            while True:
                line = await response.content.readline()
                if not line:
                    raise ConnectionError("Model instance watch closed")
                line = line.strip()
                if not line or line.startswith(b":"):
                    continue
                if line.startswith(b"data:"):
                    line = line[5:].strip()
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Invalid model instance event")
                yield event


async def cancel_tasks(tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
