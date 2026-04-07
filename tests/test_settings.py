from pathlib import Path

import pytest

from incident_copilot.core.settings import Settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_relative_paths_resolve_from_project_root() -> None:
    app_settings = Settings(data_dir=Path("./tmp-data"), index_db_path=Path("./tmp-index/repo.db"))

    assert app_settings.data_dir == PROJECT_ROOT / "tmp-data"
    assert app_settings.index_db_path == PROJECT_ROOT / "tmp-index/repo.db"


def test_directory_index_db_path_raises_on_ensure_directories(tmp_path: Path) -> None:
    db_dir = tmp_path / "index_dir"
    db_dir.mkdir(parents=True)
    app_settings = Settings(data_dir=tmp_path / "data", index_db_path=db_dir)

    with pytest.raises(ValueError, match="must be a file path"):
        app_settings.ensure_directories()


def test_ensure_directories_falls_back_when_configured_path_is_unwritable(monkeypatch: pytest.MonkeyPatch) -> None:
    app_settings = Settings(data_dir=Path("/unlikely"), index_db_path=Path("/unlikely/repo.db"))
    calls = {"count": 0}

    def fail_once() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("readonly")

    monkeypatch.setattr(app_settings, "_ensure_directories_once", fail_once)
    with pytest.warns(RuntimeWarning, match="Falling back"):
        app_settings.ensure_directories()

    assert "incident-copilot" in str(app_settings.data_dir)
    assert "repo_index.db" in str(app_settings.index_db_path)
