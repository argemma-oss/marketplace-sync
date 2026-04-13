"""Sync Claude Code plugins from upstream marketplace repos."""

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

REPO_URL_RE = re.compile(r"^https://github\.com/[\w.\-]+/[\w.\-]+(?:\.git)?$")
MARKETPLACE_DIR = ".claude-plugin"
MARKETPLACE_FILE = f"{MARKETPLACE_DIR}/marketplace.json"
DEFAULT_DESCRIPTION = (
    "Curated Claude Code plugins, automatically synced from upstream sources."
)


def fail(msg: str) -> NoReturn:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def validate_repo_url(repo_url: str):
    if not REPO_URL_RE.match(repo_url):
        fail(
            f"'{repo_url}' is not a valid GitHub HTTPS URL. Expected format: https://github.com/owner/repo"
        )


def parse_list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def repo_slug(repo_url: str) -> str:
    return repo_url.removeprefix("https://github.com/").removesuffix(".git")


def escape_md_table(text: str) -> str:
    return text.replace("|", "\\|")


def set_output(key: str, value: str):
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{key}={value}\n")


def has_git_changes(*paths: str) -> bool:
    try:
        git("diff", "--quiet", "--", *paths)
        untracked = git("ls-files", "--others", "--exclude-standard", "--", *paths)
        return bool(untracked)
    except RuntimeError:
        return True


def synthesize_plugin_json(plugin_entry: dict) -> dict:
    """Synthesize a plugin.json from a marketplace plugin entry."""
    return {
        "name": plugin_entry["name"],
        "description": plugin_entry.get("description", ""),
    }


def read_plugin_json(plugin_dir: Path) -> dict:
    path = plugin_dir / ".claude-plugin" / "plugin.json"
    if path.is_file():
        return json.loads(path.read_text())
    return {}


def discover_plugins(plugins_dir: str) -> list[dict]:
    plugins_path = Path(plugins_dir)
    if not plugins_path.is_dir():
        return []
    plugins = []
    for d in sorted(plugins_path.iterdir()):
        if d.is_dir():
            data = read_plugin_json(d)
            data.setdefault("name", d.name)
            plugins.append(data)
    return plugins


@contextmanager
def shallow_clone(repo_url: str, branch: str):
    d = Path(tempfile.mkdtemp(prefix="marketplace-sync-"))
    try:
        git("clone", "--depth", "1", "-b", branch, "--quiet", repo_url, str(d))
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def read_upstream_marketplace(repo_dir: Path) -> dict:
    path = repo_dir / ".claude-plugin" / "marketplace.json"
    if not path.is_file():
        fail("no .claude-plugin/marketplace.json found in upstream repo.")
    return json.loads(path.read_text())


def resolve_plugin_source(
    upstream_marketplace: dict, plugin_name: str
) -> tuple[str, dict]:
    for entry in upstream_marketplace.get("plugins", []):
        if entry["name"] == plugin_name:
            source = entry["source"]
            if isinstance(source, str):
                return source.removeprefix("./"), entry
            fail(
                f"plugin '{plugin_name}' has a non-path source (github/npm/etc). "
                "Only relative path sources are supported."
            )
    fail(f"plugin '{plugin_name}' not found in upstream marketplace.json.")


def stamp_plugin(plugin_dir: Path, repo: str, commit: str):
    path = plugin_dir / ".claude-plugin" / "plugin.json"
    if not path.is_file():
        fail(f"plugin.json not found in {plugin_dir}")
    data = json.loads(path.read_text())
    data["sync-metadata"] = {
        "repo": repo,
        "commit": commit,
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _plugin_content_changed(plugin_dir: Path, old_plugin_json: dict) -> bool:
    """Return True if plugin content changed vs HEAD, ignoring sync-metadata commit bumps."""
    pj_path = str(plugin_dir / ".claude-plugin" / "plugin.json")

    # Check if any non-plugin.json files changed or are newly untracked
    try:
        diff_out = git("diff", "--name-only", "--", str(plugin_dir))
        if any(f != pj_path for f in diff_out.splitlines() if f):
            return True
        untracked = git(
            "ls-files", "--others", "--exclude-standard", "--", str(plugin_dir)
        )
        if any(f != pj_path for f in untracked.splitlines() if f):
            return True
    except RuntimeError:
        return True

    # Check if plugin.json metadata changed (ignoring sync-metadata)
    new_data = read_plugin_json(plugin_dir)
    old_meta = {k: v for k, v in old_plugin_json.items() if k != "sync-metadata"}
    new_meta = {k: v for k, v in new_data.items() if k != "sync-metadata"}
    return old_meta != new_meta


def _write_plugin_json(plugin_dir: Path, data: dict):
    cp_dir = plugin_dir / ".claude-plugin"
    cp_dir.mkdir(parents=True, exist_ok=True)
    (cp_dir / "plugin.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n"
    )


def sync_plugins(
    repo: str,
    branch: str,
    plugin_names: list[str],
    plugins_dir: str,
):
    print(f"--- Cloning {repo}@{branch}")
    with shallow_clone(repo, branch) as upstream:
        commit = git("rev-parse", "HEAD", cwd=str(upstream))

        upstream_marketplace = read_upstream_marketplace(upstream)

        for name in plugin_names:
            source_path, plugin_entry = resolve_plugin_source(
                upstream_marketplace, name
            )

            src = upstream / source_path
            dest = Path(plugins_dir) / name

            if not src.is_dir():
                fail(
                    f"{source_path} resolved from marketplace.json "
                    "but not found on disk."
                )

            old_plugin_json = read_plugin_json(dest) if dest.exists() else None

            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))

            for skill_file in dest.rglob("*.skill"):
                skill_file.with_suffix(".md").write_text(
                    gzip.decompress(skill_file.read_bytes()).decode()
                )
                skill_file.unlink()

            # Synthesize plugin.json if the plugin doesn't have one
            if not (dest / ".claude-plugin" / "plugin.json").is_file():
                _write_plugin_json(dest, synthesize_plugin_json(plugin_entry))

            # Only update sync-metadata if plugin content actually changed.
            # If the upstream commit moved but files are identical, restore the
            # old plugin.json to avoid spurious diffs that produce empty PRs.
            if old_plugin_json is None or _plugin_content_changed(
                dest, old_plugin_json
            ):
                stamp_plugin(dest, repo, commit)
                print(f"  {name}: synced")
            else:
                _write_plugin_json(dest, old_plugin_json)
                print(f"  {name}: up to date")


