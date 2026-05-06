#!/usr/bin/env python3
"""Create unstable branches for Jellyfin plugin submodules."""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).parent
NUGET_SOURCE_NAME = "jellyfin-pre"
NUGET_SOURCE_URL = "https://nuget.pkg.github.com/jellyfin/index.json"
UNSTABLE_BRANCH = "unstable"
PR_TITLE = "Unstable: Update to latest Jellyfin preview packages"
MAX_FIX_ITERATIONS = 10

_RE_JELLYFIN_PKG = re.compile(
    r'(PackageReference\s[^>]*Include="Jellyfin\.[^"]*"[^>]*Version=")(\d+)\.\*-\*(")'
)
_RE_MS_PKG = re.compile(
    r'(PackageReference\s[^>]*Include="(?:Microsoft|System)\.[^"]*"[^>]*Version=")[^"]+(")'
)
_RE_NU1605 = re.compile(
    r"NU1605: Detected package downgrade: ((?:Microsoft|System)\.[^\s]+) from ([\d.]+)"
)
_RE_TFM = re.compile(r"(<TargetFramework>)[^<]+(</TargetFramework>)")
_RE_TARGET_ABI = re.compile(r'(targetAbi:\s*")[^"]+(")')


def run(cmd, cwd=None, check=True, **kwargs):
    return subprocess.run(cmd, cwd=cwd, check=check, **kwargs)


def get_output(cmd, cwd=None, check=True):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True).stdout.strip()


def ensure_nuget_source():
    sources = get_output(["dotnet", "nuget", "list", "source"])
    if NUGET_SOURCE_NAME not in sources:
        raise RuntimeError(
            f"NuGet source '{NUGET_SOURCE_NAME}' is not configured. "
            f"Add it with: dotnet nuget add source --name {NUGET_SOURCE_NAME} {NUGET_SOURCE_URL}"
        )


