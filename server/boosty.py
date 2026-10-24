import asyncio
import json
import sqlite3
import time
from urllib.parse import quote

import httpx

from .auth import parse_session
from .errors import Problem
from .state import video_key

QUALITIES = ("ultra_hd", "quad_hd", "full_hd", "high", "medium", "low", "lowest", "tiny")
API = "https://api.boosty.to"


def normalize_post(post, author):
    if post.get("hasAccess") is not True:
        return None
    videos = []
    for item in post.get("data", []):
        if item.get("type") != "ok_video" or not item.get("complete"):
            continue
        available = {u.get("type") for u in item.get("playerUrls", []) if u.get("url")}
        qualities = [q for q in QUALITIES if q in available]
        if qualities:
            videos.append({"video_id": str(item["id"]), "key": video_key(author, str(post["id"]), str(item["id"])),
                           "title": str(item.get("title") or post.get("title") or "Видео")[:500],
                           "duration_seconds": item.get("duration"), "qualities": qualities})
    if not videos:
        return None
    return {"post_id": str(post["id"]), "author_id": author, "title": str(post.get("title") or "Видео")[:500],
            "published_at": post.get("createdAt"), "videos": videos}


class Boosty:
    def __init__(self, state, client):
        self.state = state
        self.client = client
        self.lock = asyncio.Lock()
        self.generation = 0

    def check_generation(self, generation):
        if generation != self.generation:
            raise Problem("DEVICE_REQUIRED", 401)

    async def raw(self, method, path, **kwargs):
        try:
            async with self.client.stream(method, API + path, **kwargs) as response:
                if response.status_code in (401, 403):
                    raise Problem("AUTH_REQUIRED" if response.status_code == 401 else "ACCESS_DENIED", response.status_code)
                if response.status_code == 429:
                    raise Problem("RATE_LIMITED", 429)
                if response.status_code == 404:
                    raise Problem("NOT_FOUND", 404)
                if response.status_code >= 300:
                    raise Problem("UPSTREAM_UNAVAILABLE", 502)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise Problem("UPSTREAM_RESPONSE_TOO_LARGE", 502)
                return json.loads(body)
        except (httpx.HTTPError, ValueError):
            raise Problem("UPSTREAM_UNAVAILABLE", 502) from None

    async def token(self, rejected=None, generation=None):
        generation = self.generation if generation is None else generation
        async with self.lock:
            self.check_generation(generation)
            session = self.state.read_session()
            if session["expiresAt"] > (time.time() + 60) * 1000 and session["accessToken"] != rejected:
                return session["accessToken"]
            result = await self.raw("POST", "/oauth/token/", data={"grant_type": "refresh_token", "device_os": "web",
                "device_id": session["clientId"], "refresh_token": session["refreshToken"]})
            try:
                updated = parse_session({"accessToken": result["access_token"], "refreshToken": result.get("refresh_token") or session["refreshToken"],
                    "clientId": session["clientId"], "expiresAt": int((time.time() + float(result["expires_in"])) * 1000)})
                self.state.write_session(updated)
            except (KeyError, ValueError, OSError, sqlite3.Error):
                raise Problem("SESSION_STORAGE_ERROR", 503) from None
            return updated["accessToken"]

    async def get(self, path, params=None, generation=None):
        generation = self.generation if generation is None else generation
        token = await self.token(generation=generation)
        for attempt in range(2):
            try:
                result = await self.raw("GET", path, params=params, headers={"Authorization": "Bearer " + token})
                self.check_generation(generation)
                return result
            except Problem as error:
                if error.code != "AUTH_REQUIRED" or attempt:
                    raise
                token = await self.token(rejected=token, generation=generation)

    async def import_session(self, payload):
        session = parse_session(payload)
        async with self.lock:
            profile = await self.raw("GET", "/v1/user/current", headers={"Authorization": "Bearer " + session["accessToken"]})
            if not profile.get("id"):
                raise Problem("INVALID_SESSION")
            changed = self.state.install_session(str(profile["id"]), session)
            self.generation += 1
            return changed

    async def post(self, author, post, generation=None):
        return await self.get("/v1/blog/" + quote(author, safe="") + "/post/" + quote(post, safe=""), generation=generation)
