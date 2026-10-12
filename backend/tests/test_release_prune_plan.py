import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packaging"))

import release_prune_plan as rp  # noqa: E402


def cat(*urls):
    return {"signed": {"components": [{"url": u} for u in urls]}}


def rel(rid, tag, assets=(), published="2026-01-01T00:00:00Z"):
    return {
        "id": rid,
        "tag_name": tag,
        "name": tag,
        "published_at": published,
        "assets": [{"name": n} for n in assets],
    }


def decisions(plan):
    return {r["tag"]: r["decision"] for r in plan}


def test_referenced_tags_walks_nested_urls():
    catalog = cat(
        "https://github.com/kidlost1412/KidTE/releases/download/v1.14.255-p3/a.zip",
        "https://github.com/kidlost1412/KidTE/releases/download/v1.14.259-p3/b.zip",
    )
    catalog["signed"]["components"][0]["files"] = [{"url": "https://x/releases/download/v9.9.9-p1/c.zip"}]
    assert rp.referenced_tags(catalog) == {"v1.14.255-p3", "v1.14.259-p3", "v9.9.9-p1"}


def test_referenced_tags_ignores_non_release_urls():
    assert rp.referenced_tags(cat("https://example.com/a.zip")) == set()


def test_core_version_parsing():
    assert rp.core_version("v1.14.259-p3") == (1, 14, 259)
    assert rp.core_version("v1.14.259-p3-hotfix") == (1, 14, 259)
    assert rp.core_version("v1.14.254-p2") is None
    assert rp.core_version("updates-beta-p3") is None
    assert rp.core_version("") is None


def test_keeps_catalog_referenced_and_feed_tags():
    releases = [
        rel(1, "v1.14.255-p3"),
        rel(2, "updates-beta-p3"),
        rel(3, "v1.14.100-p3"),
    ]
    plan = rp.build_plan(releases, [cat("https://github.com/o/r/releases/download/v1.14.255-p3/x.zip")], keep_core=0)
    d = decisions(plan)
    assert d["v1.14.255-p3"] == "KEEP"
    assert d["updates-beta-p3"] == "KEEP"
    assert d["v1.14.100-p3"] == "DELETE"


def test_keeps_newest_n_core_versions_by_version_not_string():
    releases = [rel(i, "v1.14.%d-p3" % n) for i, n in enumerate([9, 10, 100, 99], start=1)]
    plan = rp.build_plan(releases, [cat()], keep_core=2)
    d = decisions(plan)
    assert d["v1.14.100-p3"] == "KEEP"
    assert d["v1.14.99-p3"] == "KEEP"
    assert d["v1.14.10-p3"] == "DELETE"
    assert d["v1.14.9-p3"] == "DELETE"


def test_keep_core_zero_deletes_unreferenced_core():
    plan = rp.build_plan([rel(1, "v1.14.5-p3")], [cat()], keep_core=0)
    assert decisions(plan)["v1.14.5-p3"] == "DELETE"


def test_keep_core_negative_rejected():
    with pytest.raises(ValueError):
        rp.build_plan([], [cat()], keep_core=-1)


def test_newest_installer_release_is_kept():
    releases = [
        rel(1, "v1.14.200-p3", assets=["KidTE-Setup-1.14.200.exe"]),
        rel(2, "v1.14.230-p3", assets=["KidTE-Setup-1.14.230.exe"]),
        rel(3, "v1.14.240-p3", assets=["KidTE.zip"]),
        rel(4, "v1.14.210-p3", assets=["KidTE-Setup-1.14.210.exe"]),
    ]
    plan = rp.build_plan(releases, [cat()], keep_core=0)
    d = decisions(plan)
    assert d["v1.14.230-p3"] == "KEEP"
    assert d["v1.14.200-p3"] == "DELETE"
    assert d["v1.14.210-p3"] == "DELETE"
    assert d["v1.14.240-p3"] == "DELETE"
    reasons = {r["tag"]: r["reason"] for r in plan}
    assert reasons["v1.14.230-p3"] == "newest installer (Setup exe)"


def test_installer_tie_broken_by_published_at():
    releases = [
        rel(1, "v2.0.0-p2", assets=["Setup.exe"], published="2026-01-01T00:00:00Z"),
        rel(2, "v1.0.0-p2", assets=["Setup.exe"], published="2026-06-01T00:00:00Z"),
    ]
    # Non-core tags have version (0,0,0): the later published one wins.
    assert rp.newest_installer_release_id(releases) == 2


def test_no_installer_means_no_installer_keep():
    assert rp.newest_installer_release_id([rel(1, "v1.0.0-p3", assets=["a.zip"])]) is None


def test_release_without_tag_is_kept():
    plan = rp.build_plan([{"id": 7, "tag_name": None, "assets": []}], [cat()], keep_core=0)
    assert plan[0]["decision"] == "KEEP"
    assert plan[0]["reason"] == "no tag (never pruned)"


def test_multiple_catalogs_union_references():
    plan = rp.build_plan(
        [rel(1, "v1.14.254-p2"), rel(2, "v1.14.255-p3")],
        [
            cat("https://github.com/o/r/releases/download/v1.14.254-p2/a.zip"),
            cat("https://github.com/o/r/releases/download/v1.14.255-p3/b.zip"),
        ],
        keep_core=0,
    )
    assert decisions(plan) == {"v1.14.254-p2": "KEEP", "v1.14.255-p3": "KEEP"}


def test_plan_is_sorted_and_complete():
    releases = [rel(i, "v1.14.%d-p3" % i) for i in range(1, 6)]
    plan = rp.build_plan(releases, [cat()], keep_core=2)
    assert len(plan) == 5
    assert [r["decision"] for r in plan] == sorted(r["decision"] for r in plan)
    assert sum(1 for r in plan if r["decision"] == "KEEP") == 2


def test_cli_writes_plan_and_prints(tmp_path, capsys):
    import json

    releases_path = tmp_path / "r.json"
    catalog_path = tmp_path / "c.json"
    plan_path = tmp_path / "p.json"
    releases_path.write_text(json.dumps([rel(1, "v1.14.1-p3"), rel(2, "v1.14.2-p3")]), encoding="utf-8")
    catalog_path.write_text(json.dumps(cat()), encoding="utf-8")
    rc = rp.main(["--releases", str(releases_path), "--catalog", str(catalog_path),
                  "--keep-core", "1", "--plan-out", str(plan_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SUMMARY: KEEP=1 DELETE=1 total=2" in out
    saved = json.loads(plan_path.read_text(encoding="utf-8"))
    assert {r["tag"]: r["decision"] for r in saved} == {"v1.14.2-p3": "KEEP", "v1.14.1-p3": "DELETE"}


def test_cli_requires_catalog(tmp_path, capsys):
    releases_path = tmp_path / "r.json"
    releases_path.write_text("[]", encoding="utf-8")
    assert rp.main(["--releases", str(releases_path)]) == 2
