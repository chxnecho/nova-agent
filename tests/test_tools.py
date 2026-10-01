import asyncio

import pytest
from nova.llm.base import ToolCall
from nova.tools.base import ToolRegistry, tool
from nova.tools.filesystem import FilesystemTools
from nova.tools.shell import ShellTool


def test_registry_duplicate_rejected():
    reg = ToolRegistry()

    @tool(name="t1", description="d", parameters={"type": "object", "properties": {}})
    async def t1(**kw):
        return "ok"

    reg.register(t1)
    with pytest.raises(ValueError):
        reg.register(t1)


def test_filesystem_sandbox(tmp_path):
    fs = FilesystemTools(tmp_path)
    out = asyncio.run(fs.write_file("sub/a.txt", "hello"))
    assert out.startswith("OK")
    assert (tmp_path / "sub" / "a.txt").read_text() == "hello"

    with pytest.raises(PermissionError):
        asyncio.run(fs.read_file("../outside.txt"))
    with pytest.raises(PermissionError):
        asyncio.run(fs.write_file("/etc/passwd", "x"))


def test_edit_file_uniqueness(tmp_path):
    fs = FilesystemTools(tmp_path)
    asyncio.run(fs.write_file("f.txt", "aa bb aa"))
    r = asyncio.run(fs.edit_file("f.txt", "aa", "zz"))
    assert r.startswith("ERROR")  # ambiguous
    r = asyncio.run(fs.edit_file("f.txt", "bb", "cc"))
    assert r.startswith("OK")
    assert (tmp_path / "f.txt").read_text() == "aa cc aa"


def test_shell_denylist(tmp_path):
    sh = ShellTool(str(tmp_path))
    r = asyncio.run(sh.run("rm -rf /"))
    assert r.startswith("ERROR: command blocked")


def test_shell_echo(tmp_path):
    sh = ShellTool(str(tmp_path))
    r = asyncio.run(sh.run("echo nova-test-123"))
    assert "nova-test-123" in r
    assert "[OK]" in r


async def test_registry_execute_unknown_tool():
    reg = ToolRegistry()
    call = ToolCall(id="1", name="nope", arguments={})
    result = await reg.execute(call)
    assert result.startswith("ERROR: unknown tool")


# ---- shell: enhanced denylist + workspace-write guard ----


def test_shell_normalized_denylist(tmp_path):
    """Extra whitespace must not bypass the denylist (regression)."""
    sh = ShellTool(str(tmp_path))
    r = asyncio.run(sh.run("rm -rf  /"))  # double space
    assert r.startswith("ERROR: command blocked")
    r2 = asyncio.run(sh.run("rm -fr /*"))
    assert r2.startswith("ERROR: command blocked")


def test_shell_write_outside_sandbox_blocked(tmp_path):
    sh = ShellTool(str(tmp_path), workspace_root=str(tmp_path))
    r = asyncio.run(sh.run("touch /etc/nova-evil"))
    assert "outside the workspace sandbox" in r
    r2 = asyncio.run(sh.run("echo hi > /tmp/nova-evil"))
    assert "outside the workspace sandbox" in r2


def test_shell_write_inside_sandbox_allowed(tmp_path):
    sh = ShellTool(str(tmp_path), workspace_root=str(tmp_path))
    r = asyncio.run(sh.run("touch a.txt && ls a.txt"))
    assert "[OK]" in r


# ---- web_fetch: SSRF guard ----


def test_web_blocks_loopback():
    from nova.tools.web import WebTools

    wt = WebTools()
    with pytest.raises(ValueError):
        wt._validate_url("http://127.0.0.1:8080/admin")
    with pytest.raises(ValueError):
        wt._validate_url("http://[::1]/x")


def test_web_blocks_non_http_scheme():
    from nova.tools.web import WebTools

    with pytest.raises(ValueError):
        WebTools()._validate_url("file:///etc/passwd")
    with pytest.raises(ValueError):
        WebTools()._validate_url("javascript:alert(1)")


def test_web_allow_private_opt_in():
    import httpx
    from nova.tools.web import WebTools

    def fake_getaddrinfo(host, port, *a, **kw):
        import socket

        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 0))]

    wt = WebTools(
        allow_private=True,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="private-ok")),
        resolver=fake_getaddrinfo,
    )
    assert wt._validate_url("http://127.0.0.1:8080/x") == "http://127.0.0.1:8080/x"
    assert "private-ok" in asyncio.run(wt.fetch("http://internal.test/x"))


def test_web_allowed_domains_deny():
    from nova.tools.web import WebTools

    wt = WebTools(allowed_domains=["example.com"])
    with pytest.raises(ValueError):
        wt._validate_url("http://evil.example.net/x")


def test_fetch_pins_ip_and_keeps_host():
    """The request must go to the *resolved IP* while keeping the original
    hostname in the Host header — closing the resolve-then-connect race."""
    import socket

    import httpx

    captured = {}

    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0))]

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["host_header"] = request.headers.get("host")
        return httpx.Response(200, text="pinned-ok")

    from nova.tools.web import WebTools

    wt = WebTools(transport=httpx.MockTransport(handler), resolver=fake_getaddrinfo)
    out = asyncio.run(wt.fetch("http://example.com/hello"))

    assert "pinned-ok" in out
    assert captured["url"].startswith("http://93.184.216.34/hello")
    assert captured["host_header"] == "example.com"


def test_fetch_rejects_multi_record_rebinding():
    """A hostname resolving to one public + one private address is rejected:
    both records must be public (multi-record rebinding defense)."""
    import socket

    import httpx

    def fake_getaddrinfo(host, port, *a, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", port or 0)),
        ]

    from nova.tools.web import WebTools

    wt = WebTools(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="should not get here")
        ),
        resolver=fake_getaddrinfo,
    )
    out = asyncio.run(wt.fetch("http://rebind.example/hello"))
    assert out.startswith("ERROR:")
    assert "private" in out  # rejected at entry validation or at the pin step
