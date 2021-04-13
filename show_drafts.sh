#!/bin/bash

rstrip_ver() {
    if [ -z "$1" ]; then
        grep -Po "^.*?(?=(\.0\.0\.0|\.0\.0|\.0)?$)"
    else
        echo $1 | grep -Po "^.*?(?=(\.0\.0\.0|\.0\.0|\.0)?$)"
    fi
}

esc=$(printf '\033')
colorize_changelog() {
    #1 pr number
    #2 list char
    #3 pr submitter
    sed -r -e "s/\\((#[0-9]+)\)/(${esc}[32m\1${esc}[0m)/g" \
        -e "s/^\s*[*+-]\s*/  ${esc}[1;30m-${esc}[0m /g" \
        -e "s/@([^ ]+)$/${esc}[1;30m@${esc}[0;36m\1${esc}[0m/gi"
}


IFS=$'\n'
for name in $(git submodule foreach --quiet 'basename $PWD'); do
    pushd $name > /dev/null
        draft="$(hub release --include-drafts -L2 -f 'SOF:%S%n%T%n%t%n%U%n%b%nEOF%n' --color=always | sed -n -e '/SOF:draft/,/EOF/p' | sed -e '1d' -e '$d')"

        if [ ! -z "$draft" ]; then
            IFS=$'\n' parts=(${draft})

            tag="${parts[0]}"
            title="${parts[1]}"
            url="${parts[2]}"
            IFS=$'\n' description="${parts[*]:3}"

            echo
            echo -e "\e[1m$name\e[0m: $title\n  \e[34m$url\e[0m"

            changes=$(echo "$description" | grep -Po '^\s*\K\*.*$' )

            meta_version=$(grep -Po "version: ?[\"']?\K.*(?=[\"']$)" build.yaml | rstrip_ver)
            draft_version=$(echo $tag | grep -Po 'v\K.*' | rstrip_ver)

            if [ "$meta_version" != "$draft_version" ]; then
                echo -e "  \e[1;31mVersion bump needed!"
                echo -e "  \e[0;31mDraft is for v\e[0m\e[1m$draft_version\e[0;31m, but build.yaml specifies v\e[0m\e[1m$meta_version\e[0m"
            fi

            echo "$changes" | colorize_changelog
        fi
    popd > /dev/null
done