def generate_marketplace(
    plugins: list[dict],
    marketplace_name: str,
    owner_name: str,
    plugins_dir: str,
    description: str = DEFAULT_DESCRIPTION,
) -> dict:
    entries = []
    for p in plugins:
        entry = {k: v for k, v in p.items() if k != "sync-metadata"}
        entry["source"] = f"./{plugins_dir}/{p['name']}"
        entries.append(entry)

    return {
        "name": marketplace_name,
        "owner": {"name": owner_name},
        "metadata": {
            "description": description,
            "pluginRoot": f"./{plugins_dir}",
        },
        "plugins": entries,
    }


def generate_table(plugins: list[dict]) -> str:
    lines = [
        "| Plugin | Description | Source | Commit |",
        "|--------|-------------|--------|--------|",
    ]

    sorted_plugins = sorted(
        plugins,
        key=lambda p: (
            repo_slug(p.get("sync-metadata", {}).get("repo", "")),
            p.get("name", ""),
        ),
    )

    for p in sorted_plugins:
        name = p["name"]
        description = escape_md_table(p.get("description", ""))
        sync_meta = p.get("sync-metadata", {})
        repo_url = sync_meta.get("repo", "")
        commit = sync_meta.get("commit", "")
        if repo_url:
            display = repo_slug(repo_url)
            source_col = f"[{display}]({repo_url})"
        else:
            source_col = ""

        if commit and repo_url:
            short = commit[:7]
            commit_col = f"[`{short}`]({repo_url}/commit/{commit})"
        elif commit:
            commit_col = f"`{commit[:7]}`"
        else:
            commit_col = ""

        lines.append(f"| **{name}** | {description} | {source_col} | {commit_col} |")

    if not plugins:
        lines.append("| *No plugins synced yet.* | | | |")

    return "\n".join(lines)


def generate_readme(plugins: list[dict], prefix: str = "") -> str:
    table = generate_table(plugins)
    parts = [p for p in [prefix, table] if p]
    return "\n\n".join(parts) + "\n"


def sync_cli():
    parser = argparse.ArgumentParser(description="Sync plugins from an upstream repo")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--plugins", required=True)
    parser.add_argument("--plugins-dir", default="plugins")
    args = parser.parse_args()

    validate_repo_url(args.repo)

    plugin_names = parse_list(args.plugins)
    if not plugin_names:
        fail("no plugin names provided.")

    sync_plugins(args.repo, args.branch, plugin_names, args.plugins_dir)


def generate_cli():
    parser = argparse.ArgumentParser(description="Generate marketplace and README")
    parser.add_argument("--plugins-dir", default="plugins")
    parser.add_argument("--readme-path", default="")
    parser.add_argument("--readme-table-prefix", default="")
    parser.add_argument("--marketplace-name", default="marketplace-sync")
    parser.add_argument("--marketplace-description", default=DEFAULT_DESCRIPTION)
    parser.add_argument("--owner-name", default="marketplace-sync")
    args = parser.parse_args()

    plugins = discover_plugins(args.plugins_dir)

    marketplace = generate_marketplace(
        plugins,
        args.marketplace_name,
        args.owner_name,
        args.plugins_dir,
        description=args.marketplace_description,
    )
    Path(MARKETPLACE_DIR).mkdir(exist_ok=True)
    Path(MARKETPLACE_FILE).write_text(
        json.dumps(marketplace, indent=2, sort_keys=True) + "\n"
    )
    print(f"{MARKETPLACE_FILE} generated.")

    change_paths = [args.plugins_dir, MARKETPLACE_DIR]

    if args.readme_path:
        readme = generate_readme(plugins, prefix=args.readme_table_prefix)
        Path(args.readme_path).write_text(readme)
        print(f"{args.readme_path} generated.")
        change_paths.append(args.readme_path)

    changed = has_git_changes(*change_paths)
    set_output("changed", str(changed).lower())
