import pytest

from aibrain import markdown as md
from aibrain.vault import Vault, VaultError

from conftest import git_log

GDD = "projects/kings-raid/gdd.md"


@pytest.fixture
def project(run):
    run("create_folder", path="projects/kings-raid", title="Kings Raid clone",
        summary="Gacha RPG project: combat, gacha, economy.")
    return "projects/kings-raid"


def test_init_layout(vault):
    for folder in vault.config.top_level:
        assert (vault.root / folder / "index.md").is_file()
    root = vault.read_text("index.md")
    assert root.index("[[projects/index|Projects]]") < root.index("[[inbox/index|Inbox]]")
    assert vault.check() == []


def test_top_level_is_fixed(run):
    with pytest.raises(VaultError, match="top-level folders are fixed"):
        run("create_folder", path="recipes", title="Recipes", summary="Food.")
    with pytest.raises(VaultError, match="not a top-level folder"):
        run("create_folder", path="recipes/pasta", title="Pasta", summary="Food.")
    with pytest.raises(VaultError, match="vault root"):
        run("write_note", path="loose.md", title="Loose", summary="Nope.", body="x")


def test_create_folder_updates_parent_index(vault, project):
    assert "- [[projects/kings-raid/index|Kings Raid clone]] — Gacha RPG project" in vault.read_text("projects/index.md")
    with pytest.raises(VaultError, match="parent folder"):
        vault.create_folder("projects/a/b", "B", "B.")


def test_write_note_and_index(vault, run, project):
    out = run("write_note", path="projects/kings-raid/gacha-rates", title="Gacha rates",
              summary="Pull rates and pity at 90.", body="Rates are 3% SSR.", tags=["#design"],
              related=["combat"])
    assert "saved projects/kings-raid/gacha-rates.md; folder index updated" in out
    assert "links to notes that don't exist yet: combat" in out
    doc = vault.load("projects/kings-raid/gacha-rates.md")
    assert doc.meta["type"] == "note" and doc.meta["author"] == "agent:test"
    assert doc.meta["tags"] == ["design"] and doc.meta["related"] == ["[[combat]]"]
    assert doc.body.startswith("# Gacha rates")
    index = vault.read_text("projects/kings-raid/index.md")
    assert "- [[gacha-rates]] — Pull rates and pity at 90." in index
    assert "created: 2026" in vault.read_text("projects/kings-raid/gacha-rates.md")  # unquoted date


def test_rewrite_keeps_id_and_created(vault, run, project):
    run("write_note", path=GDD, title="GDD", summary="Design doc.", body="v1")
    first = vault.load(GDD).meta
    run("write_note", path=GDD, title="GDD", summary="Design doc v2.", body="v2")
    second = vault.load(GDD).meta
    assert first["id"] == second["id"] and first["created"] == second["created"]
    assert "Design doc v2." in vault.read_text("projects/kings-raid/index.md")


def test_names_are_unique(run, project):
    run("write_note", path=GDD, title="GDD", summary="Design doc.", body="x")
    with pytest.raises(VaultError, match="already used"):
        run("write_note", path="topics/gdd.md", title="GDD", summary="Other.", body="x")


@pytest.mark.parametrize("path,error", [
    ("projects/kings-raid/Bad Name.md", "kebab-case"),
    ("projects/kings-raid/index.md", "index notes"),
    ("projects/missing/x.md", "does not exist"),
    ("../escape.md", r"'\.\.'"),
    (".brain/x.md", "off limits"),
])
def test_write_note_rejects(run, project, path, error):
    with pytest.raises(VaultError, match=error):
        run("write_note", path=path, title="T", summary="S.", body="b")


def test_summary_rules(run, project):
    with pytest.raises(VaultError, match="limit is 25"):
        run("write_note", path=GDD, title="T", summary=" ".join(["word"] * 26), body="b")
    with pytest.raises(VaultError, match="required"):
        run("write_note", path=GDD, title="T", summary=" ", body="b")


def test_failed_write_rolls_back(vault, project):
    before = vault.read_text("projects/kings-raid/index.md")
    with pytest.raises(RuntimeError):
        with vault.transaction("agent:test", "boom"):
            vault.write_note(GDD, "GDD", "Design doc.", "x")
            raise RuntimeError("boom")
    assert not (vault.root / GDD).exists()
    assert vault.read_text("projects/kings-raid/index.md") == before
    assert vault.resolve_link("gdd") is None


def test_changes_are_committed_and_logged(vault, run, project):
    run("write_note", path=GDD, title="GDD", summary="Design doc.", body="x")
    log = git_log(vault)
    assert log[0] == f"agent:test|agent:test: write {GDD}"
    assert vault.recent(1)[0]["message"] == f"write {GDD}"
    assert GDD in vault.recent(1)[0]["paths"]


