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
