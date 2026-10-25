import asyncio
import json
import sqlite3
import socket
import time
import urllib.parse

import httpx
import pytest

from server.boosty import Boosty, normalize_post, parse_session
from server.network import SafeHTTP
from server.state import State, video_key
from server.errors import Problem


@pytest.fixture
def anyio_backend():
    return "asyncio"


def session(**updates):
    return dict(accessToken="old-access", refreshToken="refresh-secret", clientId="client-123",
                expiresAt=int((time.time() + 86400) * 1000), **updates)


def test_cookie_import_is_narrow_and_validated():
    original = session()
    wrapped = {"cookies": [{"name": "auth", "domain": ".boosty.to", "value": urllib.parse.quote(json.dumps(original))},
                           {"name": "_clientId", "domain": ".boosty.to", "value": "client-123"},
                           {"name": "unrelated", "domain": ".example.org", "value": "never-save"}]}
    assert parse_session(wrapped) == original
    with pytest.raises(Problem):
        parse_session({"cookies": [{"name": "auth", "domain": ".attacker.org", "value": "{}"}]})


def test_atomic_state_and_stable_history(tmp_path):
    state = State(tmp_path)
    state.write_session(session())
    assert state.read_session()["accessToken"] == "old-access"
    with pytest.raises(Problem):
        state.write_session({"accessToken": "bad"})
    assert state.read_session()["accessToken"] == "old-access"
    key = video_key("author", "post", "video")
    state.remember_video(key, {"key": key, "author_id": "author", "post_id": "post", "video_id": "video", "title": "Film"})
    state.save_progress(key, 42, 100, False)
    assert state.continue_watching()[0]["position_seconds"] == 42
    state.close()
    reopened = State(tmp_path)
    assert reopened.progress(key)["position_seconds"] == 42
    assert len(key) == 64 and "/" not in key
    reopened.close()


def test_switch_account_is_atomic_and_migrates_legacy_session(tmp_path):
    (tmp_path / "session.json").write_text(json.dumps(session()), encoding="utf-8")
    state = State(tmp_path)
    assert state.read_session()["accessToken"] == "old-access"
    assert not (tmp_path / "session.json").exists()
    state.install_session("owner-a", session())
    device, token = state.new_device()
    state.remember_video("key", {"key": "key"})
    state.save_progress("key", 42, 100, False)
    state.db.execute("CREATE TRIGGER fail_switch BEFORE INSERT ON metadata WHEN NEW.key='session' BEGIN SELECT RAISE(ABORT, 'test write failure'); END")
    changed = {**session(), "accessToken": "new-account"}
    with pytest.raises(sqlite3.DatabaseError):
        state.install_session("owner-b", changed)
    state.close()
    state = State(tmp_path)
    assert state.meta("account") == "owner-a"
    assert state.read_session()["accessToken"] == "old-access"
    assert state.device(token) == device and state.progress("key")["position_seconds"] == 42
    state.db.execute("DROP TRIGGER fail_switch")
    assert state.install_session("owner-b", changed)
    assert state.meta("account") == "owner-b" and state.read_session()["accessToken"] == "new-account"
    assert not state.device_exists(device) and not state.continue_watching()
    state.close()


def test_closed_and_processing_posts_are_not_playable():
    video = {"id": "v", "type": "ok_video", "complete": True,
             "playerUrls": [{"type": "full_hd", "url": "https://vd1.okcdn.ru/file"}]}
    assert normalize_post({"id": "p", "hasAccess": False, "data": [video]}, "author") is None
    assert normalize_post({"id": "p", "hasAccess": True, "data": [{**video, "complete": False}]}, "author") is None
    result = normalize_post({"id": "p", "hasAccess": True, "title": "<script>bad</script>", "data": [video]}, "author")
    assert result["videos"][0]["qualities"] == ["full_hd"]
    assert "https://" not in json.dumps(result)


