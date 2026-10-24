import asyncio
import re

import anyio
import httpx
from starlette.responses import Response, StreamingResponse

from .errors import Problem


def identity(response):
    total = response.headers.get("content-range", "").rsplit("/", 1)[-1]
    if not total.isdigit():
        total = response.headers.get("content-length")
    return (total, response.headers.get("etag"), response.headers.get("last-modified"))


def same_file(before, after):
    if not before or before[0] != after[0]:
        return False
    if before[1] and not before[1].startswith("W/"):
        return before[1] == after[1]
    return bool(before[2] and before[2] == after[2])


class ClosingStream(StreamingResponse):
    def __init__(self, content, cleanup, **kwargs):
        super().__init__(content, **kwargs)
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.cleanup()


async def media_response(service, identifier, method, range_header=None, if_range=None):
    value = service.media_session(identifier)
    if range_header and (len(range_header) > 100 or not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header)):
        raise Problem("INVALID_RANGE", 416)
    headers = {}
    if range_header:
        headers["Range"] = range_header
    if if_range and len(if_range) <= 256:
        headers["If-Range"] = if_range
    try:
        await asyncio.wait_for(service.stream_slots.acquire(), 2)
    except TimeoutError:
        raise Problem("TOO_MANY_STREAMS", 429) from None
    response = None
    closed = False

    async def cleanup():
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            if response:
                with anyio.CancelScope(shield=True):
                    await response.aclose()
        finally:
            service.stream_slots.release()

    try:
        used_source = value["source"]
        response = await service.safe.open(method, used_source, headers)
        if response.status_code in (401, 403):
            await response.aclose()
            async with value["lock"]:
                if value["source"] == used_source:
                    video = value["video"]
                    fresh, _ = await service.source(video["author_id"], video["post_id"], video["video_id"], value["quality"], generation=value["generation"])
                    check = await service.safe.open("HEAD", fresh)
                    try:
                        if check.status_code != 200 or (value["identity"] and not same_file(value["identity"], identity(check))):
                            raise Problem("MEDIA_CHANGED", 409)
                        if range_header and not value["identity"]:
                            raise Problem("MEDIA_CHANGED", 409)
                        value["source"] = fresh
                    finally:
                        await check.aclose()
            response = await service.safe.open(method, value["source"], headers)
        if response.status_code == 416:
            output = Response(status_code=416, headers={"Content-Range": response.headers.get("content-range", "bytes */*")})
            await cleanup()
            return output
        if response.status_code not in (200, 206):
            raise Problem("UPSTREAM_UNAVAILABLE", 502)
        if response.headers.get("content-type", "").split(";")[0] not in ("video/mp4", "application/octet-stream"):
            raise Problem("UNSUPPORTED_MEDIA", 422)
        if response.status_code == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
            if not match or not range_header:
                raise Problem("INVALID_UPSTREAM_RANGE", 502)
            start, end, total = map(int, match.groups())
            first, last = range_header[6:].split("-")
            expected_start = int(first) if first else max(0, total - int(last))
            expected_end = min(int(last), total - 1) if first and last else total - 1
            if (start != expected_start or end != expected_end or not 0 <= start <= end < total
                    or response.headers.get("content-length", str(end - start + 1)) != str(end - start + 1)):
                raise Problem("INVALID_UPSTREAM_RANGE", 502)
        current, previous = identity(response), value["identity"]
        if previous and (previous[0] != current[0]
                or (previous[1] and current[1] and previous[1] != current[1])
                or (previous[2] and current[2] and previous[2] != current[2])):
            raise Problem("MEDIA_CHANGED", 409)
        value["identity"] = current
        outbound = {k: response.headers[k] for k in ("content-length", "content-range", "accept-ranges", "etag", "last-modified") if k in response.headers}
        outbound.update({"Content-Type": "video/mp4", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        if method == "HEAD":
            output = Response(status_code=response.status_code, headers=outbound)
            await cleanup()
            return output

        async def chunks():
            try:
                async for chunk in response.aiter_raw(chunk_size=65536):
                    service.media_session(identifier)
                    yield chunk
            finally:
                await cleanup()

        return ClosingStream(chunks(), cleanup, status_code=response.status_code, headers=outbound)
    except BaseException as error:
        await cleanup()
        if isinstance(error, httpx.HTTPError):
            raise Problem("UPSTREAM_UNAVAILABLE", 502) from None
        raise
