"""Owner-only operations through localhost or an SSH tunnel; never print keys."""
import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


def admin_origin(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Use a loopback admin address, with SSH tunnelling for a remote server")
    return url.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def admin_request(url, key_file, path, payload=None):
    base = admin_origin(url)
    key = json.loads(Path(key_file).read_text(encoding="utf-8-sig"))["key"]
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + "/admin/" + path, data=data,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            code = json.loads(error.read(4096)).get("code", "HTTP_ERROR")
        except ValueError:
            code = "HTTP_ERROR"
        raise RuntimeError(str(code)) from None


def main():
    parser = argparse.ArgumentParser(description="Manage your Boosty connection without printing secrets")
    parser.add_argument("--admin-url", default="http://127.0.0.1:19117")
    parser.add_argument("--key-file", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    load = sub.add_parser("import-session")
    load.add_argument("file")
    approve = sub.add_parser("approve")
    approve.add_argument("code")
    args = parser.parse_args()
    if args.command == "import-session":
        path = Path(args.file)
        if path.stat().st_size > 65536:
            raise ValueError("Session file exceeds 64 KiB")
        result = admin_request(args.admin_url, args.key_file, "session", json.loads(path.read_text(encoding="utf-8-sig")))
    elif args.command == "approve":
        result = admin_request(args.admin_url, args.key_file, "pairing/approve", {"code": args.code})
    else:
        result = admin_request(args.admin_url, args.key_file, "status")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Operation failed: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
