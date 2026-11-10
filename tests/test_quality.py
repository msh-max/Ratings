"""Exclusion rule, blurb selector, awards line and the single-file artifact generator."""
import json
import re
import shutil
from pathlib import Path

import pytest

from ranking.__main__ import main
from ranking.artifact import page_markup, render_artifact
from ranking.build import _awards, build, clean_note, select_notes, synthesise_why
from ranking.exclusions import cafe_suspects, load_exclusions
from ranking.geocode import Geocoder
from ranking.normalize import Aliases, cluster_mentions, load_aliases, place_attributes

REPO = Path(__file__).resolve().parents[1]


def _m(note, source_type="editorial", weight=3.0, **kw):
    return {"note": note, "source_type": source_type, "weight": weight, "_publisher_name": "P",
            "tier": "listed", "position": None, "source_id": kw.pop("source_id", "s"), **kw}


# ---------------------------------------------------------------- exclusions

def _exclusions(tmp_path, text, aliases=None):
    f = tmp_path / "exclusions.yaml"
    f.write_text(text, encoding="utf-8")
    return load_exclusions(f, aliases)


def test_exclusion_matches_any_spelling_and_aliases(tmp_path):
    a = tmp_path / "aliases.yaml"
    a.write_text("Half Million:\n  - ½ Million\n", encoding="utf-8")
    aliases = load_aliases(a)
    ex = _exclusions(tmp_path, "Half Million: coffee chain\n'% Arabica': coffee\n_keep:\n  - Urth Caffé\n", aliases)
    assert ex.reason_for(["½ Million"], aliases) == "coffee chain"
    assert ex.reason_for(["HALF-MILLION"], aliases) == "coffee chain"
    assert ex.reason_for(["% Arabica"], aliases) == "coffee"
    assert ex.reason_for(["Urth Caffé"], aliases) is None
    assert ex.reviewed(["Urth Caffe"], aliases)


def test_build_moves_excluded_places_out_of_the_ranking(tmp_path, fixture_root):
    repo = tmp_path / "repo"
    shutil.copytree(fixture_root, repo)
    plain = build(repo, "2026-11", geocoder=Geocoder(repo / "nope.json", online=False),
                  exclusions=_exclusions(tmp_path, "{}\n")).output
    victim = plain["places"][1]
    ex = _exclusions(tmp_path, f"'{victim['name']}': bakery\n")
    out = build(repo, "2026-11", geocoder=Geocoder(repo / "nope.json", online=False), exclusions=ex).output
    assert victim["name"] not in [p["name"] for p in out["places"]]
    assert out["n_places"] == plain["n_places"] - 1 == len(out["places"])
    assert [p["rank"] for p in out["places"]] == list(range(1, len(out["places"]) + 1))
    assert out["n_excluded"] == 1
    gone = out["excluded"][0]
    assert gone["name"] == victim["name"] and gone["reason"] == "bakery" and "rank" not in gone
    assert gone["mentions"] and gone["score"] == victim["score"]


def test_site_copy_never_contains_excluded(tmp_path, fixture_root):
    from ranking.build import site_copy
    out = {"sources": [], "places": [], "excluded": [{"name": "X"}], "n_excluded": 1}
    site = site_copy(out)
    assert "excluded" not in site and "n_excluded" not in site


def test_repo_exclusions_cover_coffee_chains_but_keep_cafe_restaurants():
    aliases = load_aliases(REPO / "ranking" / "aliases.yaml")
    ex = load_exclusions(REPO / "ranking" / "exclusions.yaml", aliases)
    for name in ("Half Million", "½ Million", "% Arabica", "Elixir Bunn", "Camel Step", "Brew92"):
        assert ex.reason_for([name], aliases), name
    for name in ("Urth Caffé", "Em Sherif Café", "Café Boulud", "tashas"):
        assert ex.reason_for([name], aliases) is None, name


