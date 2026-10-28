from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeConnect

from prusa_connect.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert "prusa-connect" in capsys.readouterr().out


def test_usage_error_is_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bogus"]) == 2
    assert main(["print"]) == 2


@pytest.fixture
def run(fake: FakeConnect, logged_in: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = main(list(argv), transport=fake)
        out, err = capsys.readouterr()
        assert "SECRET" not in out and "SECRET" not in err, "a token leaked into output"
        return code, out, err

    return _run


@pytest.fixture
def no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))


# --- read side ----------------------------------------------------------------


def test_printers_table_and_json(run: Any) -> None:
    code, out, _ = run("printers")
    assert code == 0 and "Core One" in out and "IDLE" in out
    code, out, _ = run("printers", "--json")
    assert json.loads(out)[0]["uuid"] == "aaaa-1111"


def test_status(run: Any, fake: FakeConnect) -> None:
    fake.printer.update(connect_state="PRINTING", job_info={"id": 3, "display_name": "benchy.bgcode",
                                                           "progress": 42, "time_remaining": 3720,
                                                           "time_printing": 600})
    code, out, _ = run("status", "core")
    assert code == 0
    assert "PRINTING" in out and "42%" in out and "1h02m" in out and "benchy" in out


def test_status_json(run: Any) -> None:
    _code, out, _ = run("--json", "status", "Core One")
    data = json.loads(out)
    assert data["state"] == "IDLE" and data["nozzle"]["actual"] == 24.5


def test_unknown_printer(run: Any) -> None:
    code, _, err = run("status", "mk4")
    assert code == 1 and "no single printer matches" in err


def test_files_jobs_whoami(run: Any) -> None:
    assert run("files")[0] == 0
    code, out, _ = run("files", "--path", "/usb")
    assert code == 0 and "a.bgcode" in out
    code, out, _ = run("jobs")
    assert code == 0 and "FIN_OK" in out and "benchy" in out
    code, out, _ = run("whoami")
    assert code == 0 and "kp@example.com" in out


def test_read_commands_send_no_writes(run: Any, fake: FakeConnect) -> None:
    for argv in (["printers"], ["status"], ["files"], ["jobs"], ["whoami"]):
        run(*argv)
    assert fake.writes() == []


# --- write side ---------------------------------------------------------------


def _gcode(tmp_path: Path) -> Path:
    f = tmp_path / "part.bgcode"
    f.write_bytes(b"GCDE" + b"\0" * 60)
    return f


def test_print_dry_run_shows_plan_and_sends_nothing(run: Any, fake: FakeConnect) -> None:
    fake.printer["connect_state"] = "READY"
    code, out, _err = run("print", "/usb/A~1.BGC", "Core One", "--dry-run")
    assert code == 0
    plan = json.loads(out)
    assert plan["requests"][0]["json"] == {"command": "START_PRINT", "kwargs": {"path": "/usb/A~1.BGC"}}
    assert plan["requests"][0]["url"] == "https://connect.prusa3d.com/app/printers/aaaa-1111/commands/sync"
    assert "not shown" in plan["requests"][0]["headers"]["Authorization"]
    assert fake.writes() == []


@pytest.mark.parametrize("state", ["IDLE", "FINISHED", "STOPPED", "PRINTING", "OFFLINE"])
def test_print_refused_unless_ready(run: Any, fake: FakeConnect, state: str) -> None:
    fake.printer["connect_state"] = state
    code, _, err = run("print", "/usb/A~1.BGC", "Core One", "--yes")
    assert code == 1 and "not READY" in err and "bed is clear" in err
    assert fake.writes() == []


def test_print_dry_run_reports_refusal(run: Any, fake: FakeConnect) -> None:
    code, out, err = run("print", "/usb/A~1.BGC", "Core One", "--dry-run")
    assert code == 1 and json.loads(out)["would_refuse"] and "REFUSED" in err
    assert fake.writes() == []


def test_print_needs_yes_without_tty(run: Any, fake: FakeConnect, no_tty: None) -> None:
    fake.printer["connect_state"] = "READY"
    code, _, err = run("print", "/usb/A~1.BGC", "Core One")
    assert code == 2 and "--yes" in err
    assert fake.writes() == []


