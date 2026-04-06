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