def test_cafe_suspects_flags_only_unreviewed_names(tmp_path):
    ex = _exclusions(tmp_path, "Barns: coffee\n_keep:\n  - Urth Caffé\n")
    places = [{"name": n, "aliases": [], "cuisines": c, "mentions": []} for n, c in
              [("Urth Caffé", ["Cafe"]), ("Barns", ["Café"]), ("New Roastery", []), ("Grill X", ["Grill"])]]
    assert [n for n, _ in cafe_suspects(places, ex)] == ["New Roastery"]


# ---------------------------------------------------------------- blurbs

def test_clean_note_drops_codes_scores_and_award_boilerplate():
    assert clean_note("11.5/20; Gourmet Restaurant; budget SAR 85 to SAR 175") == ""
    assert clean_note("Won: Best Cafe. (Best = critics/panel choice; Favourite = public vote.)") == ""
    assert clean_note("MICHELIN Selected in the inaugural Saudi selection; Lebanese.") == ""
    assert clean_note("#13 in the $ group; coffee chain") == ""
    assert clean_note("listed in Visit Saudi's Bel'afya dining guide") == ""
    assert clean_note("12.5/20; Gourmet Restaurant; chef Pepino Mammoliti") == ""
    assert clean_note("Fine dining") == ""                         # too thin to describe anything
    assert clean_note("14/20; Chef's Restaurant; tasting menu of Najdi dishes") == "Tasting menu of Najdi dishes."
    assert clean_note("  robata grill and sushi counter ") == "Robata grill and sushi counter."


def test_select_notes_prefers_guide_and_editorial_prose():
    counted = [
        _m("Winner: Favourite Japanese Restaurant.", "award", 6),
        _m("listed in Visit Saudi's dining guide", "official", 2),
        _m("Hearty local plates served on floor seating", "community", 1),
        _m("Robata grill and sushi counter with a terrace", "editorial", 3),
    ]
    assert select_notes(counted) == ["Robata grill and sushi counter with a terrace.",
                                     "Hearty local plates served on floor seating."]
    near_dupe = counted + [_m("robata grill and sushi counter with terrace", "guide", 5)]
    assert len(select_notes(near_dupe)) == 2 and "Hearty" in select_notes(near_dupe)[1]


def test_why_caps_length_and_ends_on_a_sentence():
    long1 = "A " + "very " * 50 + "long first note about the place"
    why = synthesise_why([_m(long1), _m("Second note that would not fit anyway")])
    assert len(why) <= 220 and why.endswith(".")
    two = synthesise_why([_m("First sentence about the room. Second sentence about the grill"),
                          _m("Another source says something else entirely here")])
    assert len(two) <= 220 and two.endswith(".")
    sentences = synthesise_why([_m("One. " * 30 + "Tail")])
    assert sentences.endswith(".") and len(sentences) <= 220


def test_why_never_adds_words_beyond_the_notes():
    counted = [_m("Kobe beef with Silk Road flavours", "editorial"),
               _m("12/20; Gourmet Restaurant", "guide", 5)]
    why = synthesise_why(counted)
    words = set(re.findall(r"\w+", why.lower()))
    source_words = set(re.findall(r"\w+", " ".join(m["note"] for m in counted).lower()))
    assert words <= source_words


def test_why_falls_back_to_listing_facts():
    why = synthesise_why([_m("Won: Best Steakhouse.", "award", 6, _publisher_name="FACT", tier="winner")])
    assert why == "Listed by FACT (winner)."


# ---------------------------------------------------------------- awards & branches

def test_awards_listed_once_with_years(registry):
    def aw(sid, year, tier="nominee"):
        return {"source_type": "award", "publisher": "World Culinary Awards", "tier": tier,
                "position": None, "published_at": f"{year}-09-01", "source_id": sid}
    counted = [aw("wca-2026", 2026), aw("wca-2024", 2024), aw("wca-2025", 2025)]
    out = _awards(counted, registry)
    assert len(out) == 1 and out[0].endswith("nominee (2024, 2025, 2026)")
    undated = [{"source_type": "award", "publisher": "FACT", "tier": "nominee", "position": None,
                "published_at": None, "source_id": "fact-dining-awards-riyadh-2026-nominees"}]
    assert _awards(undated, registry)[0].endswith("nominee (2026)")


