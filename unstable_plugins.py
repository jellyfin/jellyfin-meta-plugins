#!/usr/bin/env python3
"""Create unstable branches for Jellyfin plugin submodules."""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).parent
NUGET_SOURCE_NAME = "jellyfin-pre"
NUGET_SOURCE_URL = "https://nuget.pkg.github.com/jellyfin/index.json"
UNSTABLE_BRANCH = "unstable"
PR_TITLE = "Unstable: Update to latest Jellyfin preview packages"
MAX_FIX_ITERATIONS = 10
IS_CI = os.environ.get("CI", "").lower() == "true"
ERROR_LOG_MAX_LINES = 200

REBASE_CONFLICTS = []

_RE_JELLYFIN_PKG = re.compile(
    r'(PackageReference\s[^>]*Include="Jellyfin\.[^"]*"[^>]*Version=")(\d+)\.\*-\*(")'
)
_RE_MS_PKG = re.compile(
    r'(PackageReference\s[^>]*Include="(?:Microsoft\.(?:AspNetCore|Extensions)|System)\.[^"]*"[^>]*Version=")[^"]+(")'
)
_RE_NU1605 = re.compile(
    r"NU1605:(?:\s*Warning As Error:)?\s*Detected package downgrade: ((?:Microsoft|System)\.[^\s]+) from ([\d.]+)"
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


def _feed_token():
    for var in ("NUGET_AUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    configs = [
        Path.home() / ".nuget" / "NuGet" / "NuGet.Config",
        REPO_ROOT / "NuGet.Config",
        REPO_ROOT / "nuget.config",
    ]
    for cfg in configs:
        if not cfg.is_file():
            continue
        try:
            root = ET.parse(cfg).getroot()
        except ET.ParseError:
            continue
        for entry in root.findall(f"./packageSourceCredentials/{NUGET_SOURCE_NAME}/add"):
            if entry.get("key") == "ClearTextPassword" and entry.get("value"):
                return entry.get("value")
    return get_output(["gh", "auth", "token"], check=False) or None


def _feed_get(url, token):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        # GitHub Packages ignores the basic-auth username, only the token matters.
        credential = base64.b64encode(f"jellyfin-bot:{token}".encode()).decode()
        request.add_header("Authorization", f"Basic {credential}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace").strip()
        hint = ""
        if e.code in (401, 403):
            hint = "\nThe token is missing or lacks the 'read:packages' scope."
        raise RuntimeError(
            f"GET {url} failed: HTTP {e.code} {e.reason}{hint}\n{body}"
        ) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"GET {url} failed: {e.reason}") from None


def _version_key(version):
    core, _, prerelease = version.partition("-")
    core = core.split("+")[0]
    prerelease = prerelease.split("+")[0]
    numbers = [int(p) if p.isdigit() else 0 for p in core.split(".")]
    numbers += [0] * (4 - len(numbers))
    if not prerelease:
        # A release outranks every prerelease of the same core version.
        return (numbers, 1, [])
    identifiers = [
        (0, int(i), "") if i.isdigit() else (1, 0, i)
        for i in prerelease.split(".")
    ]
    return (numbers, 0, identifiers)


def discover_version():
    package_id = "Jellyfin.Controller"
    token = _feed_token()
    index = _feed_get(NUGET_SOURCE_URL, token)
    base_address = next(
        (r["@id"] for r in index.get("resources", [])
         if r.get("@type", "").startswith("PackageBaseAddress")),
        None,
    )
    if not base_address:
        raise RuntimeError(
            f"{NUGET_SOURCE_NAME} feed exposes no PackageBaseAddress resource"
        )
    url = f"{base_address.rstrip('/')}/{package_id.lower()}/index.json"
    versions = _feed_get(url, token).get("versions") or []
    if not versions:
        raise RuntimeError(f"{package_id} not found in {NUGET_SOURCE_NAME} feed")
    version = max(versions, key=_version_key)
    parts = version.split(".")
    return int(parts[0]), int(parts[1]), _get_tfm(package_id, version)


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


