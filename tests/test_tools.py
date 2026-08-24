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
