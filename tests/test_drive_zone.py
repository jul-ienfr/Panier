from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import panier.cli as cli
import panier.zone as zone
from panier.cli import app
from panier.household import (
    HouseholdProfile,
    load_household,
    save_household,
    set_active_household,
)
from panier.models import StoreOffer
from panier.zone import (
    DriveCandidate,
    GeoPoint,
    ZoneError,
    apply_selection,
    discover_drives,
    drive_zone,
    geocode,
    toggle_store,
)

RUNNER = CliRunner()


def _commune(name: str, lat: float, lon: float, postal: str, code: str) -> dict:
    return {
        "nom": name,
        "code": code,
        "codesPostaux": [postal],
        "centre": {"type": "Point", "coordinates": [lon, lat]},
    }


def _element(name: str, lat: float, lon: float, brand: str, **tags: str) -> dict:
    payload = {"name": name, "brand": brand, **tags}
    return {"type": "node", "id": 1, "lat": lat, "lon": lon, "tags": payload}


def _household(tmp_path: Path, stores: dict[str, bool] | None = None) -> HouseholdProfile:
    household = HouseholdProfile(name="foyer", stores=stores or {})
    save_household(tmp_path, household)
    set_active_household(tmp_path, household.name)
    return household


def test_geocode_postal_code_spans_several_communes(monkeypatch) -> None:
    payload = [
        _commune("Callian", 43.62, 6.75, "83440", "83029"),
        _commune("Montauroux", 43.59, 6.80, "83440", "83081"),
    ]
    monkeypatch.setattr(zone, "_get_json", lambda *a, **k: payload)
    point = geocode("83440")
    assert point.label == "83440 — 2 communes"
    assert point.city is None
    assert point.postal_code == "83440"
    assert point.latitude == pytest.approx(43.605)
    assert point.longitude == pytest.approx(6.775)


def test_geocode_city_names_the_single_commune(monkeypatch) -> None:
    monkeypatch.setattr(
        zone, "_get_json", lambda *a, **k: [_commune("Montauroux", 43.59, 6.80, "83440", "83081")]
    )
    point = geocode("Montauroux")
    assert point.label == "Montauroux (83440)"
    assert point.city == "Montauroux"


def test_geocode_rejects_empty_query() -> None:
    with pytest.raises(ZoneError):
        geocode("   ")


def test_geocode_raises_when_commune_unknown(monkeypatch) -> None:
    monkeypatch.setattr(zone, "_get_json", lambda *a, **k: [])
    with pytest.raises(ZoneError, match="introuvable"):
        geocode("99999")


def test_discover_drives_filters_dedupes_and_sorts(monkeypatch) -> None:
    payload = {
        "elements": [
            _element("E.Leclerc", 43.60, 6.80, "E.Leclerc"),
            _element("E.Leclerc", 43.60, 6.80, "E.Leclerc"),
            _element("E.Leclerc espace culturel", 43.61, 6.81, "E.Leclerc"),
            _element("Lidl", 43.70, 6.90, "Lidl"),
            _element("Boulangerie", 43.62, 6.82, "Boulangerie"),
            {"type": "node", "id": 9, "tags": {"name": "Sans coordonnées"}},
        ]
    }
    monkeypatch.setattr(zone, "_get_json", lambda *a, **k: payload)
    location = GeoPoint(label="83440", latitude=43.59, longitude=6.80)
    candidates = discover_drives(location, radius_km=20)

    assert [candidate.name for candidate in candidates] == [
        "E.Leclerc",
        "E.Leclerc espace culturel",
        "Boulangerie",
        "Lidl",
    ]
    leclerc = candidates[0]
    assert leclerc.store == "leclerc"
    assert leclerc.collectable is True
    assert leclerc.distance_km < candidates[-1].distance_km
    assert candidates[1].collectable is False
    assert candidates[2].store is None and candidates[2].collectable is False
    assert candidates[3].store == "lidl" and candidates[3].collectable is False