def test_edit_note(vault, run, project):
    run("write_note", path=GDD, title="GDD", summary="Design doc.", body="Pity at 80.\n\nMore.")
    run("edit_note", name="gdd", find="Pity at 80.", replace="Pity at 90.", summary="Design doc, pity 90.")
    run("edit_note", name="gdd", append="- 2026-11-01: banner rules added")
    doc = vault.load(GDD)
    assert "Pity at 90." in doc.body and doc.body.rstrip().endswith("banner rules added")
    assert "pity 90." in vault.read_text("projects/kings-raid/index.md")
    with pytest.raises(VaultError, match="not found"):
        run("edit_note", name="gdd", find="nope", replace="x")
    with pytest.raises(VaultError, match="nothing to change"):
        run("edit_note", name="gdd")


def test_human_content_is_protected(vault, run, project):
    path = vault.root / "projects/kings-raid/mine.md"
    path.write_text("---\nid: H\ntype: note\ntitle: Mine\nsummary: My note.\ncreated: 2026-11-01\n"
                    "updated: 2026-11-01\nauthor: human\n---\n\n# Mine\n\nhello\n")
    vault.scan()
    with pytest.raises(VaultError, match="human"):
        run("write_note", path="projects/kings-raid/mine.md", title="Mine", summary="Hijacked.", body="x")
    with pytest.raises(VaultError, match="only append"):
        run("edit_note", name="mine", find="hello", replace="bye")
    run("edit_note", name="mine", append="agent addition")
    assert "hello" in vault.read_text("projects/kings-raid/mine.md")

    run("write_note", path=GDD, title="GDD", summary="Doc.", body="a\n\n<!-- human -->keep me<!-- /human -->")
    with pytest.raises(VaultError, match="verbatim"):
        run("write_note", path=GDD, title="GDD", summary="Doc.", body="a")
    with pytest.raises(VaultError, match="human-written block"):
        run("edit_note", name="gdd", find="keep me", replace="gone")


def test_index_append_stays_above_children(vault, run, project):
    run("edit_note", name="projects/kings-raid", append="Vertical slice phase.")
    run("write_note", path=GDD, title="GDD", summary="Doc.", body="x")
    body = vault.load("projects/kings-raid/index.md").body
    assert body.index("Vertical slice phase.") < body.index("## Children") < body.index("[[gdd]]")


def test_link_and_backlinks(vault, run, project):
    run("write_note", path=GDD, title="GDD", summary="Doc.", body="x")
    run("write_note", path="topics/gacha.md", title="Gacha", summary="Gacha mechanics.", body="See [[gdd]].")
    assert run("link", source="gdd", target="gacha") == f"linked from {GDD}"
    assert "already links" in run("link", source="gdd", target="gacha")
    assert vault.load(GDD).meta["related"] == ["[[gacha]]"]
    assert "[[gdd]] — Doc." in run("follow_links", name="gacha", direction="in")
    assert "[[gacha]] — Gacha mechanics." in run("follow_links", name="gdd")
    with pytest.raises(VaultError, match="can't be used"):
        run("link", source="gdd", target="gacha", kind="summary")


def test_move_note(vault, run, project):
    run("write_note", path=GDD, title="GDD", summary="Doc.", body="x")
    run("write_note", path="topics/gacha.md", title="Gacha", summary="Mechanics.", body="See [[gdd]].")
    assert run("move_note", name="gdd", folder="topics") == "moved to topics/gdd.md"
    assert "[[gdd]]" not in vault.read_text("projects/kings-raid/index.md")
    assert "[[gdd]]" in vault.read_text("topics/index.md")
    assert vault.resolve_link("gdd") == "topics/gdd.md"
    assert vault.check() == []


def test_mark_outdated(vault, run, project):
    run("write_note", path=GDD, title="GDD", summary="Doc v1.", body="x")
    run("write_note", path="projects/kings-raid/gdd-v2.md", title="GDD v2", summary="Doc v2.", body="y")
    run("mark_outdated", name="gdd", superseded_by="gdd-v2")
    assert vault.load(GDD).meta["status"] == "outdated"
    assert vault.load(GDD).meta["superseded_by"] == "[[gdd-v2]]"
    assert vault.load("projects/kings-raid/gdd-v2.md").meta["supersedes"] == ["[[gdd]]"]
    assert "[[gdd]] — Doc v1. *(outdated)*" in vault.read_text("projects/kings-raid/index.md")


