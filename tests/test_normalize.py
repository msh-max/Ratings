from ranking.normalize import (Aliases, canonical_key, cluster_mentions, load_aliases,
                               normalise_price, pick_names, place_attributes, split_cuisines)


def names(clusters):
    return [sorted(m["name"] for m in c) for c in clusters]


def test_canonical_key_basics():
    assert canonical_key("The Sample Restaurant, Riyadh") == "sample"
    assert canonical_key("Café Bateel") == "cafe bateel"
    assert canonical_key("Lusin's") == "lusins"
    assert canonical_key("Sky Lounge") == "sky lounge"   # "lounge" is kept
    assert canonical_key("Fish & Chips") == "fish and chips"
    assert canonical_key("Restaurant") == "restaurant"   # never empty


def test_al_el_variants_share_a_key():
    keys = {canonical_key(n) for n in ["Al-Romansiah", "Al Romansiah", "El Romansiah",
                                       "AlRomansiah", "al‐romansiah", "EL-ROMANSIAH Restaurant"]}
    assert keys == {"alromansiah"}


def test_arabic_article_and_letter_folding():
    assert canonical_key("مطعم الرومانسية") == canonical_key("الرومانسيه")
    assert canonical_key("أبو ظبي") == canonical_key("ابو ظبي")


def test_exact_and_fuzzy_merge():
    ms = [{"name": "Sample Bistro"}, {"name": "Sample Bistro Restaurant"},
          {"name": "The Sample Bistro"}, {"name": "Totally Other"}]
    assert names(cluster_mentions(ms)) == [
        ["Sample Bistro", "Sample Bistro Restaurant", "The Sample Bistro"], ["Totally Other"]]


def test_fuzzy_threshold_keeps_different_places_apart():
    ms = [{"name": "Sample Kitchen"}, {"name": "Simple Kitchen House"}]
    assert len(cluster_mentions(ms)) == 2


def test_al_el_mentions_merge():
    ms = [{"name": "Al-Sample House"}, {"name": "El Sample House"}, {"name": "Alsample House"}]
    assert len(cluster_mentions(ms)) == 1


def test_arabic_latin_merge_via_name_ar():
    ms = [{"name": "Sample House", "name_ar": "بيت العينة"},
          {"name": "بيت العينة", "name_ar": "بيت العينة"}]
    clusters = cluster_mentions(ms)
    assert len(clusters) == 1
    name, name_ar, aliases = pick_names(clusters[0])
    assert name == "Sample House" and name_ar == "بيت العينة" and aliases == []


def test_arabic_latin_merge_via_aliases_file(tmp_path):
    f = tmp_path / "aliases.yaml"
    f.write_text("Al Sample Romance:\n  - الرومانسية للعينة\n  - Romance Sample Co\n", encoding="utf-8")
    aliases = load_aliases(f)
    ms = [{"name": "Al-Sample Romance"}, {"name": "الرومانسية للعينة"}, {"name": "Romance Sample Co"}]
    assert len(cluster_mentions(ms, aliases)) == 1
    assert len(cluster_mentions(ms, Aliases())) == 3


def test_distinct_pairs_block_fuzzy_merge(tmp_path):
    f = tmp_path / "aliases.yaml"
    f.write_text("_distinct:\n  - [Sample Sherif, Sample Sherif Cafe]\n", encoding="utf-8")
    ms = [{"name": "Sample Sherif"}, {"name": "Sample Sherif Cafe"}]
    assert len(cluster_mentions(ms)) == 1           # token_set_ratio = 100 merges by default
    assert len(cluster_mentions(ms, load_aliases(f))) == 2


def test_empty_aliases_file_from_repo():
    from pathlib import Path
    import ranking
    aliases = load_aliases(Path(ranking.__file__).with_name("aliases.yaml"))
    assert aliases.key_map == {}


def test_place_attributes_and_branches():
    ms = [{"name": "Sample Grill", "cuisine": "Grill", "area": "Olaya", "price_band": "$$$", "branch": None},
          {"name": "Sample Grill Riyadh", "cuisine": "Steakhouse / grill", "area": "Olaya",
           "price_band": " $$$ ", "branch": "Hittin"},
          {"name": "Sample Grill", "cuisine": None, "area": "Hittin", "price_band": None, "branch": "Olaya"}]
    a = place_attributes(ms)
    assert a["name"] == "Sample Grill"
    assert a["aliases"] == ["Sample Grill Riyadh"]
    assert a["cuisines"] == ["Grill", "Steakhouse"]
    assert a["area"] == "Olaya"
    assert a["price_band"] == "$$$"
    assert a["branches"] == ["Hittin", "Olaya"]


def test_price_and_cuisine_helpers():
    assert normalise_price("$$$$$") == "$$$$"
    assert normalise_price("SAR 200-300") == "SAR 200-300"
    assert normalise_price("  ") is None
    assert split_cuisines(["Japanese, Sushi", "japanese"]) == ["Japanese", "Sushi"]


def test_generic_short_name_does_not_chain_merge():
    ms = [{"name": "Villa Mamas"}, {"name": "The Villa Restaurant"}, {"name": "Villa Fayrouz"}]
    assert len(cluster_mentions(ms)) == 3


def test_subset_name_merges_when_unambiguous():
    ms = [{"name": "Lusin"}, {"name": "Lusin Armenian"}, {"name": "Myazu"}]
    assert names(cluster_mentions(ms)) == [["Lusin", "Lusin Armenian"], ["Myazu"]]


def test_subset_name_ambiguous_between_two_places():
    ms = [{"name": "Sample Nour Kitchen"}, {"name": "Sample Nour Bakery"}, {"name": "Sample Nour"}]
    # "sample nour" is contained in both longer names, which are different places
    assert len(cluster_mentions(ms)) == 3


def test_x_by_y_joins_x_not_y():
    ms = [{"name": "Kayzample"}, {"name": "Myazample"}, {"name": "Kayzample by Myazample"},
          {"name": "Spample by Wolfgang Sample"}, {"name": "Spample"}]
    assert names(cluster_mentions(ms)) == [
        ["Kayzample", "Kayzample by Myazample"], ["Myazample"],
        ["Spample", "Spample by Wolfgang Sample"]]


def test_parenthetical_alternate_name_merges():
    ms = [{"name": "La Sample Maison (LSM) Riyadh"}, {"name": "LSM Riyadh"},
          {"name": "La Sample Maison"}]
    assert len(cluster_mentions(ms)) == 1


def test_hotel_suffix_is_ignored():
    assert canonical_key("Cucina - Riyadh Marriott Hotel") == canonical_key("Cucina")
    assert canonical_key("Sample Bar - The St. Regis Riyadh") == "sample bar"
    assert canonical_key("Ruby Room - Hocho") == "ruby room hocho"
    ms = [{"name": "Sample Brasserie - Mandarin Oriental Al Faisaliah"}, {"name": "Sample Brasserie"}]
    assert len(cluster_mentions(ms)) == 1


def test_different_numbers_never_merge():
    ms = [{"name": "Sample Restaurant 1"}, {"name": "Sample Restaurant 12"}, {"name": "Sample 1"}]
    assert names(cluster_mentions(ms)) == [["Sample 1", "Sample Restaurant 1"], ["Sample Restaurant 12"]]