def rebase_onto_master(plugin_dir):
    result = subprocess.run(
        ["git", "rebase", "origin/master"], cwd=plugin_dir, capture_output=True, text=True
    )
    if result.returncode == 0:
        return True
    print(result.stdout + result.stderr, file=sys.stderr)
    run(["git", "rebase", "--abort"], cwd=plugin_dir, check=False)
    return False


def branch_moved(plugin_dir):
    head = get_output(["git", "rev-parse", "HEAD"], cwd=plugin_dir)
    remote = get_output(["git", "rev-parse", f"origin/{UNSTABLE_BRANCH}"], cwd=plugin_dir)
    return head != remote


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
    last_err = None
    for _ in range(MAX_FIX_ITERATIONS):
        result = subprocess.run(
            ["dotnet", "restore"], cwd=plugin_dir, capture_output=True, text=True
        )
        if result.returncode == 0:
            return True, None
        combined = result.stdout + result.stderr
        last_err = combined
        if "NU1605" in combined and fix_nu1605(combined, plugin_dir):
            continue
        return False, combined
    return False, last_err or "max fix iterations reached"


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


def commit_push(plugin_dir, failing=False):
    run(["git", "add", "-A"], cwd=plugin_dir)
    has_changes = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--exit-code"], cwd=plugin_dir
    ).returncode != 0
    if not has_changes and not failing:
        return False
    msg = "Update Jellyfin NuGet packages to latest preview"
    commit_cmd = ["git", "commit", "-m", f"[build-failing] {msg}" if failing else msg]
    if not has_changes:
        commit_cmd.append("--allow-empty")
    run(commit_cmd, cwd=plugin_dir)
    push_branch(plugin_dir)
    return True


def push_branch(plugin_dir):
    # gh defaults to SSH; HTTPS pushes fail without credentials.
    ssh_url = get_output(
        ["gh", "repo", "view", "--json", "sshUrl", "-q", ".sshUrl"], cwd=plugin_dir
    )
    run(["git", "remote", "set-url", "origin", ssh_url], cwd=plugin_dir)
    run(["git", "push", "origin", UNSTABLE_BRANCH, "--force-with-lease"], cwd=plugin_dir)


def _format_errors_section(errors):
    if not errors:
        return ""
    lines = errors.strip().splitlines()
    truncated = len(lines) > ERROR_LOG_MAX_LINES
    if truncated:
        lines = lines[-ERROR_LOG_MAX_LINES:]
    body = "\n".join(lines)
    note = f" (truncated, last {ERROR_LOG_MAX_LINES} lines)" if truncated else ""
    return f"\n\n## Build errors{note}\n\n```\n{body}\n```\n"


def _format_rebase_section(rebase_conflict):
    if not rebase_conflict:
        return ""
    return (
        "\n\n## Rebase conflict\n\n"
        "Rebasing this branch onto `master` failed with conflicts, so it is still "
        "based on an older `master`. Resolve the conflicts and rebase manually.\n"
    )


def _build_pr_body(new_major, errors=None, rebase_conflict=False):
    return (
        f"Update Jellyfin NuGet package version to `{new_major}.*-*`."
        + _format_rebase_section(rebase_conflict)
        + _format_errors_section(errors)
    )


def create_pr(plugin_dir, repo, new_major, errors=None):
    return get_output([
        "gh", "pr", "create",
        "--repo", repo,
        "--title", PR_TITLE,
        "--body", _build_pr_body(new_major, errors),
        "--base", "master",
        "--head", UNSTABLE_BRANCH,
        "--draft",
    ], cwd=plugin_dir)


def update_pr_body(plugin_dir, pr_url, body):
    run(["gh", "pr", "edit", pr_url, "--body", body], cwd=plugin_dir)