def test_branch_that_only_repeats_the_city_is_dropped():
    ms = [{"name": "X Grill", "branch": "Riyadh"}, {"name": "X Grill", "branch": "Olaya"},
          {"name": "X Grill", "branch": "الرياض"}]
    assert place_attributes(ms)["branches"] == ["Olaya"]


def test_spacing_variants_merge_and_alias_name_wins(tmp_path):
    ms = [{"name": "SushiSamba"}, {"name": "Sushi Samba"}, {"name": "A.O.K Kitchen"}, {"name": "AOK Kitchen"}]
    assert len(cluster_mentions(ms)) == 2
    f = tmp_path / "aliases.yaml"
    f.write_text("Lucca:\n  - Lucca Steakhouse\n", encoding="utf-8")
    aliases = load_aliases(f)
    cluster = [{"name": "Lucca Steakhouse"}, {"name": "Lucca Steakhouse"}, {"name": "Lucca"}]
    assert place_attributes(cluster, aliases)["name"] == "Lucca"
    assert place_attributes(cluster, Aliases())["name"] == "Lucca Steakhouse"


# ---------------------------------------------------------------- artifact

@pytest.fixture
def built_repo(tmp_path, fixture_root):
    repo = tmp_path / "repo"
    shutil.copytree(fixture_root, repo)
    shutil.copytree(REPO / "docs", repo / "docs", dirs_exist_ok=True)
    assert main(["--root", str(repo), "build", "--month", "2026-11", "--no-geocode"]) == 0
    return repo


def test_artifact_is_one_fragment_with_inline_data(built_repo):
    assert main(["--root", str(built_repo), "artifact", "--month", "2026-11", "--out", "build/a.html"]) == 0
    html = (built_repo / "build/a.html").read_text(encoding="utf-8")
    assert html.startswith("<title>Riyadh Best Restaurants</title>\n"
                           '<meta name="claude:viewport-fit-sides" content="cover">')
    for tag in ("<!doctype", "<html", "<head", "<body", "</head", "</body", "</html"):
        assert not re.search(re.escape(tag) + r"[\s>]", html, re.I), tag
    assert html.count("<script") == 1 and 'src="app.js"' not in html and 'href="styles.css"' not in html
    assert html.index("<style>") < html.index('id="cards"') < html.index("window.RANKING_DATA =")
    assert html.index("window.RANKING_DATA =") < html.index('"use strict"')
    data = json.loads(re.search(r"window\.RANKING_DATA = (\{.*?\});\n", html, re.S).group(1)
                      .replace("<\\/", "</"))
    month = json.loads((built_repo / "data/rankings/restaurants/2026-11.json").read_text())
    assert [p["id"] for p in data["places"]] == [p["id"] for p in month["places"]]
    assert "excluded" not in data
    assert "Map view is available on the" in html and "https://msh-max.github.io/Ratings/" in html
    css = html[html.index("<style>"):html.index("</style>")]
    assert '@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"])' in css
    assert ':root[data-theme="dark"]' in css and "background: var(--bg)" in css


def test_artifact_escapes_script_breakers(tmp_path):
    out = {"month": "2026-11", "sources": [], "places": [
        {"id": "x", "name": "</script><script>alert(1)</script>", "mentions": []}]}
    html = render_artifact(out, REPO / "docs")
    assert html.count("</script>") == 1 and "<\\/script>" in html


def test_artifact_needs_a_build_first(tmp_path, capsys):
    assert main(["--root", str(tmp_path), "artifact", "--month", "2026-11"]) == 1
    assert "run `python -m ranking build" in capsys.readouterr().err


def test_page_markup_takes_the_body_without_the_script_tag():
    body = page_markup((REPO / "docs/index.html").read_text(encoding="utf-8"))
    assert body.startswith('<a class="skip"') and "<script" not in body and 'id="map-off"' in body
