import json
import math
from urllib.parse import unquote

from .errors import Problem


def parse_session(value):
    try:
        if isinstance(value, str):
            value = json.loads(unquote(value))
        if isinstance(value, dict) and "cookies" in value:
            cookies = {c["name"]: c["value"] for c in value["cookies"]
                       if c.get("domain", "").lstrip(".") == "boosty.to"}
            value = json.loads(unquote(cookies["auth"]))
            value["clientId"] = cookies["_clientId"]
        result = {k: value[k] for k in ("accessToken", "refreshToken", "clientId", "expiresAt")}
        if any(not isinstance(result[k], str) or not 1 <= len(result[k]) <= 8192
               for k in ("accessToken", "refreshToken", "clientId")):
            raise ValueError()
        if isinstance(result["expiresAt"], bool) or not math.isfinite(float(result["expiresAt"])):
            raise ValueError()
        result["expiresAt"] = int(result["expiresAt"])
        if result["expiresAt"] < 0:
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        raise Problem("INVALID_SESSION") from None