def process_plugin(plugin_dir, new_major, new_minor, tfm):
    name = plugin_dir.name
    print(f"\n{'#' * 60}\n{name}\n{'#' * 60}")

    init_submodule(name)
    branch_exists, pr_url, repo = check_unstable(plugin_dir)
    rebase_conflict = False
    rebased = False

    if branch_exists and pr_url:
        print(f"  Updating existing PR: {pr_url}")
        run(["git", "checkout", "-f", "-B", UNSTABLE_BRANCH, f"origin/{UNSTABLE_BRANCH}"], cwd=plugin_dir)
        print("  Rebasing onto master...")
        if rebase_onto_master(plugin_dir):
            rebased = branch_moved(plugin_dir)
        else:
            print("  Rebase conflicted; continuing without rebasing", file=sys.stderr)
            REBASE_CONFLICTS.append(name)
            rebase_conflict = True
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
    ok, errors = dotnet_restore(plugin_dir)
    if not ok:
        print(errors, file=sys.stderr)
        if IS_CI:
            return _push_failing(
                plugin_dir, repo, new_major, pr_url, errors, "restore failed", rebase_conflict
            )
        return "error", "restore failed"

    print("  Building...")
    ok, errors = dotnet_build(plugin_dir)
    if not ok:
        print(errors, file=sys.stderr)
        if IS_CI:
            return _push_failing(
                plugin_dir, repo, new_major, pr_url, errors, "build failed", rebase_conflict
            )
        return "error", "build failed"
    print("  Build succeeded.")

    committed = commit_push(plugin_dir)
    if not committed and rebased:
        print("  Pushing rebased branch...")
        push_branch(plugin_dir)

    # Always rebuild the body so a clean run clears a previous run's warnings.
    if pr_url:
        update_pr_body(
            plugin_dir, pr_url, _build_pr_body(new_major, rebase_conflict=rebase_conflict)
        )
        if committed:
            return "updated", pr_url
        return ("rebased" if rebased else "built"), pr_url

    if not committed:
        return "built", None

    new_pr = create_pr(plugin_dir, repo, new_major)
    print(f"  Created PR: {new_pr}")
    return "created", new_pr


def _push_failing(plugin_dir, repo, new_major, pr_url, errors, reason, rebase_conflict=False):
    del reason  # commit_push(failing=True) always pushes (empty commit if needed)
    commit_push(plugin_dir, failing=True)
    body = _build_pr_body(new_major, errors, rebase_conflict)
    if pr_url:
        update_pr_body(plugin_dir, pr_url, body)
        print(f"  Pushed [build-failing] commit to existing PR: {pr_url}")
        return "pushed_failing", pr_url
    new_pr = create_pr(plugin_dir, repo, new_major, errors=errors)
    print(f"  Created [build-failing] PR: {new_pr}")
    return "pushed_failing", new_pr


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

    results = {
        "created": [], "updated": [], "rebased": [], "built": [], "pushed_failing": [], "error": []
    }

    for plugin_dir in plugins:
        try:
            status, detail = process_plugin(plugin_dir, new_major, new_minor, tfm)
        except subprocess.CalledProcessError as e:
            status, detail = "error", str(e)
        except Exception as e:  # keep processing the remaining plugins
            traceback.print_exc()
            status, detail = "error", f"{type(e).__name__}: {e}"
        results[status].append((plugin_dir.name, detail))

    print(f"\n{'=' * 60}\nSummary\n{'=' * 60}")
    for label, key in [
        ("PRs created", "created"),
        ("PRs updated", "updated"),
        ("Rebased onto master (no other changes)", "rebased"),
        ("Built (no changes)", "built"),
        ("Pushed with failing build", "pushed_failing"),
        ("Errors", "error"),
    ]:
        if results[key]:
            print(f"\n{label}:")
            for name, detail in results[key]:
                print(f"  {name}" + (f": {detail}" if detail else ""))

    if REBASE_CONFLICTS:
        print("\nRebase onto master conflicted (resolve manually):")
        for name in REBASE_CONFLICTS:
            print(f"  {name}")


if __name__ == "__main__":
    main()
