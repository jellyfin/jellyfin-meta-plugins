#!/usr/bin/env python3

# checkout_unstable.py - Checkout submodules for a build (checkout unstable)
#
# Part of the Jellyfin CI system
###############################################################################

from datetime import datetime
import os.path
from subprocess import run, PIPE
import contextlib
from git import Repo

@contextlib.contextmanager
def pushd(new_dir):
    previous_dir = os.getcwd()
    os.chdir(new_dir)
    try:
        yield
    finally:
        os.chdir(previous_dir)

target_head = "origin/unstable"
print(f"Preparing targets for {target_head}")

# Determine top level directory of this repository ("jellyfin-meta-plugins")
revparse = run(["git", "rev-parse", "--show-toplevel"], stdout=PIPE)
revparse_dir = revparse.stdout.decode().strip()

# Prepare repo object for this repository
this_repo = Repo(revparse_dir)

# Update all the submodules
while True:
    try:
        this_repo.submodule_update(init=True, recursive=True)
        break
    except Exception as e:
        print(e)
        pass

# Prepare a dictionary form of the submodules so we can reference them by name
submodules = dict()
for submodule in this_repo.submodules:
    submodules[submodule.name] = submodule.module()

plugin_version = datetime.now().strftime("%Y.%m.%d%H%M")

for submodule in submodules.keys():
    try:
        # Checkout the given head and reset the working tree
        submodules[submodule].head.reference = target_head
        submodules[submodule].head.reset(index=True, working_tree=True)
        sha = submodules[submodule].head.object.hexsha
        date = datetime.fromtimestamp(submodules[submodule].head.object.committed_date)
        print(f"Submodule {submodule} now at commit {sha} ({date})")

        with pushd(submodule):
            # Update Jellyfin dependencies
            update_command = "dotnet outdated -pre Always -u -inc Jellyfin"
            print(f"{submodule} >> {update_command}")
            os.system(update_command)

            # Build with jprm
            if not os.path.exists("artifacts"):
                os.makedirs("artifacts")

            jprm_command = f"jprm plugin build --version {plugin_version}"
            print(f"{submodule} >> {jprm_command}")
            os.system(jprm_command)

            # TODO upload to repo server and update manifest

    except Exception as e:
        print(e)
        pass

print(f"Successfully checked out submodules to ref {target_head}")