import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import quote, urlsplit

import httpx

from .boosty import Boosty, normalize_post
from .errors import Problem
from .network import SafeHTTP, resolve_public
from .state import State


@dataclass
class Settings:
    state_dir: Path
    public_url: str
    origins: tuple = ()
    networks: tuple = ("127.0.0.0/8",)

    @classmethod
    def from_env(cls):
        url = os.environ["BOOSTY_PUBLIC_URL"].rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path or parsed.query or parsed.username:
            raise ValueError("BOOSTY_PUBLIC_URL must be an origin")
        return cls(Path(os.environ.get("BOOSTY_STATE_DIR", "/state")), url,
                   tuple(x.strip() for x in os.environ.get("BOOSTY_ORIGINS", "").split(",") if x.strip()),
                   tuple(x.strip() for x in os.environ.get("BOOSTY_NETWORKS", "127.0.0.0/8").split(",") if x.strip()))


class Service:
    def __init__(self, settings, api_transport=None, media_transport=None, resolver=resolve_public):
        self.settings = settings
        self.state = State(settings.state_dir)
        self.api_client = httpx.AsyncClient(transport=api_transport, timeout=httpx.Timeout(15, connect=5),
            follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=4))
        # Each pinned IP gets a fresh TLS handshake for its original hostname.
        self.media_client = httpx.AsyncClient(transport=media_transport, timeout=httpx.Timeout(30, connect=5),
            follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=8, max_keepalive_connections=0))
        self.boosty = Boosty(self.state, self.api_client)
        self.safe = SafeHTTP(self.media_client, resolver)
        self.pending = {}
        self.playbacks = {}
        self.opened = set()
        self.images = OrderedDict()
        self.cache = OrderedDict()
        self.catalog_lock = asyncio.Lock()
        self.stream_slots = asyncio.Semaphore(4)

    async def close(self):
        await self.api_client.aclose()
        await self.media_client.aclose()
        self.state.close()

    def reset(self):
        self.pending.clear()
        self.playbacks.clear()
        self.opened.clear()
        self.images.clear()
        self.cache.clear()

    def pair(self, address):
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v["expires"] > now}
        if len(self.pending) >= 10 or sum(v["address"] == address for v in self.pending.values()) >= 3:
            raise Problem("RATE_LIMITED", 429)
        identifier, secret = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        used = {v["code"] for v in self.pending.values()}
        code = str(secrets.randbelow(1000000)).zfill(6)
        while code in used:
            code = str(secrets.randbelow(1000000)).zfill(6)
        self.pending[identifier] = {"hash": hashlib.sha256(secret.encode()).hexdigest(), "code": code,
                                    "address": address, "expires": now + 300, "token": None}
        return {"id": identifier, "secret": secret, "code": code, "expires_in": 300}

    def approve(self, code):
        matches = [v for v in self.pending.values() if v["code"] == code and v["expires"] > time.time()]
        if not matches or matches[0]["token"]:
            raise Problem("PAIRING_EXPIRED", 404)
        _, token = self.state.new_device()
        self.playbacks.clear()
        self.opened.clear()
        matches[0]["token"] = token

    def claim(self, identifier, secret):
        item = self.pending.get(identifier)
        if not item or item["expires"] < time.time():
            self.pending.pop(identifier, None)
            raise Problem("PAIRING_EXPIRED", 404)
        if not hmac.compare_digest(item["hash"], hashlib.sha256(secret.encode()).hexdigest()):
            raise Problem("FORBIDDEN", 403)
        if not item["token"]:
            return {"pending": True}
        self.pending.pop(identifier)
        return {"token": item["token"]}

    def cursor(self, scope, offset=None, encoded=None):
        key = self.state.admin_key.encode()
        if encoded:
            try:
                body, signature = encoded.split(".")
                if not hmac.compare_digest(signature, hmac.new(key, body.encode(), "sha256").hexdigest()):
                    raise ValueError()
                decoded = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
                if decoded[0] != scope or not isinstance(decoded[1], (str, int)):
                    raise ValueError()
                return decoded[1]
            except (ValueError, TypeError, IndexError):
                raise Problem("INVALID_CURSOR") from None
        if offset is None:
            return None
        body = base64.urlsafe_b64encode(json.dumps([scope, offset], separators=(",", ":")).encode()).decode().rstrip("=")
        return body + "." + hmac.new(key, body.encode(), "sha256").hexdigest()

    def image_url(self, source):
        if not isinstance(source, str) or not source.startswith("https://"):
            return None
        identifier = secrets.token_urlsafe(24)
        self.images[identifier] = (source, time.time() + 21600)
        while len(self.images) > 512:
            self.images.popitem(last=False)
        return self.settings.public_url + "/boosty/images/" + identifier

    def decorate(self, post, author):
        result = normalize_post(post, author)
        if result:
            for output in result["videos"]:
                raw = next(v for v in post["data"] if str(v.get("id")) == output["video_id"])
                output["image_url"] = self.image_url(raw.get("preview") or raw.get("defaultPreview"))
            result["image_url"] = result["videos"][0]["image_url"]
        return result

    def check_device(self, current):
        self.boosty.check_generation(current[1])
        if not self.state.device_exists(current[0]):
            raise Problem("DEVICE_REQUIRED", 401)

    async def catalog(self, current, kind, author=None, cursor=None):
        scope = kind + ":" + (author or "")
        offset = self.cursor(scope, encoded=cursor) if cursor else None
        key = (scope, cursor)
        async with self.catalog_lock:
            self.check_device(current)
            saved = self.cache.get(key)
            if saved and saved[0] > time.time():
                return saved[1]
            params = {"limit": 25}
            if offset is not None:
                params["offset"] = offset
            if kind == "authors":
                params["with_follow"] = "true"
                result = await self.boosty.get("/v1/user/subscriptions", params, generation=current[1])
                self.check_device(current)
                items = []
                for row in result.get("data", []):
                    blog = row.get("blog", {})
                    if blog.get("blogUrl"):
                        items.append({"author_id": blog["blogUrl"], "title": str(blog.get("title") or blog["blogUrl"])[:500],
                                      "image_url": self.image_url(blog.get("avatarUrl"))})
                consumed = int(offset or 0) + len(result.get("data", []))
                next_offset = consumed if result.get("data") and consumed < result.get("total", 0) else None
            else:
                params.update({"is_only_allowed": "true", "comments_limit": 0, "reply_limit": 0})
                path = "/v1/blog/" + quote(author, safe="") + "/post/" if kind == "posts" else "/v1/feed/post/"
                result = await self.boosty.get(path, params, generation=current[1])
                self.check_device(current)
                items = []
                posts = result.get("data", [])
                if kind == "feed":
                    posts = posts.get("posts", [])
                for post in posts:
                    slug = author or post.get("user", {}).get("blogUrl") or post.get("blog", {}).get("blogUrl")
                    if slug:
                        item = self.decorate(post, slug)
                        if item and not any(p["post_id"] == item["post_id"] for p in items):
                            items.append(item)
                extra = result.get("extra", {})
                next_offset = extra.get("offset") if not extra.get("isLast") else None
                if next_offset == offset:
                    next_offset = None
            value = {"items": items, "next_cursor": self.cursor(scope, next_offset)}
            self.cache[key] = (time.time() + 300, value)
            while len(self.cache) > 128:
                self.cache.popitem(last=False)
            return value

    async def source(self, author, post_id, video_id, quality, generation=None):
        raw = await self.boosty.post(author, post_id, generation=generation)
        if raw.get("hasAccess") is not True:
            raise Problem("ACCESS_DENIED", 403)
        normalized = self.decorate(raw, author)
        if not normalized:
            raise Problem("UNSUPPORTED_MEDIA", 422)
        video = next((v for v in normalized["videos"] if v["video_id"] == video_id), None)
        if not video or quality not in video["qualities"]:
            raise Problem("UNSUPPORTED_MEDIA", 422)
        original = next(v for v in raw["data"] if str(v.get("id")) == video_id)
        source = next(u["url"] for u in original["playerUrls"] if u.get("type") == quality and u.get("url"))
        return source, {**video, "author_id": author, "post_id": post_id}

    async def play(self, current, author, post_id, video_id, quality):
        self.check_device(current)
        source, video = await self.source(author, post_id, video_id, quality, generation=current[1])
        self.check_device(current)
        device = current[0]
        now = time.time()
        self.playbacks.clear()
        identifier = secrets.token_urlsafe(32)
        self.playbacks[identifier] = {"device": device, "source": source, "video": video, "quality": quality,
            "created": now, "used": now, "identity": None, "lock": asyncio.Lock(), "generation": current[1]}
        self.opened.add((device, video["key"]))
        self.state.remember_video(video["key"], video)
        return {"playback_id": identifier, "url": self.settings.public_url + "/boosty/media/" + identifier + "/video.mp4",
                "key": video["key"], "mime_type": "video/mp4", "expires_at": now + 43200,
                "title": video["title"], **self.state.progress(video["key"])}

    def media_session(self, identifier):
        value = self.playbacks.get(identifier)
        now = time.time()
        if (not value or now - value["used"] > 3600 or now - value["created"] > 43200
                or value["generation"] != self.boosty.generation
                or not self.state.device_exists(value["device"])):
            self.playbacks.pop(identifier, None)
            raise Problem("PLAYBACK_EXPIRED", 404)
        value["used"] = now
        return value
