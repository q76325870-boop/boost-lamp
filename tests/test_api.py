import asyncio
import time
from urllib.parse import urlsplit

import httpx
import pytest

from server.app import make_apps
from server.service import Service, Settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def fixture(tmp_path):
    controls = {"access": True, "closed": 0, "media_calls": [], "post_calls": 0, "refresh_media": False}
    source = bytes(range(256)) * 16

    class Stream(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data

        async def __aiter__(self):
            for index in range(0, len(self.data), 64):
                yield self.data[index:index + 64]

        async def aclose(self):
            import anyio
            await anyio.sleep(0)
            controls["closed"] += 1

    async def api(request):
        if request.url.path == "/v1/user/current":
            return httpx.Response(200, json={"id": "owner-b" if request.headers.get("authorization") == "Bearer owner-b-access" else "owner"})
        if controls.get("delay") and request.headers.get("authorization") == "Bearer access-secret":
            controls["waiting"] += 1
            if controls["waiting"] == 2:
                controls["arrived"].set()
            await controls["release"].wait()
        if request.url.path == "/v1/user/subscriptions":
            blogs = controls.get("blogs", [{"blogUrl": "author", "title": "Author"}])
            return httpx.Response(200, json={"data": [{"blog": b} for b in blogs], "total": len(blogs)})
        post = {"id": "post-1", "hasAccess": controls["access"], "title": "Film", "createdAt": 123,
                "user": {"blogUrl": "author"}, "data": [{"id": "video-1", "type": "ok_video", "complete": True,
                "title": "Film", "duration": 100, "playerUrls": [{"type": "full_hd", "url": "https://vd1.okcdn.ru/film?signature=private"}]}]}
        if request.url.path.endswith("/post-1"):
            controls["post_calls"] += 1
            return httpx.Response(200, json=post)
        if request.url.path == "/v1/feed/post/":
            return httpx.Response(200, json={"data": {"posts": [post], "blogs": [], "counters": {}}, "extra": {"isLast": True}})
        return httpx.Response(200, json={"data": [post], "extra": {"isLast": True}})

    def media(request):
        controls["media_calls"].append(request)
        headers = {"content-type": "video/mp4", "etag": '"same-file"', "accept-ranges": "bytes"}
        if controls.get("changed"):
            headers["etag"] = '"different-file"'
        if controls["refresh_media"] and len(controls["media_calls"]) == 2:
            return httpx.Response(403, stream=Stream(b""))
        if "range" in request.headers:
            start, end = request.headers["range"].split("=")[1].split("-")
            start, end = int(start), int(end) if end else len(source) - 1
            if start >= len(source):
                return httpx.Response(416, headers={"content-range": "bytes */4096"}, stream=Stream(b""))
            headers.update({"content-range": f"bytes {start}-{end}/{len(source)}", "content-length": str(end - start + 1)})
            if controls.get("wrong_range"):
                headers["content-range"] = "bytes 0-99/4096"
            return httpx.Response(206, headers=headers, stream=Stream(source[start:end + 1]))
        headers["content-length"] = str(len(source))
        return httpx.Response(200, headers=headers, stream=Stream(b"" if request.method == "HEAD" else source))

    async def resolve(host):
        return ["93.184.216.34"]

    svc = Service(Settings(tmp_path, "http://media.test:19118", ("http://lampa.test",), ("127.0.0.0/8",)),
                  api_transport=httpx.MockTransport(api), media_transport=httpx.MockTransport(media), resolver=resolve)
    svc.state.write_session({"accessToken": "access-secret", "refreshToken": "refresh-secret", "clientId": "client", "expiresAt": int((time.time() + 86400) * 1000)})
    svc.state.install_session("owner", svc.state.read_session())
    device, token = svc.state.new_device()
    public, admin = make_apps(svc)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://media.test:19118", headers={"Authorization": "Bearer " + token}) as client:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin), base_url="http://127.0.0.1:19117", headers={"Authorization": "Bearer " + svc.state.admin_key}) as owner:
            yield svc, client, owner, controls, device
    await svc.close()


async def playback(client):
    response = await client.post("/boosty/v1/playbacks", json={"author_id": "author", "post_id": "post-1", "video_id": "video-1", "quality": "full_hd"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.anyio
async def test_public_admin_isolation_pairing_and_one_time_claim(fixture):
    svc, client, admin, controls, device = fixture
    assert (await client.get("/admin/status")).status_code == 404
    assert (await admin.get("/admin/status", headers={"Authorization": "Bearer invalid"})).status_code == 401
    assert (await admin.get("/admin/status", headers={"Origin": "https://evil.test"})).status_code == 403
    request = (await client.post("/boosty/v1/pairing", json={})).json()
    assert "token" not in request
    path = "/boosty/v1/pairing/" + request["id"]
    assert (await client.get(path)).status_code == 403
    assert (await admin.post("/admin/pairing/approve", json={"code": request["code"]})).status_code == 200
    claim = await client.get(path, headers={"X-Pair-Secret": request["secret"]})
    assert claim.status_code == 200 and len(claim.json()["token"]) >= 40
    assert (await client.get(path, headers={"X-Pair-Secret": request["secret"]})).status_code == 404


@pytest.mark.anyio
async def test_access_checked_at_play_and_no_secrets_returned(fixture):
    svc, client, admin, controls, device = fixture
    catalog = await client.get("/boosty/v1/posts?author=author")
    assert catalog.status_code == 200
    assert "signature" not in catalog.text and "access-secret" not in catalog.text
    controls["access"] = False
    response = await client.post("/boosty/v1/playbacks", json={"author_id": "author", "post_id": "post-1", "video_id": "video-1", "quality": "full_hd"})
    assert response.status_code == 403


@pytest.mark.anyio
async def test_author_identity_and_images_use_owner_fields(fixture):
    svc, client, admin, controls, device = fixture
    controls["blogs"] = [
        {"blogUrl": "creator", "title": "Video essays", "owner": {"name": "Creator name", "avatarUrl": "https://images.boosty.to/avatar.jpg"}, "coverUrl": "https://images.boosty.to/cover.jpg"},
        {"blogUrl": "legacy", "title": "Legacy", "owner": None},
        {"blogUrl": "slug-only"},
    ]
    items = (await client.get("/boosty/v1/authors")).json()["items"]
    assert items[0]["title"] == "Creator name"
    assert items[0]["description"] == "Video essays"
    for field, source in [("image_url", "avatar.jpg"), ("cover_url", "cover.jpg")]:
        identifier = items[0][field].rsplit("/", 1)[1]
        assert svc.images[identifier][0] == "https://images.boosty.to/" + source
    assert items[1]["title"] == "Legacy" and items[1]["image_url"] is None
    assert items[2]["title"] == "slug-only"


@pytest.mark.anyio
async def test_multiple_devices_keep_tokens_streams_and_shared_history(fixture):
    from server.state import State
    svc, first, admin, controls, device = fixture
    first_play = await playback(first)
    pairing = (await first.post("/boosty/v1/pairing", json={})).json()
    assert (await admin.post("/admin/pairing/approve", json={"code": pairing["code"]})).status_code == 200
    second_token = (await first.get("/boosty/v1/pairing/" + pairing["id"], headers={"X-Pair-Secret": pairing["secret"]})).json()["token"]
    assert (await first.get("/boosty/v1/authors")).status_code == 200
    async with httpx.AsyncClient(transport=first._transport, base_url=first.base_url, headers={"Authorization": "Bearer " + second_token}) as second:
        second_play = await playback(second)
        for player, item in [(first, first_play), (second, second_play)]:
            response = await player.get(item["url"], headers={"Range": "bytes=100-199"})
            assert response.status_code == 206 and len(response.content) == 100
        assert (await second.delete("/boosty/v1/playbacks/" + first_play["playback_id"])).status_code == 200
        assert (await first.head(first_play["url"])).status_code == 200
        await playback(second)
        assert (await second.head(second_play["url"])).status_code == 404
        assert (await first.head(first_play["url"])).status_code == 200
        progress = "/boosty/v1/progress/" + first_play["key"]
        assert (await first.put(progress, json={"position_seconds": 42, "duration_seconds": 100})).status_code == 200
        assert (await second.get("/boosty/v1/continue")).json()["items"][0]["position_seconds"] == 42
        reopened = State(svc.settings.state_dir)
        try:
            assert reopened.device(first.headers["authorization"][7:]) == device
            assert reopened.device(second_token) != device
        finally:
            reopened.close()
        replacement = {"accessToken": "owner-b-access", "refreshToken": "refresh-b", "clientId": "client", "expiresAt": int((time.time() + 86400) * 1000)}
        assert (await admin.post("/admin/session", json=replacement)).status_code == 200
        assert (await first.get("/boosty/v1/authors")).status_code == 401
        assert (await second.get("/boosty/v1/authors")).status_code == 401


@pytest.mark.anyio
async def test_feed_unwraps_posts_and_keeps_author(fixture):
    svc, client, admin, controls, device = fixture
    response = await client.get("/boosty/v1/feed")
    assert response.status_code == 200
    assert response.json()["items"][0]["author_id"] == "author"


@pytest.mark.anyio
async def test_range_head_416_and_stream_cleanup(fixture):
    svc, client, admin, controls, device = fixture
    item = await playback(client)
    path = urlsplit(item["url"]).path
    response = await client.get(path, headers={"Range": "bytes=100-199"})
    assert response.status_code == 206
    assert response.content == bytes(range(100, 200))
    assert response.headers["content-range"] == "bytes 100-199/4096"
    assert controls["closed"] == 1
    head = await client.head(path)
    assert head.status_code == 200 and head.content == b"" and head.headers["content-length"] == "4096"
    assert (await client.get(path, headers={"Range": "bytes=99999-"})).status_code == 416
    assert (await client.get(path, headers={"Range": "bytes=1-2,4-5"})).status_code == 416
    assert not any("authorization" in r.headers or "cookie" in r.headers for r in controls["media_calls"])
    assert controls["closed"] == 3


@pytest.mark.anyio
async def test_expired_cdn_link_is_refreshed_once_with_identity_check(fixture):
    svc, client, admin, controls, device = fixture
    item = await playback(client)
    path = urlsplit(item["url"]).path
    assert (await client.head(path)).status_code == 200
    controls["refresh_media"] = True
    response = await client.get(path, headers={"Range": "bytes=100-199"})
    assert response.status_code == 206 and response.content == bytes(range(100, 200))
    assert controls["post_calls"] == 2


@pytest.mark.anyio
async def test_progress_validation_and_resume(fixture):
    svc, client, admin, controls, device = fixture
    item = await playback(client)
    path = "/boosty/v1/progress/" + item["key"]
    assert (await client.put(path, json={"position_seconds": -1, "duration_seconds": 100, "completed": False})).status_code == 422
    assert (await client.put(path, json={"position_seconds": 101, "duration_seconds": 100, "completed": False})).status_code == 422
    assert (await client.put(path, json={"position_seconds": 42, "duration_seconds": 100, "completed": False})).status_code == 200
    assert (await client.get("/boosty/v1/continue")).json()["items"][0]["position_seconds"] == 42
    assert (await playback(client))["position_seconds"] == 42


@pytest.mark.anyio
async def test_request_limits_and_unknown_media(fixture):
    svc, client, admin, controls, device = fixture
    assert (await admin.post("/admin/session", content=b"x" * 70000)).status_code == 413
    assert (await client.get("/boosty/media/unknown/video.mp4")).status_code == 404
    assert (await client.get("/boosty/v1/posts?author=../private")).status_code == 422
    assert (await client.get("/boosty/v1/posts?author=author&cursor=forged")).status_code == 400


@pytest.mark.anyio
async def test_seek_rejects_changed_file_and_wrong_range(fixture):
    svc, client, admin, controls, device = fixture
    path = urlsplit((await playback(client))["url"]).path
    assert (await client.head(path)).status_code == 200
    controls["changed"] = True
    assert (await client.get(path, headers={"Range": "bytes=100-199"})).status_code == 409
    controls["changed"] = False
    controls["wrong_range"] = True
    assert (await client.get(path, headers={"Range": "bytes=100-199"})).status_code == 502
    assert controls["closed"] == 3


@pytest.mark.anyio
async def test_disconnect_closes_upstream_inside_cancel_scope(fixture):
    import anyio
    from server.stream import media_response
    svc, client, admin, controls, device = fixture
    item = await playback(client)
    response = await media_response(svc, item["playback_id"], "GET")
    async def receive():
        await anyio.sleep_forever()
    async def send(message):
        if message["type"] == "http.response.start":
            scope.cancel()
        await anyio.sleep(0)
    original = svc.stream_slots._value
    with anyio.CancelScope() as scope:
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert controls["closed"] == 1
    assert svc.stream_slots._value == original + 1


@pytest.mark.anyio
async def test_switch_rejects_late_catalog_play_and_token_waiter(fixture):
    svc, client, admin, controls, device = fixture
    controls.update(delay=True, waiting=0, arrived=asyncio.Event(), release=asyncio.Event())
    body = {"author_id": "author", "post_id": "post-1", "video_id": "video-1", "quality": "full_hd"}
    catalog = asyncio.create_task(client.get("/boosty/v1/posts?author=author"))
    play = asyncio.create_task(client.post("/boosty/v1/playbacks", json=body))
    await asyncio.wait_for(controls["arrived"].wait(), 2)
    replacement = {**svc.state.read_session(), "accessToken": "owner-b-access"}
    try:
        assert (await admin.post("/admin/session", json=replacement)).status_code == 200
        _, new_token = svc.state.new_device()
        new_headers = {"Authorization": "Bearer " + new_token}
        active = (await client.post("/boosty/v1/playbacks", json=body, headers=new_headers)).json()
    finally:
        controls["release"].set()
    assert [(await task).status_code for task in (catalog, play)] == [401, 401]
    assert active["playback_id"] in svc.playbacks and not svc.cache

    # A request admitted before a switch must not acquire the replacement token.
    before = svc.boosty.generation
    async with svc.boosty.lock:
        waiter = asyncio.create_task(svc.boosty.get("/v1/feed/post/", generation=before))
        await asyncio.sleep(0)
        svc.boosty.generation += 1
    from server.errors import Problem
    with pytest.raises(Problem, match="DEVICE_REQUIRED"):
        await waiter
