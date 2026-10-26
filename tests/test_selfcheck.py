import hashlib
import json
import subprocess
import sys


def test_installed_cli_checks_real_media_and_preserves_neighbor_files(tmp_path):
    workspace = tmp_path / "checks"
    workspace.mkdir()
    neighbor = workspace / "keep.txt"
    neighbor.write_text("keep me", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-B", "-m", "reelpilot.cli", "selfcheck", "--workspace", str(workspace)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    from pathlib import Path

    run = Path(data["directory"])
    report = data["report"]
    assert run.parent == workspace and run.name.startswith("selfcheck-")
    assert json.loads((run / "selfcheck.json").read_text()) == report
    assert report["models_loaded"] is False and all(report["checks"].values())
    assert report["render_commits_before_corruption"] == 1 and report["render_commits_after_repair"] == 2
    output = run / report["output"]["path"]
    assert hashlib.sha256(output.read_bytes()).hexdigest() == report["output"]["sha256"]
    assert neighbor.read_text() == "keep me"