@pytest.mark.anyio
async def test_refresh_single_flight_and_secret_safe_error(tmp_path):
    state = State(tmp_path)
    expired = session()
    expired["expiresAt"] = 1
    state.write_session(expired)
    calls = 0

    async def handler(request):
        nonlocal calls
        assert request.url.path == "/oauth/token/"
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    boosty = Boosty(state, client)
    assert await asyncio.gather(*(boosty.token() for _ in range(10))) == ["new-access"] * 10
    assert calls == 1
    assert state.read_session()["refreshToken"] == "new-refresh"
    await client.aclose()
    state.close()


@pytest.mark.anyio
async def test_media_dns_is_pinned_and_private_redirect_is_rejected():
    observed = []

    async def resolve(host):
        return ["93.184.216.34"]

    def handler(request):
        observed.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    safe = SafeHTTP(client, resolver=resolve)
    with pytest.raises(Problem):
        await safe.open("GET", "https://vd1.okcdn.ru/video?secret=hidden")
    assert str(observed[0].url.host) == "93.184.216.34"
    assert observed[0].headers["host"] == "vd1.okcdn.ru"
    assert observed[0].extensions["sni_hostname"] == "vd1.okcdn.ru"
    assert len(observed) == 1
    await client.aclose()


@pytest.mark.anyio
async def test_dns_rebinding_and_arbitrary_proxy_inputs_are_blocked():
    async def resolve(host):
        return ["127.0.0.1"]

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("must not connect")))
    safe = SafeHTTP(client, resolver=resolve)
    for url in ("http://vd1.okcdn.ru/video", "https://attacker.example/video", "https://vd1.okcdn.ru/video",
                "http://ok9-2.vkuser.net/video", "https://ok9-2.vkuser.net/video",
                "https://vkuser.net.attacker.example/video", "https://notvkuser.net/video",
                "https://user@ok9-2.vkuser.net/video", "https://ok9-2.vkuser.net:8443/video"):
        with pytest.raises(Problem):
            await safe.open("GET", url)
    await client.aclose()


