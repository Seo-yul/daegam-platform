"""파일 호출: 올리기·내려받기·목록, 10 MiB, 500 개, 거절 (P40 3절)."""

from __future__ import annotations

import base64
import os

from conftest import Sandbox

from daegam_sandbox.files import MAX_FILE, MAX_LIST


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_upload_then_execute_and_download_see_one_workspace(sandbox: Sandbox) -> None:
    status, body = sandbox.call(
        "/files/upload",
        {
            "files": [
                {"path": "/workspace/a.csv", "content_b64": b64(b"1,2\n")},
                {"path": "/uploads/report.xlsx", "content_b64": b64(b"PK\x03\x04")},
                {"path": "/tmp/.deepagents_edit_x_old", "content_b64": b64(b"old")},
            ]
        },
    )
    assert status == 200 and [r["error"] for r in body["results"]] == [None, None, None]
    assert sandbox.execute("cat a.csv uploads/report.xlsx | head -c 4")["output"] == "1,2\n"
    sandbox.execute("echo made > out.txt")
    status, body = sandbox.call("/files/download", {"paths": ["/workspace/out.txt", "a.csv"]})
    got = [base64.b64decode(r["content_b64"]) for r in body["results"]]
    assert got == [b"made\n", b"1,2\n"]


def test_file_errors_use_the_deepagents_words(sandbox: Sandbox) -> None:
    sandbox.execute("mkdir d; mkfifo pipe; ln -s /etc/passwd link")
    status, body = sandbox.call(
        "/files/download",
        {"paths": ["/workspace/none", "/workspace/d", "/etc/passwd", "/workspace/link", "pipe"]},
    )
    assert [r["error"] for r in body["results"]] == [
        "file_not_found",
        "is_directory",
        "invalid_path",
        "invalid_path",
        "not_a_file",
    ]
    status, body = sandbox.call(
        "/files/upload",
        {
            "files": [
                {"path": "/workspace/d", "content_b64": b64(b"x")},
                {"path": "/workspace/../x", "content_b64": b64(b"x")},
                {"path": "/workspace/y", "content_b64": "%%%"},
                {"path": "/workspace/pipe", "content_b64": b64(b"x")},
            ]
        },
    )
    assert [r["error"] for r in body["results"]] == [
        "is_directory",
        "invalid_path",
        "invalid_content",
        "not_a_file",
    ]


def test_files_over_ten_mebibytes_are_refused(sandbox: Sandbox) -> None:
    (sandbox.workspace / "big.bin").write_bytes(b"\0" * (MAX_FILE + 1))
    _, body = sandbox.call("/files/download", {"paths": ["/workspace/big.bin"]})
    assert body["results"][0]["error"] == "too_large"
    _, body = sandbox.call(
        "/files/upload",
        {"files": [{"path": "/workspace/up.bin", "content_b64": b64(b"\0" * (MAX_FILE + 1))}]},
    )
    assert body["results"][0]["error"] == "too_large"


def test_the_list_skips_excluded_roots_and_symlinks_and_is_capped(sandbox: Sandbox) -> None:
    ws = sandbox.workspace
    (ws / "uploads").mkdir()
    (ws / "uploads" / "a.csv").write_text("x")
    (ws / "skills").mkdir()
    (ws / "skills" / "SKILL.md").write_text("x")
    (ws / "out").mkdir()
    (ws / "out" / "chart.png").write_bytes(b"\x89PNG")
    os.symlink("/etc/passwd", ws / "link")
    _, body = sandbox.call("/files/list", {"exclude": ["uploads", "skills"]})
    assert [f["path"] for f in body["files"]] == ["/workspace/out/chart.png"]
    assert body["files"][0]["size"] == 4 and body["truncated"] is False
    sandbox.execute(f"mkdir many; for i in $(seq {MAX_LIST + 20}); do : > many/f$i; done")
    _, body = sandbox.call("/files/list", {})
    assert len(body["files"]) == MAX_LIST and body["truncated"] is True
    assert sandbox.call("/files/list", {"exclude": ["a/b"]})[0] == 400
