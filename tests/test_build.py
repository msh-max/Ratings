import json
import shutil

import pytest

from ranking.__main__ import main
from ranking.build import build, synthesise_why
from ranking.geocode import Geocoder
from ranking.load import latest_month, load_month
from ranking.schema import validate_extract, validate_output


@pytest.fixture
def repo(tmp_path, fixture_root):
    shutil.copytree(fixture_root, tmp_path / "repo")
    return tmp_path / "repo"


def _build(root):
    return build(root, "2026-11", geocoder=Geocoder(root / "data/geocode/cache.json", online=False),
                 generated_at="2026-11-10T00:00:00Z")


def test_fixture_extracts_are_valid(fixture_root):
    res = load_month(fixture_root, "2026-11")
    assert res.errors == [] and len(res.extracts) == 5


def test_extract_schema_strict_keys_lenient_nulls():
    ok = json.loads((__import__("pathlib").Path(__file__).parent /
                     "fixtures/repo/data/sources/2026-11/mena-50-best-2026.json").read_text())
    assert validate_extract(ok) == []
    missing = dict(ok); del missing["published_at"]
    assert any("published_at" in e for e in validate_extract(missing))
    nulls = dict(ok, published_at=None, url=None, language=None, coverage_note=None)
    assert validate_extract(nulls) == []
    assert validate_extract(dict(ok, source_type="blog"))
    assert validate_extract(dict(ok, source_id="Not Kebab"))
    no_name = dict(ok, entries=[{"tier": "listed"}])
    assert validate_extract(no_name)


def test_build_output(repo):
    res = _build(repo)
    assert res.errors == []
    out = res.output
    assert validate_output(out) == []
    assert out["month"] == "2026-11" and out["n_sources"] == 5
    places = out["places"]
    assert [p["rank"] for p in places] == list(range(1, len(places) + 1))
    assert [p["name"] for p in places][:3] == ["Sample Bistro", "El Sample House", "Sample Grill"]
    top = places[0]
    # Michelin 1 star + Green Star 11, MENA #5 9, Time Out award 6 + Time Out list (#1 x0.4) 1.6
    assert top["score"] == 27.6 and top["confidence"] == "strong"
    assert (top["lat"], top["lng"], top["geocode"]) == (24.7612, 46.6401, "nominatim")
    assert top["why"] == "Seasonal tasting menu built on local produce. Best fine dining."
    assert "MICHELIN 1 star, Green Star (2026)" in top["awards"]
    house = places[1]
    assert house["name_ar"] == "بيت العينة" and "Al-Sample House" in house["aliases"]
    grill = places[2]
    assert grill["branches"] == ["Hittin", "Olaya"] and grill["lat"] is None   # cache hit rejected
    assert grill["why"].startswith("Listed by MICHELIN Guide (Selected)")
    scores = [p["score"] for p in places]
    assert scores == sorted(scores, reverse=True)


def test_build_is_deterministic(repo):
    a = json.dumps(_build(repo).output, sort_keys=False)
    b = json.dumps(_build(repo).output, sort_keys=False)
    assert a == b


def test_why_never_adds_text():
    counted = [{"note": "  grills over  charcoal ", "_publisher_name": "X", "tier": "listed", "position": None},
               {"note": "Grills over charcoal.", "_publisher_name": "Y", "tier": "listed", "position": None}]
    assert synthesise_why(counted) == "Grills over charcoal."


def test_cli_build_writes_outputs(repo, capsys):
    assert main(["--root", str(repo), "build", "--month", "2026-11", "--check", "--no-geocode"]) == 0
    assert not (repo / "data/rankings").exists()
    assert main(["--root", str(repo), "build", "--month", "2026-11", "--no-geocode"]) == 0
    month = json.loads((repo / "data/rankings/restaurants/2026-11.json").read_text())
    latest = json.loads((repo / "data/rankings/restaurants/latest.json").read_text())
    site = json.loads((repo / "docs/data/restaurants.json").read_text())
    assert month == latest
    assert {k: v for k, v in site.items() if k not in ("places", "site_format")} == \
        {k: v for k, v in month.items() if k != "places"}
    assert [p["id"] for p in site["places"]] == [p["id"] for p in month["places"]]
    assert set(site["places"][0]["mentions"][0]) <= {"source_id", "tier", "position", "weight", "url"}
    assert "name_ar" not in site["places"][0] and "name_ar" in month["places"][0]   # nulls omitted
    assert latest_month(repo) == "2026-11"
    assert main(["--root", str(repo), "qa", "--month", "2026-11"]) == 0
    out = capsys.readouterr().out
    assert "Non-Riyadh suspects: 1" in out and "Sample Cafe 9" in out


def test_cli_fails_on_invalid_extract(repo):
    bad = repo / "data/sources/2026-11/broken.json"
    bad.write_text('{"source_id": "broken"}')
    assert main(["--root", str(repo), "build", "--month", "2026-11", "--check", "--no-geocode"]) == 1


def test_cli_fails_when_no_extracts(tmp_path):
    assert main(["--root", str(tmp_path), "build", "--month", "2026-11", "--check", "--no-geocode"]) == 1