@pytest.mark.anyio
async def test_vkuser_video_redirect_keeps_public_ip_pinning_and_range():
    observed = []

    async def resolve(host):
        return ["93.184.216.34"]

    def handler(request):
        observed.append(request)
        if len(observed) == 1:
            return httpx.Response(302, headers={"location": "https://ok9-2.vkuser.net/video"})
        return httpx.Response(206, content=b"MP4", headers={"Content-Range": "bytes 0-2/3"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await SafeHTTP(client, resolver=resolve).open("GET", "https://vd1.okcdn.ru/video", {"Range": "bytes=0-2"})
        assert response.status_code == 206
        assert await response.aread() == b"MP4"
        await response.aclose()
    assert [r.headers["host"] for r in observed] == ["vd1.okcdn.ru", "ok9-2.vkuser.net"]
    assert all(r.url.host == "93.184.216.34" and r.headers["range"] == "bytes=0-2" for r in observed)
    assert observed[-1].extensions["sni_hostname"] == "ok9-2.vkuser.net"


@pytest.mark.anyio
async def test_vkuser_redirect_to_private_dns_is_rejected():
    observed = []

    async def resolve(host):
        return ["127.0.0.1"] if host == "private.vkuser.net" else ["93.184.216.34"]

    def handler(request):
        observed.append(request)
        return httpx.Response(302, headers={"location": "https://private.vkuser.net/video"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(Problem) as caught:
            await SafeHTTP(client, resolver=resolve).open("GET", "https://ok9-2.vkuser.net/video")
    assert caught.value.code == "UNSAFE_UPSTREAM"
    assert len(observed) == 1


@pytest.mark.anyio
async def test_dns_recovers_transient_failures_before_pinned_request():
    calls = 0
    observed = []

    async def resolve(host):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise socket.gaierror(socket.EAI_NODATA if calls == 1 else socket.EAI_AGAIN, "DNS unavailable")
        return ["93.184.216.34"]

    def handler(request):
        observed.append(request)
        return httpx.Response(206, content=b"MP4")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await SafeHTTP(client, resolve).open("GET", "https://ok9-2.vkuser.net/video?signature=private", {"Range": "bytes=0-2"})
        assert response.status_code == 206
        assert await response.aread() == b"MP4"
        await response.aclose()
    assert calls == 3 and len(observed) == 1
    assert observed[0].url.host == "93.184.216.34"
    assert observed[0].headers["Host"] == observed[0].extensions["sni_hostname"] == "ok9-2.vkuser.net"
    assert observed[0].headers["Range"] == "bytes=0-2"


@pytest.mark.anyio
async def test_dns_failure_is_bounded_and_secret_safe(caplog):
    calls = 0

    async def resolve(host):
        nonlocal calls
        calls += 1
        raise socket.gaierror(socket.EAI_NODATA, "private-signature-must-not-be-logged")

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("must not connect"))) as client:
        with pytest.raises(Problem) as error:
            await SafeHTTP(client, resolve).open("GET", "https://ok9-2.vkuser.net/video?signature=private")
    assert (error.value.code, error.value.status) == ("UPSTREAM_UNAVAILABLE", 502)
    assert calls == 3
    assert "DNS" in caplog.text
    assert "signature" not in caplog.text and "https://" not in caplog.text


@pytest.mark.anyio
async def test_dns_timeout_cancels_each_attempt_and_has_bounded_delays(monkeypatch):
    actual_wait = asyncio.wait_for
    timeouts, delays, canceled = [], [], []

    async def quick_wait(awaitable, timeout):
        timeouts.append(timeout)
        return await actual_wait(awaitable, .001)

    async def delay(seconds):
        delays.append(seconds)

    async def resolve(host):
        try:
            await asyncio.Event().wait()
        finally:
            canceled.append(True)

    monkeypatch.setattr("server.network.asyncio.wait_for", quick_wait)
    monkeypatch.setattr("server.network.asyncio.sleep", delay)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("must not connect"))) as client:
        with pytest.raises(Problem) as error:
            await actual_wait(SafeHTTP(client, resolve).open("GET", "https://ok9-2.vkuser.net/video"), .1)
    assert error.value.code == "UPSTREAM_UNAVAILABLE"
    assert timeouts == [2, 2, 2] and delays == [.2, .4]
    assert len(canceled) == 3


@pytest.mark.anyio
async def test_dns_retry_does_not_bypass_private_answer_or_cancellation():
    calls = 0

    async def resolve(host):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise socket.gaierror(socket.EAI_AGAIN, "temporary")
        return ["93.184.216.34", "127.0.0.1"]

    async def canceled(host):
        raise asyncio.CancelledError()

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("must not connect"))) as client:
        with pytest.raises(Problem) as error:
            await SafeHTTP(client, resolve).open("GET", "https://ok9-2.vkuser.net/video")
        assert error.value.code == "UNSAFE_UPSTREAM" and calls == 2
        with pytest.raises(asyncio.CancelledError):
            await SafeHTTP(client, canceled).open("GET", "https://ok9-2.vkuser.net/video")


@pytest.mark.anyio
@pytest.mark.parametrize("image", [False, True])
async def test_dns_recovers_for_redirects_and_images(image):
    calls, observed = {}, []
    first = "img.boosty.to" if image else "vd1.okcdn.ru"

    async def resolve(host):
        calls[host] = calls.get(host, 0) + 1
        if calls[host] == 1:
            raise socket.gaierror(socket.EAI_AGAIN, "temporary")
        return ["93.184.216.34"]

    def handler(request):
        observed.append(request.headers["host"])
        if not image and len(observed) == 1:
            return httpx.Response(302, headers={"location": "https://ok9-2.vkuser.net/video"})
        return httpx.Response(200, content=b"data")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await SafeHTTP(client, resolve).open("GET", "https://" + first + "/resource", image=image)
        assert response.status_code == 200
        await response.aclose()
    assert observed == ([first] if image else [first, "ok9-2.vkuser.net"])
    assert all(count == 2 for count in calls.values())
