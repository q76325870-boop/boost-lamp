import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from .errors import Problem


async def resolve_public(host):
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(record[4][0] for record in records))


class SafeHTTP:
    def __init__(self, client, resolver=resolve_public):
        self.client = client
        self.resolver = resolver

    async def open(self, method, url, headers=None, image=False):
        for _ in range(4):
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            domains = ("okcdn.ru", "mycdn.me", "boosty.to") if image else ("okcdn.ru", "vkuser.net")
            if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                    or not any(host == d or host.endswith("." + d) for d in domains)):
                raise Problem("UNSAFE_UPSTREAM", 502)
            for attempt in range(3):
                try:
                    addresses = await asyncio.wait_for(self.resolver(host), timeout=2)
                    break
                except (socket.gaierror, TimeoutError):
                    if attempt == 2:
                        logging.warning("DNS resolution failed for allowed host: %s", host)
                        raise Problem("UPSTREAM_UNAVAILABLE", 502) from None
                    await asyncio.sleep(.2 * (attempt + 1))
            if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                raise Problem("UNSAFE_UPSTREAM", 502)
            address = sorted(addresses, key=lambda a: ":" in a)[0]
            request = self.client.build_request(method, httpx.URL(url).copy_with(host=address),
                headers={"Host": host, "Accept-Encoding": "identity", "User-Agent": "Mozilla/5.0", **(headers or {})},
                extensions={"sni_hostname": host})
            response = await self.client.send(request, stream=True)
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("location", "")
            await response.aclose()
            url = urljoin(url, location)
        raise Problem("UPSTREAM_REDIRECT_LOOP", 502)