def test_add_source_from_inbox(vault, run):
    (vault.root / "inbox" / "Design Notes.md").write_text("# Notes\n\nThe pity counter resets at 90 pulls.")
    assert "inbox/Design Notes.md" in run("list_inbox")
    out = run("add_source", file="inbox/Design Notes.md")
    assert "extracted" in out
    name = out.split("[[")[1].split("]]")[0]
    note = vault.resolve(name)
    meta = vault.load(note).meta
    assert meta["type"] == "source" and meta["status"] == "draft" and meta["extraction"] == "done"
    assert not (vault.root / "inbox" / "Design Notes.md").exists()
    assert (vault.root / note).parent.joinpath("original.md").is_file()
    assert "pity counter" in run("read_source", name=name)
    assert name in vault.read_text(f"sources/{note.split('/')[1]}/{note.split('/')[2]}/index.md")

    (vault.root / "inbox" / "copy.md").write_text("# Notes\n\nThe pity counter resets at 90 pulls.")
    assert "already in the brain" in run("add_source", file="inbox/copy.md")
    assert run("list_inbox") == "(inbox is empty)"
    assert vault.check() == []


def test_add_source_paging_and_binary(vault, run):
    (vault.root / "inbox" / "long.txt").write_text("a" * 100)
    name = run("add_source", file="inbox/long.txt", title="Long", summary="Long text.").split("[[")[1].split("]]")[0]
    out = run("read_source", name=name, start=0, max_chars=40)
    assert "chars 0-40 of 100; continue with start=40" in out
    assert "end of text" in run("read_source", name=name, start=80, max_chars=40)
    (vault.root / "inbox" / "pic.png").write_bytes(b"\x89PNG\r\n")
    assert "no text extractor" in run("add_source", file="inbox/pic.png")


def test_add_source_outside_allowed_roots(run, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    with pytest.raises(VaultError, match="inbox"):
        run("add_source", file=str(outside))


def test_import_root(vault, tmp_path):
    from aibrain.tools import Context, call
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "plan.txt").write_text("plan")
    out = call(vault, "add_source", {"file": str(docs / "plan.txt")}, Context("agent:t", [docs]))
    assert "added" in out and (docs / "plan.txt").exists()  # originals outside the inbox are copied, not moved


def test_navigation_path(run, project):
    run("write_note", path=GDD, title="GDD", summary="Full design doc.", body="x")
    assert "[[projects/index|Projects]]" in run("read_index")
    assert "[[projects/kings-raid/index|Kings Raid clone]]" in run("read_index", path="projects")
    assert "[[gdd]] — Full design doc." in run("read_index", path="projects/kings-raid")
    with pytest.raises(VaultError, match="not a folder"):
        run("read_index", path="gdd")


def test_read_note_section_and_suggestions(run, project):
    run("write_note", path=GDD, title="GDD", summary="Doc.", body="# GDD\n\n## Combat\nauto battle\n## Gacha\nrates")
    assert run("read_note", name="gdd", section="combat").endswith("## Combat\nauto battle")
    with pytest.raises(VaultError, match="sections: GDD, Combat, Gacha"):
        run("read_note", name="gdd", section="economy")
    with pytest.raises(VaultError, match="did you mean: gdd"):
        run("read_note", name="gdd2")


def test_search(run, project):
    run("write_note", path=GDD, title="Game design", summary="Combat and gacha.", body="The pity system resets.")
    run("write_note", path="topics/misc.md", title="Misc", summary="Other.", body="nothing relevant")
    out = run("search", query="pity system")
    assert out.startswith("- [[gdd]]") and "pity system" in out
    assert run("search", query="zzzz") == "no results"


def test_external_edits_are_picked_up(vault, run, project):
    other = Vault(vault.root)  # e.g. a second MCP server on the same vault
    other.write_note(GDD, "GDD", "Doc.", "x")
    vault._fresh(force=True)
    assert vault.resolve_link("gdd") == GDD


def test_check_finds_problems(vault, run, project):
    run("write_note", path=GDD, title="GDD", summary="Doc.", body="see [[nowhere]]")
    (vault.root / "topics" / "raw.md").write_text("no frontmatter")
    (vault.root / "topics" / "empty-dir").mkdir()
    kinds = {i["kind"] for i in vault.check()}
    assert {"broken-link", "missing-fields", "stale-index", "missing-index"} <= kinds
    run("refresh_index", all=True)
    assert "stale-index" not in {i["kind"] for i in vault.check()}


def test_oversized_index_is_reported(vault, run):
    vault.config.limits.index_max_entries = 2
    for i in range(3):
        run("write_note", path=f"topics/t{i}.md", title=f"T{i}", summary="x.", body="x")
    assert "OVERSIZED" in run("refresh_index", path="topics")
    assert "oversized-index" in {i["kind"] for i in vault.check()}