def test_discover_drives_reads_way_center(monkeypatch) -> None:
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 42,
                "center": {"lat": 43.60, "lon": 6.80},
                "tags": {"name": "Carrefour Market", "brand": "Carrefour Market"},
            }
        ]
    }
    monkeypatch.setattr(zone, "_get_json", lambda *a, **k: payload)
    candidates = discover_drives(GeoPoint(label="83440", latitude=43.59, longitude=6.80))
    assert len(candidates) == 1
    assert candidates[0].store == "carrefour"
    assert candidates[0].collectable is True
    assert candidates[0].latitude == pytest.approx(43.60)


def test_discover_drives_rejects_out_of_range_radius() -> None:
    with pytest.raises(ZoneError, match="rayon invalide"):
        discover_drives(GeoPoint(label="x", latitude=0, longitude=0), radius_km=0)
    with pytest.raises(ZoneError, match="rayon invalide"):
        discover_drives(
            GeoPoint(label="x", latitude=0, longitude=0), radius_km=zone.MAX_RADIUS_KM + 1
        )


def test_overpass_env_override_disables_mirror_fallback(monkeypatch) -> None:
    calls: list[str] = []

    def fake(url: str, **kwargs: object) -> dict:
        calls.append(url)
        raise ZoneError("boom")

    monkeypatch.setattr(zone, "_get_json", fake)
    with pytest.raises(ZoneError, match="aucun miroir"):
        discover_drives(
            GeoPoint(label="x", latitude=0, longitude=0),
            environ={zone.OVERPASS_ENV_VAR: "http://localhost:1234/api"},
        )
    assert calls == ["http://localhost:1234/api"]


def test_overpass_falls_back_to_next_mirror(monkeypatch) -> None:
    calls: list[str] = []

    def fake(url: str, **kwargs: object) -> dict:
        calls.append(url)
        if len(calls) == 1:
            raise ZoneError("502")
        return {"elements": [_element("Lidl", 0.0, 0.0, "Lidl")]}

    monkeypatch.setattr(zone, "_get_json", fake)
    candidates = discover_drives(GeoPoint(label="x", latitude=0, longitude=0))
    assert calls == list(zone.DEFAULT_OVERPASS_MIRRORS[:2])
    assert len(candidates) == 1


def test_geo_api_env_override_is_used(monkeypatch) -> None:
    urls: list[str] = []

    def fake(url: str, **kwargs: object) -> list:
        urls.append(url)
        return [_commune("Montauroux", 43.59, 6.80, "83440", "83081")]

    monkeypatch.setattr(zone, "_get_json", fake)
    geocode("83440", environ={zone.GEO_API_ENV_VAR: "http://localhost:9/"})
    assert urls == [
        "http://localhost:9/communes?codePostal=83440&fields=nom,centre,code,codesPostaux"
    ]


def test_apply_selection_is_tri_state() -> None:
    candidates = [
        DriveCandidate(
            name="Leclerc",
            store="leclerc",
            address=None,
            city=None,
            postal_code=None,
            latitude=0,
            longitude=0,
            distance_km=1,
            collectable=True,
        ),
        DriveCandidate(
            name="Carrefour",
            store="carrefour",
            address=None,
            city=None,
            postal_code=None,
            latitude=0,
            longitude=0,
            distance_km=2,
            collectable=True,
        ),
        DriveCandidate(
            name="Lidl",
            store="lidl",
            address=None,
            city=None,
            postal_code=None,
            latitude=0,
            longitude=0,
            distance_km=3,
            collectable=False,
        ),
        DriveCandidate(
            name="Inconnu",
            store=None,
            address=None,
            city=None,
            postal_code=None,
            latitude=0,
            longitude=0,
            distance_km=4,
            collectable=False,
        ),
    ]
    household = HouseholdProfile(name="foyer", stores={"leclerc": True, "carrefour": False})
    result = apply_selection(candidates, household)
    assert [(c.selected, c.enabled) for c in result] == [
        (True, True),
        (False, False),
        (False, None),
        (False, None),
    ]
    assert apply_selection(candidates, None) == [
        DriveCandidate(**{**candidate.__dict__, "selected": False, "enabled": None})
        for candidate in candidates
    ]