def discover_version():
    result = subprocess.run(
        ["dotnet", "package", "search", "Jellyfin.Controller",
         "--source", NUGET_SOURCE_NAME,
         "--prerelease", "--take", "1", "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(result.stdout)
    packages = data["searchResult"][0]["packages"]
    if not packages:
        raise RuntimeError("Jellyfin.Controller not found in jellyfin-pre feed")
    version = packages[0]["latestVersion"]
    parts = version.split(".")
    return int(parts[0]), int(parts[1]), _get_tfm("Jellyfin.Controller", version)


def _get_tfm(package_id, version):
    cache_root = get_output(["dotnet", "nuget", "locals", "global-packages", "--list"])
    cache_root = Path(cache_root.split("global-packages:")[-1].strip())
    lib_dir = cache_root / package_id.lower() / version / "lib"
    if not lib_dir.exists():
        _fetch_to_cache(package_id, version)
    if not lib_dir.exists():
        raise RuntimeError(f"Failed to resolve TFM for {package_id} {version}")
    tfms = sorted(
        d.name for d in lib_dir.iterdir()
        if d.is_dir() and re.match(r"net\d+\.\d+$", d.name)
    )
    if not tfms:
        raise RuntimeError(f"No .NET TFM found for {package_id} {version}")
    return tfms[-1]


def _fetch_to_cache(package_id, version):
    sdk_version = get_output(["dotnet", "--version"])
    tfm = f"net{sdk_version.split('.')[0]}.0"
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "tmp.csproj").write_text(
            f'<Project Sdk="Microsoft.NET.Sdk">'
            f'<PropertyGroup><TargetFramework>{tfm}</TargetFramework></PropertyGroup>'
            f'<ItemGroup><PackageReference Include="{package_id}" Version="{version}" /></ItemGroup>'
            f'</Project>',
            encoding="utf-8",
        )
        result = subprocess.run(
            ["dotnet", "restore"],
            cwd=tmp, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to fetch {package_id} {version} to NuGet cache:\n"
                f"{result.stdout}{result.stderr}"
            )


def get_plugins(plugin_arg):
    if plugin_arg:
        p = REPO_ROOT / plugin_arg
        if not p.is_dir():
            print(f"Error: {plugin_arg} not found", file=sys.stderr)
            sys.exit(1)
        return [p]
    return sorted(REPO_ROOT.glob("jellyfin-plugin-*"))


def init_submodule(name):
    run(["git", "submodule", "update", "--init", name], cwd=REPO_ROOT)


def check_unstable(plugin_dir):
    run(["git", "fetch", "origin"], cwd=plugin_dir)
    branch_exists = bool(
        get_output(["git", "ls-remote", "--heads", "origin", UNSTABLE_BRANCH], cwd=plugin_dir)
    )
    repo = get_output(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=plugin_dir,
    )
    pr_url = get_output(
        ["gh", "pr", "list", "--repo", repo, "--head", UNSTABLE_BRANCH,
         "--state", "open", "--json", "url", "-q", ".[0].url // empty"],
        cwd=plugin_dir, check=False,
    ) or None
    return branch_exists, pr_url, repo


def update_jellyfin_packages(plugin_dir, new_major):
    changed = False
    for csproj in plugin_dir.rglob("*.csproj"):
        content = csproj.read_text(encoding="utf-8")
        new_content = _RE_JELLYFIN_PKG.sub(rf'\g<1>{new_major}.*-*\3', content)
        if new_content != content:
            csproj.write_text(new_content, encoding="utf-8")
            changed = True
    return changed


def update_build_yaml(plugin_dir, new_major, new_minor):
    build_yaml = plugin_dir / "build.yaml"
    if not build_yaml.exists():
        return
    content = build_yaml.read_text(encoding="utf-8")
    new_content = _RE_TARGET_ABI.sub(rf'\g<1>{new_major}.{new_minor}.0.0\2', content)
    if new_content != content:
        build_yaml.write_text(new_content, encoding="utf-8")


def update_dotnet_framework(plugin_dir, tfm):
    targets = [*plugin_dir.rglob("*.csproj"), plugin_dir / "Directory.Build.props"]
    for path in targets:
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        new_content = _RE_TFM.sub(rf"\g<1>{tfm}\2", content)
        if new_content != content:
            path.write_text(new_content, encoding="utf-8")


def update_ms_packages(plugin_dir, dotnet_major):
    version = f"{dotnet_major}.0.0"
    for csproj in plugin_dir.rglob("*.csproj"):
        content = csproj.read_text(encoding="utf-8")
        new_content = _RE_MS_PKG.sub(rf'\g<1>{version}\2', content)
        if new_content != content:
            csproj.write_text(new_content, encoding="utf-8")


def fix_nu1605(output_text, plugin_dir):
    fixes = {m.group(1): m.group(2) for m in _RE_NU1605.finditer(output_text)}
    if not fixes:
        return False
    pkg_patterns = {
        pkg: re.compile(
            rf'(PackageReference\s[^>]*Include="{re.escape(pkg)}"[^>]*Version=")[^"]+(")'
        )
        for pkg in fixes
    }
    for csproj in plugin_dir.rglob("*.csproj"):
        content = csproj.read_text(encoding="utf-8")
        new_content = content
        for pkg, ver in fixes.items():
            new_content = pkg_patterns[pkg].sub(rf'\g<1>{ver}\2', new_content)
        if new_content != content:
            csproj.write_text(new_content, encoding="utf-8")
    return True


def dotnet_restore(plugin_dir):
    for _ in range(MAX_FIX_ITERATIONS):
        result = subprocess.run(
            ["dotnet", "restore"], cwd=plugin_dir, capture_output=True, text=True
        )
        if result.returncode == 0:
            return True
        combined = result.stdout + result.stderr
        if "NU1605" in combined and fix_nu1605(combined, plugin_dir):
            continue
        print(combined, file=sys.stderr)
        return False
    return False


def dotnet_build(plugin_dir):
    for _ in range(MAX_FIX_ITERATIONS):
        result = subprocess.run(
            ["dotnet", "build", "--no-restore"], cwd=plugin_dir, capture_output=True, text=True
        )
        if result.returncode == 0:
            return True, None
        combined = result.stdout + result.stderr
        if "NU1605" in combined and fix_nu1605(combined, plugin_dir):
            run(["dotnet", "restore"], cwd=plugin_dir)
            continue
        return False, combined
    return False, "max fix iterations reached"


def commit_push(plugin_dir):
    run(["git", "add", "-A"], cwd=plugin_dir)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--exit-code"], cwd=plugin_dir
    )
    if staged.returncode == 0:
        return False
    run(
        ["git", "commit", "-m", "Update Jellyfin NuGet packages to latest preview"],
        cwd=plugin_dir,
    )
    ssh_url = get_output(
        ["gh", "repo", "view", "--json", "sshUrl", "-q", ".sshUrl"], cwd=plugin_dir
    )
    run(["git", "remote", "set-url", "origin", ssh_url], cwd=plugin_dir)
    run(["git", "push", "origin", UNSTABLE_BRANCH, "--force-with-lease"], cwd=plugin_dir)
    return True