def test_print_path_with_yes_sends_start_print(run: Any, fake: FakeConnect) -> None:
    fake.printer["connect_state"] = "READY"
    code, _, _ = run("print", "/usb/A~1.BGC", "Core One", "--yes")
    assert code == 0
    (w,) = fake.writes()
    assert w.url.endswith("/app/printers/aaaa-1111/commands/sync")
    assert json.loads(w.body) == {"command": "START_PRINT", "kwargs": {"path": "/usb/A~1.BGC"}}  # type: ignore[arg-type]


def test_print_local_file_uploads_then_queues_without_set_ready(run: Any, fake: FakeConnect,
                                                                tmp_path: Path) -> None:
    fake.printer["connect_state"] = "READY"
    f = _gcode(tmp_path)
    code, _, _ = run("print", str(f), "Core One", "--yes")
    assert code == 0
    reg, put, queue = fake.writes()
    assert json.loads(reg.body) == {"destination": "/usb", "filename": "part.bgcode", "size": 64,  # type: ignore[arg-type]
                                    "printer_uuid": "aaaa-1111"}
    assert put.method == "PUT" and "upload_id=555" in put.url
    assert put.headers["Content-Type"] == "text/x.gcode"
    body = json.loads(queue.body)  # type: ignore[arg-type]
    assert body == {"hash": "HASH1", "team_id": 4242, "position": 0}
    assert "set_ready" not in body


def test_upload_dry_run_and_real(run: Any, fake: FakeConnect, tmp_path: Path) -> None:
    f = _gcode(tmp_path)
    code, out, _ = run("upload", str(f), "Core One", "--dry-run")
    assert code == 0 and fake.writes() == []
    plan = json.loads(out)
    assert plan["moves_printer"] is False and len(plan["requests"]) == 2
    assert "64 bytes" in plan["requests"][1]["body"]
    code, out, _ = run("upload", str(f), "Core One")
    assert code == 0
    assert [w.method for w in fake.writes()] == ["POST", "PUT"]


def test_upload_rejects_non_gcode(run: Any, fake: FakeConnect, tmp_path: Path) -> None:
    f = tmp_path / "model.stl"
    f.write_text("solid")
    code, _, err = run("upload", str(f), "Core One")
    assert code == 1 and "sliced" in err and fake.writes() == []


@pytest.mark.parametrize("cmd,state,command", [("pause", "PRINTING", "PAUSE_PRINT"),
                                               ("resume", "PAUSED", "RESUME_PRINT"),
                                               ("stop", "PRINTING", "STOP_PRINT")])
def test_control_commands(run: Any, fake: FakeConnect, cmd: str, state: str, command: str) -> None:
    fake.printer["connect_state"] = state
    code, out, _ = run(cmd, "Core One", "--dry-run")
    assert code == 0 and json.loads(out)["requests"][0]["json"]["command"] == command
    assert fake.writes() == []
    assert run(cmd, "Core One", "--yes")[0] == 0
    (w,) = fake.writes()
    assert json.loads(w.body) == {"command": command, "kwargs": {}}  # type: ignore[arg-type]


def test_control_wrong_state_refused(run: Any, fake: FakeConnect) -> None:
    code, _, err = run("pause", "Core One", "--yes")
    assert code == 1 and "PAUSE_PRINT needs PRINTING" in err and fake.writes() == []


def test_control_needs_yes_without_tty(run: Any, fake: FakeConnect, no_tty: None) -> None:
    fake.printer["connect_state"] = "PRINTING"
    assert run("stop", "Core One")[0] == 2
    assert fake.writes() == []


def test_tty_confirmation(run: Any, fake: FakeConnect, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.printer["connect_state"] = "PRINTING"

    class TTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", TTY("no\n"))
    assert run("pause", "Core One")[0] == 2 and fake.writes() == []
    monkeypatch.setattr("sys.stdin", TTY("yes\n"))
    assert run("pause", "Core One")[0] == 0 and len(fake.writes()) == 1