def test_toggle_store_updates_only_targeted_store() -> None:
    household = HouseholdProfile(name="foyer", stores={"leclerc": True})
    updated = toggle_store(household, "Carrefour Market", enabled=False)
    assert updated.stores == {"leclerc": True, "carrefour": False}
    assert household.stores == {"leclerc": True}
    with pytest.raises(ZoneError):
        toggle_store(household, "   ", enabled=True)


def test_drive_zone_combines_geocode_discovery_and_selection(monkeypatch) -> None:
    point = GeoPoint(label="83440", latitude=43.59, longitude=6.80)
    monkeypatch.setattr(zone, "geocode", lambda query, **kwargs: point)
    monkeypatch.setattr(
        zone,
        "discover_drives",
        lambda location, **kwargs: [
            DriveCandidate(
                name="Leclerc",
                store="leclerc",
                address="Route de Grasse",
                city="Montauroux",
                postal_code="83440",
                latitude=43.60,
                longitude=6.80,
                distance_km=1.2,
                collectable=True,
            )
        ],
    )
    household = HouseholdProfile(name="foyer", stores={"leclerc": True})
    result = drive_zone("83440", household=household)
    payload = result.as_dict()
    assert payload["selected_stores"] == ["leclerc"]
    assert payload["enabled_stores"] == ["leclerc"]
    assert payload["candidates"][0]["distance_km"] == 1.2


