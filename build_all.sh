#!/bin/bash
#set -x
MY=$(dirname $(realpath -s "${0}"))

export ARTIFACT_DIR="${MY}/artifacts"
mkdir -p "${ARTIFACT_DIR}"

DEFAULT_REPO_DIR="${MY}/test_repo"
DEFAULT_REPO_URL="http://localhost:8080"

export JELLYFIN_REPO=${JELLYFIN_REPO:-$DEFAULT_REPO_DIR}
export JELLYFIN_REPO_URL=${JELLYFIN_REPO_URL:-$DEFAULT_REPO_URL}

export VERSION_SUFFIX=$(date -u +%y%m.%d%H.%M%S)

FAILED=()

for plugin in $(find . -maxdepth 1 -mindepth 1 -type d -name 'jellyfin-plugin-*' | sort); do
  name=$(basename $plugin)
  if [ "$name" = "jellyfin-plugin-meta" ]; then
    continue
  fi
  pushd $plugin > /dev/null
    echo -e "\n##### ${name} #####"

    bash $MY/build_plugin.sh || {
      FAILED+=("$name")
    }

  popd > /dev/null
done

if [ ! ${#FAILED[@]} -eq 0 ]; then
  echo -e "\n\nThe following plugins failed to compile:" > /dev/stderr
  for plugin in "${FAILED[@]}"; do
    echo " - $plugin" > /dev/stderr
  done

  exit 1
fi
