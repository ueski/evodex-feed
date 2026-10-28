#!/usr/bin/env bash
# The feed's state between daily runs (manifest.json, history/, daily/) lives on the
# `data` branch of this repository. GitHub Pages only serves a copy of it.
#
#   scripts/state.sh pull <dir> [<feed-url>]
#       Empties <dir>, fills it with the data branch's files and prints where the previous
#       feed is, for `publish_feed.py --previous "$PREVIOUS"` / `validate_feed.py` (also
#       written to $GITHUB_OUTPUT as `previous=` when that is set):
#         data branch exists                    <dir>
#         no data branch, live feed answers 200 <feed-url>  (recovery: the state is read back
#                                               from the site, then pushed as the new branch)
#         no data branch, live feed answers 404 (empty)     first run: bootstrap
#         anything else                         exit 1: never guess, a wrong guess wipes history
#
#   scripts/state.sh push <site-dir> <message>
#       Commits <site-dir>/{manifest.json,history,daily} as the next commit of the data
#       branch and pushes it (fast-forward only). Uses a temporary index, so the checkout
#       of main is never touched.
#
# Environment: STATE_REMOTE (default origin), STATE_BRANCH (default data).
set -euo pipefail

REMOTE="${STATE_REMOTE:-origin}"
BRANCH="${STATE_BRANCH:-data}"
REF="refs/remotes/$REMOTE/$BRANCH"
UA="EvodexFeedPublisher/1.0 (+https://github.com/ueski/evodex-feed)"

log() { echo "[state] $*" >&2; }
die() { log "error: $*"; exit 1; }

pull() {
    local dir="$1" url="${2:-}" rc=0 previous="" code
    rm -rf "$dir"
    mkdir -p "$dir"
    git ls-remote --exit-code --heads "$REMOTE" "$BRANCH" >/dev/null || rc=$?
    case "$rc" in
        0)
            git fetch --quiet --no-tags --depth=1 "$REMOTE" "+refs/heads/$BRANCH:$REF"
            git archive --format=tar "$REF" | tar -x -C "$dir"
            [ -f "$dir/manifest.json" ] || die "the $BRANCH branch has no manifest.json"
            previous="$dir"
            log "previous feed: $BRANCH branch at $(git rev-parse --short "$REF"), $(du -sh "$dir" | cut -f1)"
            ;;
        2)
            git update-ref -d "$REF" 2>/dev/null || true
            if [ -n "$url" ]; then
                code="$(curl -sS -o /dev/null -w '%{http_code}' --retry 3 --retry-delay 5 -A "$UA" \
                    "${url%/}/manifest.json")" || die "cannot reach ${url%/}/manifest.json"
                case "$code" in
                    404) log "no $BRANCH branch and no live feed at $url: first run (bootstrap)" ;;
                    200) previous="${url%/}/"
                         log "no $BRANCH branch but a live feed at $url: reading the state back from it" ;;
                    *)   die "no $BRANCH branch and ${url%/}/manifest.json answered HTTP $code" ;;
                esac
            else
                log "no $BRANCH branch: first run (bootstrap)"
            fi
            ;;
        *)
            die "git ls-remote $REMOTE failed (exit $rc)"
            ;;
    esac
    if [ -n "${GITHUB_OUTPUT:-}" ]; then
        echo "previous=$previous" >> "$GITHUB_OUTPUT"
    fi
    echo "$previous"
}

push() {
    local site="$1" message="$2" tmp tree parent="" commit
    [ -f "$site/manifest.json" ] || die "$site/manifest.json missing"
    [ -d "$site/history" ] && [ -d "$site/daily" ] || die "$site has no history/ or daily/"
    tmp="$(mktemp -d)"
    GIT_INDEX_FILE="$tmp/index" git --work-tree="$site" add --force -- manifest.json history daily
    tree="$(GIT_INDEX_FILE="$tmp/index" git write-tree)"
    rm -rf "$tmp"
    if git rev-parse --verify --quiet "$REF^{commit}" >/dev/null; then
        parent="$(git rev-parse "$REF")"
        if [ "$(git rev-parse "$parent^{tree}")" = "$tree" ]; then
            log "state unchanged, nothing to push"
            return 0
        fi
    fi
    export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-$(git config user.name || echo 'github-actions[bot]')}"
    export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-$(git config user.email || echo '41898282+github-actions[bot]@users.noreply.github.com')}"
    export GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-$GIT_AUTHOR_NAME}"
    export GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-$GIT_AUTHOR_EMAIL}"
    if [ -n "$parent" ]; then
        commit="$(git commit-tree "$tree" -p "$parent" -m "$message")"
    else
        commit="$(git commit-tree "$tree" -m "$message")"
    fi
    # Fast-forward only: if anything else moved the branch since `pull`, fail instead of overwriting.
    git push --quiet "$REMOTE" "$commit:refs/heads/$BRANCH"
    git update-ref "$REF" "$commit"
    log "pushed $BRANCH $(git rev-parse --short "$commit"): $message"
}

case "${1:-}" in
    pull) [ $# -ge 2 ] || die "usage: $0 pull <dir> [<feed-url>]"; pull "$2" "${3:-}" ;;
    push) [ $# -eq 3 ] || die "usage: $0 push <site-dir> <message>"; push "$2" "$3" ;;
    *) die "usage: $0 pull <dir> [<feed-url>] | push <site-dir> <message>" ;;
esac