def test_cli_zone_json_payload(monkeypatch, tmp_path: Path) -> None:
    _household(tmp_path, {"leclerc": True})
    zone_payload = zone.DriveZone(
        location=GeoPoint(label="83440", latitude=43.59, longitude=6.80),
        radius_km=20,
        candidates=[
            DriveCandidate(
                name="Leclerc",
                store="leclerc",
                address=None,
                city=None,
                postal_code=None,
                latitude=43.60,
                longitude=6.80,
                distance_km=1.2,
                collectable=True,
                selected=True,
                enabled=True,
            )
        ],
        enabled_stores=("leclerc",),
    )
    monkeypatch.setattr(cli, "drive_zone", lambda *a, **k: zone_payload)
    result = RUNNER.invoke(
        app, ["drive", "zone", "83440", "--data-dir", str(tmp_path), "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["household"] == "foyer"
    assert payload["selected_stores"] == ["leclerc"]


def test_cli_zone_enable_persists_in_household(monkeypatch, tmp_path: Path) -> None:
    _household(tmp_path)
    monkeypatch.setattr(
        cli,
        "drive_zone",
        lambda query, **kwargs: zone.DriveZone(
            location=GeoPoint(label="83440", latitude=43.59, longitude=6.80), radius_km=20
        ),
    )
    result = RUNNER.invoke(
        app,
        ["drive", "zone", "83440", "--enable", "leclerc", "--data-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert load_household(tmp_path, "foyer").stores == {"leclerc": True}


def test_cli_zone_enable_without_household_fails(monkeypatch, tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app, ["drive", "zone", "83440", "--enable", "leclerc", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert "aucun foyer actif" in result.output


def test_cli_zone_reports_unavailable_source(monkeypatch, tmp_path: Path) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise ZoneError("source indisponible")

    monkeypatch.setattr(cli, "drive_zone", boom)
    result = RUNNER.invoke(app, ["drive", "zone", "83440", "--data-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "source indisponible" in result.output


def _zone_with_candidates(**overrides: object) -> zone.DriveZone:
    payload: dict[str, object] = {
        "location": GeoPoint(label="83440", latitude=43.59, longitude=6.80),
        "radius_km": 20,
        "candidates": [
            DriveCandidate(
                name="Leclerc",
                store="leclerc",
                address=None,
                city=None,
                postal_code=None,
                latitude=43.60,
                longitude=6.80,
                distance_km=1.2,
                collectable=True,
                suggested=True,
            ),
            DriveCandidate(
                name="Carrefour",
                store="carrefour",
                address=None,
                city=None,
                postal_code=None,
                latitude=43.61,
                longitude=6.81,
                distance_km=3.4,
                collectable=True,
                suggested=True,
            ),
            DriveCandidate(
                name="Intermarché",
                store="intermarche",
                address=None,
                city=None,
                postal_code=None,
                latitude=43.62,
                longitude=6.82,
                distance_km=0.6,
                collectable=True,
                suggested=False,
            ),
        ],
        "suggested_stores": ("carrefour", "leclerc"),
    }
    payload.update(overrides)
    return zone.DriveZone(**payload)  # type: ignore[arg-type]


def test_zone_drives_uses_household_enabled_stores(monkeypatch, tmp_path: Path) -> None:
    _household(tmp_path, {"intermarche": True, "leclerc": False})
    monkeypatch.setattr(cli, "drive_zone", lambda *a, **k: _zone_with_candidates())
    assert cli._drives_for_zone(tmp_path, "83440", 20) == ["intermarche"]


def test_zone_drives_falls_back_to_suggestions(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "drive_zone", lambda *a, **k: _zone_with_candidates())
    assert cli._drives_for_zone(tmp_path, "83440", 20) == ["carrefour", "leclerc"]


def test_zone_drives_ignores_enabled_store_outside_radius(
    monkeypatch, tmp_path: Path
) -> None:
    _household(tmp_path, {"auchan": True})
    monkeypatch.setattr(cli, "drive_zone", lambda *a, **k: _zone_with_candidates())
    assert cli._drives_for_zone(tmp_path, "83440", 20) == ["carrefour", "leclerc"]


def test_cli_collect_zone_json(monkeypatch, tmp_path: Path) -> None:
    shopping = tmp_path / "courses.yaml"
    shopping.write_text("items:\n- name: lait\n  quantity: 2\n", encoding="utf-8")
    monkeypatch.setattr(cli, "drive_zone", lambda *a, **k: _zone_with_candidates())
    captured: dict[str, object] = {}

    def fake_collect(items, drives, **kwargs):
        captured["drives"] = drives
        return (
            [
                StoreOffer(store="leclerc", item="lait", product="Lait 1L", price=1.0),
                StoreOffer(store="carrefour", item="lait", product="Lait 1L", price=1.1),
            ],
            False,
            {"leclerc": {"source": "collected", "count": 1, "age_s": 0.0, "stale": False}},
        )

    monkeypatch.setattr(cli, "collect_offers_with_cache", fake_collect)
    result = RUNNER.invoke(
        app,
        [
            "drive",
            "collect",
            str(shopping),
            "--zone",
            "83440",
            "--data-dir",
            str(tmp_path),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert captured["drives"] == ["carrefour", "leclerc"]
    assert payload["zone"] == "83440"
    assert payload["drives"] == ["carrefour", "leclerc"]
    assert payload["count"] == 2
    assert payload["had_failure"] is False
    assert {offer["store"] for offer in payload["offers"]} == {"leclerc", "carrefour"}


def test_cli_collect_zone_without_drives_fails(monkeypatch, tmp_path: Path) -> None:
    shopping = tmp_path / "courses.yaml"
    shopping.write_text("items:\n- name: lait\n", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "drive_zone",
        lambda *a, **k: _zone_with_candidates(candidates=[], suggested_stores=()),
    )
    result = RUNNER.invoke(
        app, ["drive", "collect", str(shopping), "--zone", "83440", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "Aucun drive collectable" in result.output


def test_split_stores_accepts_comma_lists() -> None:
    assert cli._split_stores(["leclerc,carrefour", "super u"]) == [
        "leclerc",
        "carrefour",
        "super u",
    ]
    assert cli._split_stores(None) == []


def test_cli_select_accepts_comma_list(monkeypatch, tmp_path: Path) -> None:
    _household(tmp_path, {})
    result = RUNNER.invoke(
        app,
        [
            "drive",
            "select",
            "--enable",
            "leclerc,carrefour",
            "--disable",
            "auchan",
            "--data-dir",
            str(tmp_path),
            "--format",
            "json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["enabled_stores"] == ["carrefour", "leclerc"]
    assert payload["disabled_stores"] == ["auchan"]
