"""PR amont : `profile show --household <name>` adresse un foyer nommé.

Comportement demandé (plan panier-foyer-amont.md) :
- défaut = foyer actif (comportement inchangé, exit 0) ;
- `--household <name>` = dump json même schéma que l'actif ;
- exit != 0 si le foyer est absent du data-dir.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from panier.cli import app

RUNNER = CliRunner()


def _seed_foyers(data_dir: Path, names: tuple[str, ...], active: str) -> None:
    for name in names:
        result = RUNNER.invoke(
            app,
            ["profile", "create", name, "--data-dir", str(data_dir), "--format", "json"],
        )
        assert result.exit_code == 0, result.output
    use = RUNNER.invoke(
        app,
        ["profile", "use", active, "--data-dir", str(data_dir), "--format", "json"],
    )
    assert use.exit_code == 0, use.output


def test_profile_show_without_household_defaults_to_active(tmp_path: Path) -> None:
    _seed_foyers(tmp_path, ("maison", "apiary"), active="maison")

    result = RUNNER.invoke(
        app, ["profile", "show", "--data-dir", str(tmp_path), "--format", "json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["household"]["name"] == "maison"


def test_profile_show_household_named(tmp_path: Path) -> None:
    _seed_foyers(tmp_path, ("maison", "apiary"), active="maison")

    result = RUNNER.invoke(
        app,
        [
            "profile",
            "show",
            "--household",
            "apiary",
            "--data-dir",
            str(tmp_path),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["household"]["name"] == "apiary"
    assert set(payload.keys()) == {"household", "profile"}
    # même schéma que le dump de l'actif
    active_result = RUNNER.invoke(
        app, ["profile", "show", "--data-dir", str(tmp_path), "--format", "json"]
    )
    active_payload = json.loads(active_result.output)
    assert set(payload.keys()) == set(active_payload.keys())


def test_profile_show_household_missing_exits_nonzero(tmp_path: Path) -> None:
    _seed_foyers(tmp_path, ("maison",), active="maison")

    result = RUNNER.invoke(
        app,
        [
            "profile",
            "show",
            "--household",
            "inconnu",
            "--data-dir",
            str(tmp_path),
            "--format",
            "json",
        ],
    )

    assert result.exit_code != 0
    assert "inconnu" in (result.output + str(result.exception))


def test_profile_show_household_text_format(tmp_path: Path) -> None:
    _seed_foyers(tmp_path, ("maison", "apiary"), active="maison")

    result = RUNNER.invoke(
        app,
        ["profile", "show", "--household", "apiary", "--data-dir", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Foyer:" in result.output
    assert "apiary" in result.output
