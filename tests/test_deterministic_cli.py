import yaml
from typer.testing import CliRunner

from panier.cli import app
from panier.deterministic import explain_item, is_no_llm_enabled, no_llm_status


def test_no_llm_env_flag_parses_common_values() -> None:
    assert is_no_llm_enabled({"PANIER_NO_LLM": "1"}) is True
    assert is_no_llm_enabled({"PANIER_NO_LLM": "true"}) is True
    assert is_no_llm_enabled({"PANIER_NO_LLM": "0"}) is False
    assert is_no_llm_enabled({}) is False


def test_no_llm_cli_override_is_reported() -> None:
    status = no_llm_status({"PANIER_NO_LLM": "0"}, cli_no_llm=True)

    assert status.no_llm is True
    assert status.source == "--no-llm"
    assert status.mode == "deterministic"


def test_explain_item_is_deterministic_and_local() -> None:
    explanation = explain_item("Tomates concassées bio 400g")

    assert explanation.canonical_name == "tomates concassées"
    assert explanation.query == "tomates concassées"
    assert explanation.confidence == "medium"


def test_llm_status_command_reflects_env(monkeypatch) -> None:
    monkeypatch.setenv("PANIER_NO_LLM", "1")

    result = CliRunner().invoke(app, ["llm", "status"])

    assert result.exit_code == 0
    assert "Mode: deterministic" in result.output
    assert "LLM autorisé: non" in result.output
    assert "Garde-fou: PANIER_NO_LLM" in result.output
    assert "Appels LLM implémentés: non" in result.output


def test_global_no_llm_flag_reflects_in_status() -> None:
    result = CliRunner().invoke(app, ["--no-llm", "llm", "status"])

    assert result.exit_code == 0
    assert "Mode: deterministic" in result.output
    assert "Source: --no-llm" in result.output


def test_explain_item_command_outputs_core_fields() -> None:
    result = CliRunner().invoke(app, ["explain", "item", "Tomates concassées bio 400g"])

    assert result.exit_code == 0
    assert "Entrée: Tomates concassées bio 400g" in result.output
    assert "Nom canonique: tomates concassées" in result.output
    assert "Requête: tomates concassées" in result.output
    assert "Confiance: medium" in result.output


def test_explain_offer_command_outputs_deterministic_fields(tmp_path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "explain",
            "offer",
            "yaourt",
            "Yaourt Maison A",
            "--price",
            "2.40",
            "--store",
            "leclerc",
            "--data-dir",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert "Article demandé: yaourt" in result.output
    assert "Offre: Yaourt Maison A" in result.output
    assert "Décision: déterministe locale, aucun appel LLM" in result.output


def test_cache_import_and_compare_without_prices_uses_local_cache(tmp_path) -> None:
    shopping = tmp_path / "list.yaml"
    prices = tmp_path / "prices.yaml"
    shopping.write_text(yaml.safe_dump({"items": [{"name": "yaourt"}]}), encoding="utf-8")
    prices.write_text(
        yaml.safe_dump(
            {
                "offers": [
                    {
                        "store": "leclerc",
                        "item": "yaourt",
                        "product": "Yaourt local",
                        "price": 1.8,
                        "confidence": "high",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    import_result = CliRunner().invoke(
        app, ["cache", "import", str(prices), "--data-dir", str(tmp_path)]
    )
    compare_result = CliRunner().invoke(
        app, ["compare", str(shopping), "--data-dir", str(tmp_path), "--max-stores", "1"]
    )

    assert import_result.exit_code == 0
    assert "Offres importées: 1" in import_result.output
    assert compare_result.exit_code == 0
    assert "Yaourt local" in compare_result.output
    assert "Total: 1.80 €" in compare_result.output


def test_substitution_and_constraints_are_applied_in_compare(tmp_path) -> None:
    shopping = tmp_path / "list.yaml"
    prices = tmp_path / "prices.yaml"
    shopping.write_text(yaml.safe_dump({"items": [{"name": "lait"}]}), encoding="utf-8")
    prices.write_text(
        yaml.safe_dump(
            {
                "offers": [
                    {
                        "store": "leclerc",
                        "item": "boisson avoine",
                        "product": "Boisson avoine",
                        "price": 2.2,
                        "confidence": "medium",
                    },
                    {
                        "store": "auchan",
                        "item": "lait",
                        "product": "Lait Auchan",
                        "price": 1.0,
                        "confidence": "high",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    sub_result = CliRunner().invoke(
        app,
        [
            "substitution",
            "add",
            "lait",
            "boisson avoine",
            "--data-dir",
            str(tmp_path),
        ],
    )
    constraint_result = CliRunner().invoke(
        app,
        [
            "constraint",
            "set",
            "--blocked-store",
            "auchan",
            "--data-dir",
            str(tmp_path),
        ],
    )
    compare_result = CliRunner().invoke(
        app,
        ["compare", str(shopping), "--prices", str(prices), "--data-dir", str(tmp_path)],
    )

    assert sub_result.exit_code == 0
    assert constraint_result.exit_code == 0
    assert compare_result.exit_code == 0
    assert "Boisson avoine" in compare_result.output
    assert "Lait Auchan" not in compare_result.output


def test_doctor_determinism_lists_local_files(tmp_path) -> None:
    result = CliRunner().invoke(app, ["doctor", "determinism", "--data-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert "Diagnostic déterminisme Panier" in result.output
    assert "Chemin critique: règles locales + fichiers YAML + tie-breaks stables" in result.output
    assert "cache prix:" in result.output