def create_pr(plugin_dir, repo, new_major):
    return get_output([
        "gh", "pr", "create",
        "--repo", repo,
        "--title", PR_TITLE,
        "--body", f"Update Jellyfin NuGet package version to `{new_major}.*-*`.",
        "--base", "master",
        "--head", UNSTABLE_BRANCH,
        "--draft",
    ], cwd=plugin_dir)


def process_plugin(plugin_dir, new_major, new_minor, tfm):
    name = plugin_dir.name
    print(f"\n{'#' * 60}\n{name}\n{'#' * 60}")

    init_submodule(name)
    branch_exists, pr_url, repo = check_unstable(plugin_dir)

    if branch_exists and pr_url:
        print(f"  Updating existing PR: {pr_url}")
        run(["git", "checkout", "-f", "-B", UNSTABLE_BRANCH, f"origin/{UNSTABLE_BRANCH}"], cwd=plugin_dir)
    else:
        if branch_exists:
            print("  Deleting stale unstable branch")
            run(["git", "push", "origin", "--delete", UNSTABLE_BRANCH], cwd=plugin_dir)
        run(["git", "checkout", "-f", "-B", "master", "origin/master"], cwd=plugin_dir)
        run(["git", "checkout", "-f", "-B", UNSTABLE_BRANCH, "master"], cwd=plugin_dir)

    update_jellyfin_packages(plugin_dir, new_major)
    update_build_yaml(plugin_dir, new_major, new_minor)

    dotnet_major = int(re.match(r"net(\d+)", tfm).group(1))
    update_dotnet_framework(plugin_dir, tfm)
    update_ms_packages(plugin_dir, dotnet_major)

    print("  Restoring...")
    if not dotnet_restore(plugin_dir):
        return "error", "restore failed"

    print("  Building...")
    ok, errors = dotnet_build(plugin_dir)
    if not ok:
        print(errors, file=sys.stderr)
        return "error", "build failed"
    print("  Build succeeded.")

    if not commit_push(plugin_dir):
        return "built", None

    if pr_url:
        return "updated", pr_url

    new_pr = create_pr(plugin_dir, repo, new_major)
    print(f"  Created PR: {new_pr}")
    return "created", new_pr


def main():
    parser = argparse.ArgumentParser(
        description="Create unstable branches for Jellyfin plugin submodules"
    )
    parser.add_argument(
        "plugin",
        nargs="?",
        help="Target a specific plugin (e.g. jellyfin-plugin-tvdb); defaults to all",
    )
    args = parser.parse_args()

    plugins = get_plugins(args.plugin)
    if not plugins:
        print("No plugin directories found", file=sys.stderr)
        sys.exit(1)

    ensure_nuget_source()
    new_major, new_minor, tfm = discover_version()
    print(f"Target Jellyfin version: {new_major}.{new_minor} ({tfm})")

    results = {"created": [], "updated": [], "built": [], "error": []}

    for plugin_dir in plugins:
        try:
            status, detail = process_plugin(plugin_dir, new_major, new_minor, tfm)
        except subprocess.CalledProcessError as e:
            status, detail = "error", str(e)
        results[status].append((plugin_dir.name, detail))

    print(f"\n{'=' * 60}\nSummary\n{'=' * 60}")
    for label, key in [
        ("PRs created", "created"),
        ("PRs updated", "updated"),
        ("Built (no changes)", "built"),
        ("Errors", "error"),
    ]:
        if results[key]:
            print(f"\n{label}:")
            for name, detail in results[key]:
                print(f"  {name}" + (f": {detail}" if detail else ""))


if __name__ == "__main__":
    main()
