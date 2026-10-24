import pytest

from scripts.manage import admin_origin, admin_request


def test_admin_key_cannot_be_sent_to_remote_or_credentialed_url():
    assert admin_origin('http://127.0.0.1:19117/') == 'http://127.0.0.1:19117'
    for url in ('http://example.com', 'http://user:secret@127.0.0.1', 'http://127.0.0.1/path', 'file:///tmp/key'):
        with pytest.raises(ValueError):
            admin_origin(url)


def test_admin_redirect_does_not_forward_key(tmp_path, monkeypatch):
    import io
    import urllib.request
    import urllib.response
    from email.message import Message
    from scripts import manage
    key = tmp_path / "admin.json"
    key.write_text('{"key":"test-owner-key"}', encoding="utf-8")
    forwarded = []
    def local(self, request):
        headers = Message()
        headers["Location"] = "https://outside.invalid/collect"
        response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
        response.msg = "Found"
        return response
    def outside(self, request):
        forwarded.append(request.get_header("Authorization"))
        raise RuntimeError("Unexpected external request")
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", local)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", outside)
    with monkeypatch.context() as baseline:
        baseline.setattr(manage, "NoRedirect", urllib.request.HTTPRedirectHandler)
        with pytest.raises(RuntimeError):
            admin_request("http://127.0.0.1:19117", key, "status")
    assert forwarded == ["Bearer test-owner-key"]
    forwarded.clear()
    with pytest.raises(RuntimeError):
        admin_request("http://127.0.0.1:19117", key, "status")
    assert forwarded == []
