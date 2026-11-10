import pytest

from ranking.scoring import Tier, confidence, months_between, parse_tier, score_place


@pytest.mark.parametrize("text,position,expected", [
    ("3 stars", None, Tier("stars", 3)),
    ("1 star", None, Tier("stars", 1)),
    ("One MICHELIN Star", None, Tier("stars", 1)),
    ("Two Stars", None, Tier("stars", 2)),
    ("Bib Gourmand", None, Tier("bib")),
    ("Selected", None, Tier("listed")),
    ("Green Star", None, Tier("listed", green=True)),
    ("1 star, Green Star", None, Tier("stars", 1, green=True)),
    ("3 toques", None, Tier("toques", 3)),
    ("1 toque", None, Tier("toques", 1)),
    ("#12", 12, Tier("rank", 12)),
    ("# 7", None, Tier("rank", 7)),
    ("No. 4", None, Tier("rank", 4)),
    ("12th", None, Tier("rank", 12)),
    ("listed", 3, Tier("rank", 3)),
    ("recommended by 4 commenters", 4, Tier("recommended", 4)),
    ("Recommended by 1 commenter", None, Tier("recommended", 1)),
    ("winner", None, Tier("winner")),
    ("Winner – Best Fine Dining", None, Tier("winner")),
    ("Highly Commended", None, Tier("finalist")),
    ("finalist", None, Tier("finalist")),
    ("Nominee", None, Tier("finalist")),
    ("Special award: Best Restaurant in Saudi Arabia", None, Tier("special")),
    ("listed", None, Tier("listed")),
    ("creator top list", None, Tier("creator")),
    (None, None, Tier("listed")),
    ("", "5", Tier("rank", 5)),
])
def test_parse_tier(text, position, expected):
    assert parse_tier(text, position) == expected


def _w(registry, publisher, source_type, tier, position=None):
    scheme = registry.scheme_for(publisher, source_type)
    return registry.base_weight(parse_tier(tier, position), scheme)


@pytest.mark.parametrize("publisher,stype,tier,pos,weight", [
    ("MICHELIN Guide", "guide", "3 stars", None, 14),
    ("Michelin", "guide", "2 stars", None, 12),
    ("MICHELIN Guide", "guide", "1 star", None, 10),
    ("MICHELIN Guide", "guide", "Bib Gourmand", None, 7),
    ("MICHELIN Guide", "guide", "Selected", None, 5),
    ("MICHELIN Guide", "guide", "1 star, Green Star", None, 11),
    ("Gault&Millau", "guide", "4 toques", None, 8),
    ("Gault & Millau KSA", "guide", "3 toques", None, 7),
    ("Gault&Millau", "guide", "2 toques", None, 6),
    ("Gault&Millau", "guide", "1 toque", None, 5),
    ("Gault&Millau", "guide", "listed", None, 4),
    ("La Liste", "ranking", "92.5 points", None, 5),
    ("MENA's 50 Best Restaurants", "ranking", "#3", 3, 9),
    ("MENA's 50 Best Restaurants", "ranking", "#11", 11, 8),
    ("MENA's 50 Best Restaurants", "ranking", "#50", 50, 8),
    ("MENA's 50 Best Restaurants", "ranking", "#77", 77, 5),
    ("MENA's 50 Best Restaurants", "award", "Special award: Art of Hospitality", None, 6),
    ("FACT Dining Awards", "award", "winner", None, 6),
    ("World Culinary Awards", "award", "nominee", None, 3),
    ("Time Out Riyadh", "award", "Highly commended", None, 3),
    ("Time Out Riyadh", "editorial", "#2", 2, 4),
    ("Time Out Riyadh", "editorial", "#4", 4, 3),
    ("Time Out Riyadh", "editorial", "listed", None, 3),
    ("Visit Saudi", "official", "listed", None, 2),
    ("Reddit", "community", "recommended by 1 commenters", 1, 1),
    ("Reddit", "community", "recommended by 2 commenters", 2, 1.5),
    ("Reddit", "community", "recommended by 5 commenters", 5, 2),
    ("Reddit", "community", "listed", 7, 2),
    ("YouTube - Some Channel", "community", "#1", 1, 1.5),
    ("Some Food Blog", "community", "creator top list", None, 1.5),
    # unknown publishers fall back to type defaults
    ("Some New Blog", "editorial", "#1", 1, 4),
    ("Unknown Awards Org", "award", "winner", None, 6),
    ("Unknown Guide", "guide", "listed", None, 4),
    ("Unknown City Portal", "official", "listed", None, 2),
])
def test_base_weights(registry, publisher, stype, tier, pos, weight):
    assert _w(registry, publisher, stype, tier, pos) == weight


@pytest.mark.parametrize("published,months,mult", [
    ("2026-11-01", 0, 1.0),
    ("2025-11-30", 12, 1.0),
    ("2025-10-01", 13, 0.7),
    ("2024-11-01", 24, 0.7),
    ("2024-10-01", 25, 0.4),
    ("2023-11-01", 36, 0.4),
    ("2023-10-01", 37, 0.2),
    ("2027-01-01", 0, 1.0),
    ("2026", 5, 1.0),
    (None, None, 0.8),
])
def test_recency(registry, published, months, mult):
    assert months_between(published, "2026-11") == months
    assert registry.recency(published, "2026-11") == mult


def _m(source_id, publisher, weight, published_at="2026-01-01"):
    return {"source_id": source_id, "publisher": publisher, "source_type": "editorial",
            "tier": "listed", "position": None, "published_at": published_at, "weight": weight}


def test_source_counts_once_max_tier(registry):
    score, counted, n_pub = score_place([_m("a", "Time Out", 3), _m("a", "Time Out", 4)], registry)
    assert score == 4 and len(counted) == 1 and n_pub == 1


def test_publisher_cap(registry):
    # three Time Out lists: 4 + 3 + 3 = 10, capped at 1.5 x 4 = 6
    ms = [_m("to-1", "Time Out Riyadh", 4), _m("to-2", "Time Out", 3), _m("to-3", "TimeOut Riyadh", 3)]
    score, counted, n_pub = score_place(ms, registry)
    assert score == 6.0 and n_pub == 1 and len(counted) == 3
    # a second publisher is not capped together with the first
    score2, _, n_pub2 = score_place(ms + [_m("cn", "Condé Nast Traveller", 3)], registry)
    assert score2 == 9.0 and n_pub2 == 2


def test_cap_not_binding(registry):
    score, _, _ = score_place([_m("m1", "MICHELIN Guide", 10), _m("m2", "Michelin", 2.5)], registry)
    assert score == 12.5


def test_youtube_channels_are_separate_publishers(registry):
    ms = [_m("yt-a", "YouTube - Channel A", 1.5), _m("yt-b", "YouTube - Channel B", 1.5)]
    score, _, n_pub = score_place(ms, registry)
    assert n_pub == 2 and score == 3.0


def test_confidence():
    assert confidence(3) == "strong"
    assert confidence(2) == "medium"
    assert confidence(1) == "single"
