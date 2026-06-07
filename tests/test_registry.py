import shutil

from graphskill.index import build_index
from graphskill.registry import list_projects, project_db_path

FIXTURE = __import__("tests.conftest", fromlist=["FIXTURE"]).FIXTURE


def test_two_repos_get_isolated_dbs(tmp_path):
    a = tmp_path / "repoA"
    b = tmp_path / "repoB"
    shutil.copytree(FIXTURE, a)
    shutil.copytree(FIXTURE, b)

    # distinct, path-derived DB locations
    assert project_db_path(a) != project_db_path(b)
    assert project_db_path(a).parent != project_db_path(b).parent

    build_index(a)
    build_index(b)

    # both registered, each pointing at its own DB
    roots = {e["root"] for e in list_projects()}
    assert str(a.resolve()) in roots and str(b.resolve()) in roots
    dbs = {e["db"] for e in list_projects()}
    assert len(dbs) == 2


def test_default_db_is_outside_project_tree(tmp_path):
    a = tmp_path / "repoA"
    shutil.copytree(FIXTURE, a)
    db = project_db_path(a)
    # isolation lives under GRAPHSKILL_HOME, never inside the repo
    assert str(a) not in str(db)
