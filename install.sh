#!/usr/bin/env bash
# Installs the harness skeleton. Safe to run again. Never prompts.
# HARNESS_REPO: URL or local path to clone.

resolved() {
  if [ -d "$1" ]; then (cd "$1" && pwd -P); else printf '%s' "${1%/}"; fi
}

main() {
  set -euo pipefail
  local repo="${HARNESS_REPO:-https://github.com/B0yko/harness-skeleton.git}"
  local core="$HOME/.harness/core"
  local fresh=0

  command -v git >/dev/null 2>&1 || { echo "harness: git is required" >&2; exit 1; }
  command -v python3 >/dev/null 2>&1 || { echo "harness: python3 (3.9+) is required" >&2; exit 1; }
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' ||
    { echo "harness: python3 is older than 3.9" >&2; exit 1; }

  if [ -e "$core/.git" ] && ! git -C "$core" rev-parse --verify -q HEAD >/dev/null 2>&1; then
    echo "harness: $core is a broken git clone (an interrupted download?). Move it away and run again." >&2
    exit 1
  elif [ -d "$core/.git" ] && ! git -C "$core" symbolic-ref -q HEAD >/dev/null; then
    echo "$core is on a detached commit, so it was left there."
  elif [ -d "$core/.git" ]; then
    local origin remote head
    origin="$(git -C "$core" remote get-url origin 2>/dev/null || true)"
    if [ -n "${HARNESS_REPO:-}" ] && [ -n "$origin" ] && [ "$(resolved "$HARNESS_REPO")" != "$(resolved "$origin")" ]; then
      echo "harness: $core pulls from $origin, not from $HARNESS_REPO. To switch, run:" >&2
      echo "  git -C \"$core\" remote set-url origin \"$HARNESS_REPO\"" >&2
      exit 1
    fi
    head="$(git -C "$core" rev-parse HEAD)"
    if remote="$(git -C "$core" ls-remote origin HEAD 2>/dev/null)" && [ -n "$remote" ]; then
      remote="${remote%%[[:space:]]*}"
      if [ "$remote" != "$head" ]; then
        if git -C "$core" fetch --quiet origin HEAD 2>/dev/null &&
          git -C "$core" merge --quiet --ff-only FETCH_HEAD 2>/dev/null; then
          [ "$(git -C "$core" rev-parse HEAD)" = "$head" ] || echo "Updated $core"
        else
          echo "harness: could not fast-forward $core (diverged or local changes). Installing what is there." >&2
        fi
      fi
    else
      echo "harness: could not reach origin of $core. Installing what is there." >&2
    fi
  elif [ -e "$core" ]; then
    echo "harness: $core exists and is not a git clone. Move it away and run again." >&2
    exit 1
  else
    (umask 077; mkdir -p "$HOME/.harness")
    local tmp="$core.clone.$$"
    git clone --quiet "$repo" "$tmp" || { rm -rf "$tmp"; rmdir "$HOME/.harness" 2>/dev/null || true; exit 1; }
    # A rename is atomic and refuses a non-empty folder, so of parallel runs exactly one wins.
    if python3 -c 'import os, sys; os.rename(sys.argv[1], sys.argv[2])' "$tmp" "$core" 2>/dev/null; then
      fresh=1
      echo "Cloned $repo to $core"
    else
      rm -rf "$tmp"
    fi
  fi

  local file
  for file in tools/harness.py core/hooks/gate.py core/settings.json core/rules/core.md \
    profile-template/profile.json; do
    if [ ! -f "$core/$file" ]; then
      echo "harness: $core is missing $file. To restore it, run: git -C \"$core\" checkout -- ." >&2
      exit 1
    fi
  done

  if ! python3 "$core/tools/harness.py" install; then
    if [ "$fresh" = 1 ]; then
      rm -rf "$core"
      rmdir "$HOME/.harness" 2>/dev/null || true
    fi
    exit 1
  fi

  echo
  echo "Next: run ~/.local/bin/harness doctor, then start a new Claude Code session."
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "Add ~/.local/bin to PATH to run plain \`harness\`." ;;
  esac
}

main "$@"
