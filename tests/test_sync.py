"""Tests for sync and generate."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from marketplace_sync import (
    discover_plugins,
    escape_md_table,
    generate_marketplace,
    generate_readme,
    generate_table,
    parse_list,
    read_plugin_json,
    repo_slug,
    set_output,
    sync_plugins,
    validate_repo_url,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_repo_slug():
    assert repo_slug("https://github.com/owner/repo") == "owner/repo"
    assert repo_slug("https://github.com/owner/repo.git") == "owner/repo"


def test_validate_repo_url():
    validate_repo_url("https://github.com/owner/repo")
    validate_repo_url("https://github.com/owner/repo.git")
    validate_repo_url("https://github.com/my-org/my.repo")
    for bad in ["git@github.com:owner/repo.git", "https://gitlab.com/owner/repo", ""]:
        with pytest.raises(SystemExit):
            validate_repo_url(bad)


def test_parse_list():
    assert parse_list("commit") == ["commit"]
    assert parse_list("foo, bar, baz") == ["foo", "bar", "baz"]
    assert parse_list("  foo ,  bar  ") == ["foo", "bar"]
    assert parse_list("") == []
    assert parse_list("foo,") == ["foo"]


def test_escape_md_table():
    assert escape_md_table("hello world") == "hello world"
    assert escape_md_table("foo | bar") == "foo \\| bar"


def test_read_plugin_json(tmp_path):
    data = read_plugin_json(FIXTURES / "marketplace" / "plugins" / "foo")
    assert data["name"] == "foo"
    assert data["description"] == "Foo plugin"

    # missing plugin.json returns empty dict
    empty = tmp_path / "empty-plugin"
    empty.mkdir()
    assert read_plugin_json(empty) == {}


def test_discover_plugins(tmp_path):
    plugins = discover_plugins(str(FIXTURES / "marketplace" / "plugins"))
    assert len(plugins) == 2
    assert plugins[0]["name"] == "bar"
    assert plugins[1]["name"] == "foo"

    # empty and missing dirs return []
    (tmp_path / "empty").mkdir()
    assert discover_plugins(str(tmp_path / "empty")) == []
    assert discover_plugins(str(tmp_path / "nonexistent")) == []


def test_generate_marketplace():
    plugins = [
        {
            "name": "my-plugin",
            "description": "Great plugin",
            "version": "2.0.0",
            "sync-metadata": {"repo": "https://github.com/a/b", "commit": "abc123"},
        }
    ]
    mp = generate_marketplace(plugins, "test-marketplace", "Test Owner", "plugins")

    assert mp["plugins"][0]["source"] == "./plugins/my-plugin"
    assert "sync-metadata" not in mp["plugins"][0]
    assert generate_marketplace([], "t", "o", "p")["plugins"] == []


def test_set_output(tmp_path, monkeypatch):
    output_file = tmp_path / "output"
    output_file.touch()
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    set_output("changed", "true")
    assert output_file.read_text() == "changed=true\n"

    # no-op when env unset
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    set_output("changed", "true")


def _git_run(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True)


def _make_upstream(tmp_path) -> Path:
    """Copy the marketplace fixture into a tmp git repo."""
    repo = tmp_path / "upstream"
    shutil.copytree(FIXTURES / "marketplace", repo)
    _git_run(repo, "init", "--initial-branch=main")
    _git_run(repo, "config", "user.email", "test@test.com")
    _git_run(repo, "config", "user.name", "Test")
    _git_run(repo, "add", ".")
    _git_run(repo, "commit", "-m", "add plugins")
    return repo


@pytest.fixture
def upstream_repo(tmp_path):
    return _make_upstream(tmp_path)


@pytest.fixture
def work_dir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


class TestSyncPlugins:
    def test_initial_sync(self, upstream_repo, work_dir, monkeypatch):
        monkeypatch.chdir(work_dir)

        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")

        dest = work_dir / "plugins" / "foo"
        assert dest.is_dir()
        assert (dest / ".claude-plugin" / "plugin.json").is_file()
        assert (dest / "skills" / "foo-skill" / "SKILL.md").is_file()

    def test_stamps_sync_metadata(self, upstream_repo, work_dir, monkeypatch):
        monkeypatch.chdir(work_dir)

        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")

        plugin_json = json.loads(
            (
                work_dir / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
            ).read_text()
        )
        meta = plugin_json["sync-metadata"]
        assert meta["repo"] == str(upstream_repo)
        assert len(meta["commit"]) == 40

    def test_overwrites_on_rerun(self, upstream_repo, work_dir, monkeypatch):
        monkeypatch.chdir(work_dir)

        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")
        (work_dir / "plugins" / "foo" / "stale.txt").write_text("old")
        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")
        assert not (work_dir / "plugins" / "foo" / "stale.txt").exists()

    def test_copies_updated_content(self, upstream_repo, work_dir, monkeypatch):
        monkeypatch.chdir(work_dir)

        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")

        skill_file = (
            upstream_repo / "plugins" / "foo" / "skills" / "foo-skill" / "SKILL.md"
        )
        skill_file.write_text(
            "---\ndescription: Updated\n---\n# Foo Skill\n\nUpdated.\n"
        )
        _git_run(upstream_repo, "add", ".")
        _git_run(upstream_repo, "commit", "-m", "update skill")

        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")
        synced = (
            work_dir / "plugins" / "foo" / "skills" / "foo-skill" / "SKILL.md"
        ).read_text()
        assert "Updated." in synced

    def test_copies_reference_files(self, upstream_repo, work_dir, monkeypatch):
        monkeypatch.chdir(work_dir)

        sync_plugins(str(upstream_repo), "main", ["bar"], "plugins")

        ref = work_dir / "plugins" / "bar" / "skills" / "bar-skill" / "reference.md"
        assert ref.is_file()
        assert "Extra info." in ref.read_text()

    def test_decompresses_skill_files(self, upstream_repo, work_dir, monkeypatch):
        """Gzip-compressed .skill files are decompressed to .md and the originals removed."""
        import gzip

        monkeypatch.chdir(work_dir)

        skill_content = "---\ndescription: My skill\n---\n# My Skill\n\nDoes things.\n"
        skill_path = (
            upstream_repo / "plugins" / "foo" / "skills" / "foo-skill" / "foo.skill"
        )
        skill_path.write_bytes(gzip.compress(skill_content.encode()))
        _git_run(upstream_repo, "add", ".")
        _git_run(upstream_repo, "commit", "-m", "add .skill file")

        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")

        dest = work_dir / "plugins" / "foo" / "skills" / "foo-skill"
        assert not (dest / "foo.skill").exists()
        md = dest / "foo.md"
        assert md.is_file()
        assert md.read_text() == skill_content

    def test_missing_plugin_errors(self, upstream_repo, work_dir, monkeypatch):
        monkeypatch.chdir(work_dir)

        with pytest.raises(SystemExit):
            sync_plugins(str(upstream_repo), "main", ["nonexistent"], "plugins")

    def test_preserves_commit_when_no_content_changes(
        self, upstream_repo, work_dir, monkeypatch
    ):
        """Re-syncing with no upstream content changes must not bump the commit SHA."""
        monkeypatch.chdir(work_dir)
        _git_run(work_dir, "init", "--initial-branch=main")
        _git_run(work_dir, "config", "user.email", "test@test.com")
        _git_run(work_dir, "config", "user.name", "Test")

        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")
        _git_run(work_dir, "add", ".")
        _git_run(work_dir, "commit", "-m", "initial sync")

        first_commit = json.loads(
            (
                work_dir / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
            ).read_text()
        )["sync-metadata"]["commit"]

        # Make a no-op upstream commit (only touches an unrelated file)
        (upstream_repo / "unrelated.txt").write_text("noise")
        _git_run(upstream_repo, "add", ".")
        _git_run(upstream_repo, "commit", "-m", "unrelated change")

        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")

        second_commit = json.loads(
            (
                work_dir / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
            ).read_text()
        )["sync-metadata"]["commit"]

        assert first_commit == second_commit

    def test_updates_commit_when_content_changes(
        self, upstream_repo, work_dir, monkeypatch
    ):
        """Re-syncing after a real content change must bump the commit SHA."""
        monkeypatch.chdir(work_dir)
        _git_run(work_dir, "init", "--initial-branch=main")
        _git_run(work_dir, "config", "user.email", "test@test.com")
        _git_run(work_dir, "config", "user.name", "Test")

        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")
        _git_run(work_dir, "add", ".")
        _git_run(work_dir, "commit", "-m", "initial sync")

        first_commit = json.loads(
            (
                work_dir / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
            ).read_text()
        )["sync-metadata"]["commit"]

        skill_file = (
            upstream_repo / "plugins" / "foo" / "skills" / "foo-skill" / "SKILL.md"
        )
        skill_file.write_text("---\ndescription: Updated\n---\n# Foo\n\nUpdated.\n")
        _git_run(upstream_repo, "add", ".")
        _git_run(upstream_repo, "commit", "-m", "update skill content")

        sync_plugins(str(upstream_repo), "main", ["foo"], "plugins")

        second_commit = json.loads(
            (
                work_dir / "plugins" / "foo" / ".claude-plugin" / "plugin.json"
            ).read_text()
        )["sync-metadata"]["commit"]

        assert first_commit != second_commit


def test_generate_table(upstream_repo, work_dir, monkeypatch):
    monkeypatch.chdir(work_dir)
    repo = str(upstream_repo)

    sync_plugins(repo, "main", ["foo"], "plugins")
    plugins = discover_plugins("plugins")
    table = generate_table(plugins)

    assert "**foo**" in table
    assert "Foo plugin" in table
    assert repo in table
    meta = plugins[0]["sync-metadata"]
    assert f"`{meta['commit'][:7]}`" in table

    # empty plugins
    assert "No plugins synced yet" in generate_table([])


def test_generate_readme():
    plugins = [{"name": "p1", "description": "desc"}]

    readme = generate_readme(plugins, prefix="# My Plugins")
    assert readme.startswith("# My Plugins\n")
    assert "**p1**" in readme

    # no prefix
    assert generate_readme(plugins).startswith("| Plugin")

    # empty
    assert "No plugins synced yet" in generate_readme([])


# --- root-source plugin tests (source: "./") ---

ROOT_SOURCE_FIXTURES = FIXTURES / "root-source-marketplace"


def _make_root_source_upstream(tmp_path) -> Path:
    repo = tmp_path / "root-source-upstream"
    shutil.copytree(ROOT_SOURCE_FIXTURES, repo)
    _git_run(repo, "init", "--initial-branch=main")
    _git_run(repo, "config", "user.email", "test@test.com")
    _git_run(repo, "config", "user.name", "Test")
    _git_run(repo, "add", ".")
    _git_run(repo, "commit", "-m", "add plugin")
    return repo


@pytest.fixture
def root_source_upstream(tmp_path):
    return _make_root_source_upstream(tmp_path)


class TestSyncRootSource:
    def test_copies_full_directory(self, root_source_upstream, work_dir, monkeypatch):
        """source: './' copies the entire plugin directory (minus .git)."""
        monkeypatch.chdir(work_dir)

        sync_plugins(str(root_source_upstream), "main", ["my-plugin"], "plugins")

        dest = work_dir / "plugins" / "my-plugin"
        # plugin components are copied
        assert (dest / ".claude-plugin" / "plugin.json").is_file()
        assert (dest / "skills" / "my-skill" / "SKILL.md").is_file()
        assert (dest / "commands" / "review.md").is_file()
        assert (dest / "agents" / "helper.md").is_file()
        assert (dest / "hooks" / "hooks.json").is_file()
        assert (dest / "settings.json").is_file()

        # repo-level files ARE copied (full directory copy)
        assert (dest / "README.md").is_file()
        assert (dest / "LICENSE").is_file()

        # .git is excluded
        assert not (dest / ".git").exists()

    def test_preserves_existing_plugin_json(
        self,
        root_source_upstream,
        work_dir,
        monkeypatch,
    ):
        monkeypatch.chdir(work_dir)

        sync_plugins(str(root_source_upstream), "main", ["my-plugin"], "plugins")

        dest = work_dir / "plugins" / "my-plugin"
        pj = json.loads((dest / ".claude-plugin" / "plugin.json").read_text())
        assert pj["name"] == "my-plugin"
        assert pj["version"] == "1.0.0"
        assert "sync-metadata" in pj


# --- plugins without plugin.json (e.g. marketingskills) ---

SKILLS_FIXTURES = FIXTURES / "skills-marketplace"


def _make_skills_upstream(tmp_path) -> Path:
    repo = tmp_path / "skills-upstream"
    shutil.copytree(SKILLS_FIXTURES, repo)
    _git_run(repo, "init", "--initial-branch=main")
    _git_run(repo, "config", "user.email", "test@test.com")
    _git_run(repo, "config", "user.name", "Test")
    _git_run(repo, "add", ".")
    _git_run(repo, "commit", "-m", "add skills")
    return repo


@pytest.fixture
def skills_upstream(tmp_path):
    return _make_skills_upstream(tmp_path)


class TestSyncSynthesizedPluginJson:
    def test_synthesizes_plugin_json(self, skills_upstream, work_dir, monkeypatch):
        """Plugins without plugin.json get one synthesized from the marketplace entry."""
        monkeypatch.chdir(work_dir)

        sync_plugins(str(skills_upstream), "main", ["all-skills"], "plugins")

        dest = work_dir / "plugins" / "all-skills"
        assert dest.is_dir()
        pj = json.loads((dest / ".claude-plugin" / "plugin.json").read_text())
        assert pj["name"] == "all-skills"
        assert pj["description"] == "A bundle of skills"
        assert pj["sync-metadata"]["repo"] == str(skills_upstream)

    def test_copies_full_directory(self, skills_upstream, work_dir, monkeypatch):
        """Full directory is copied, including non-skill files."""
        monkeypatch.chdir(work_dir)

        sync_plugins(str(skills_upstream), "main", ["all-skills"], "plugins")

        dest = work_dir / "plugins" / "all-skills"
        assert (dest / "skills" / "alpha" / "SKILL.md").is_file()
        assert (dest / "skills" / "beta" / "SKILL.md").is_file()
        assert (dest / "README.md").is_file()
        assert not (dest / ".git").exists()
