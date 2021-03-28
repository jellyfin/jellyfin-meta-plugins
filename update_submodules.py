#!/usr/bin/env python3
import os
import subprocess
import requests
import time


try:
    subprocess.run(["git", "diff", "--staged", "--quiet", "--exit-code"], check=True)
except subprocess.SubprocessError:
    print("Error: You have staged changes!")
    exit(1)

available = []
fetched = []
failed = []
removed = []
added = []


def update(_name, url=None):
    if not os.path.exists(_name):
        print("Adding {} @ {}".format(_name, url))
        subprocess.run(["git", "submodule", "add", "--force", "-b", "master", url, _name], check=True)
        added.append(_name)
    subprocess.run(["git", "submodule", "update", "--init", "--remote", "--checkout", "--force", _name], check=True)

    subprocess.run(["git", "fetch", "--all", "--tags"], cwd=_name, check=True)
    subprocess.run(["git", "checkout", "-f", "-B", "master", "origin/master"], cwd=_name, check=True)
    fetched.append(_name)


def remove(_name):
    if os.path.exists(_name):
        print("Removing {}".format(_name))
        subprocess.run(["git", "rm", _name], check=True)
        removed.append(_name)


page_num = 1
per_page = 100
PAGINATION_URL = "https://api.github.com/orgs/jellyfin/repos?sort=created&per_page={per}&page={page}"

next = PAGINATION_URL.format(per=per_page, page=page_num)

while next:
    resp = requests.get(next)
    repos = resp.json()

    page_num += 1
    next = PAGINATION_URL.format(per=per_page, page=page_num)
    if len(repos) < per_page:
        next = None

    for repo in repos:
        _name = repo.get("name")
        url = repo.get("clone_url")
        if _name.startswith("jellyfin-plugin-"):
            available.append(_name)
            try:
                update(_name, url)
                pass
            except Exception as e:
                failed.append((_name, e))


for repo in os.listdir("."):
    if repo in fetched:
        continue

    if not os.path.isdir(repo):
        continue

    if not repo.startswith("jellyfin-plugin-"):
        continue

    try:
        if not repo in available:
            remove(repo)
    except Exception as e:
        failed.append((repo, e))


if failed:
    print("The following repositories failed to update:")
    for repo, e in failed:
        print(repo, e)

commit_message = ["Updated plugin submodules"]

if added:
    commit_message.append("")
    commit_message.append("Added")
    commit_message.append("-----")
    commit_message.append("")
    for plugin in added:
        commit_message.append("- {}".format(plugin))

if removed:
    commit_message.append("")
    commit_message.append("Removed")
    commit_message.append("-------")
    commit_message.append("")
    for plugin in removed:
        commit_message.append("- {}".format(plugin))

subprocess.run(["git", "commit", "-m", "\n".join(commit_message)])
