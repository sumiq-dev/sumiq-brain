import pytest

from aibrain import markdown as md


def test_parse_and_render_roundtrip():
    text = "---\nid: X\ntitle: T\ncreated: 2026-11-01\n---\n\n# T\n\nBody [[a]] and [[b|B]].\n"
    doc = md.parse(text)
    assert doc.meta["created"] == "2026-11-01"
    assert doc.body.startswith("# T")
    assert md.render(doc) == text


def test_no_frontmatter():
    doc = md.parse("# Just text\n")
    assert doc.meta == {} and doc.body == "# Just text\n"


def test_unclosed_frontmatter():
    with pytest.raises(md.FormatError):
        md.parse("---\ntitle: x\n# body")


def test_links():
    links = md.find_links("see [[gacha-rates]], [[projects/x/index|X]] and [[combat#Damage]]")
    assert [(lk.target, lk.heading, lk.alias) for lk in links] == [
        ("gacha-rates", None, None), ("projects/x/index", None, "X"), ("combat", "Damage", None)]
    assert [lk.target for lk in md.meta_links({"related": ["[[a]]"], "summary": "[[no]]"})] == ["a"]


def test_replace_section_keeps_other_sections():
    body = "# T\n\nintro\n\n## Children\n- old\n\n## See also\n- keep\n"
    out = md.replace_section(body, "Children", "- new")
    assert "- new" in out and "- old" not in out and "## See also\n- keep" in out


def test_replace_section_appends_when_missing():
    assert md.replace_section("# T\n", "Children", "- a").endswith("## Children\n- a\n")


def test_get_section_and_human_blocks():
    body = "# T\n\n## A\none\n### A1\nsub\n## B\ntwo\n<!-- human -->mine<!-- /human -->"
    assert md.get_section(body, "a") == "## A\none\n### A1\nsub"
    assert md.human_blocks(body) == ["<!-- human -->mine<!-- /human -->"]
    start = body.index("mine")
    assert md.overlaps_human_block(body, start, start + 2)
    assert not md.overlaps_human_block(body, 0, 3)


def test_slugify():
    assert md.slugify("Game Design Doc v3 (final)!") == "game-design-doc-v3-final"
