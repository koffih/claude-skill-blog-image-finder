#!/usr/bin/env bash
# Install or update the blog-image-finder skill for Claude Code.
#
#   curl -fsSL https://raw.githubusercontent.com/koffih/claude-skill-blog-image-finder/main/install.sh | bash
#
# Options (after `bash -s --` when piped):
#   --project     install into ./.claude/skills of the current directory
#   --uninstall   remove the installed skill
#   -h, --help    show this help
#
# The skill is a git clone, so running this again updates it (fast-forward only).
# Set SKILLS_DIR to install somewhere else than ~/.claude/skills.
#
# Shared folders: when the skills folder belongs to another account (for
# example /opt/<project>/.claude used by both root and dev), root works in it
# as that account. Root never runs git in a tree another user can write to,
# and the owner keeps managing its own skills.
set -euo pipefail

NAME="blog-image-finder"
REPO="koffih/claude-skill-blog-image-finder"
URL="https://github.com/$REPO.git"

BASE="${SKILLS_DIR:-$HOME/.claude/skills}"
MODE="install"

usage() {
  cat <<USAGE
Install or update the $NAME skill for Claude Code.

  install.sh              install into ~/.claude/skills (or \$SKILLS_DIR), or update it
  install.sh --project    install into ./.claude/skills of the current directory
  install.sh --uninstall  remove it
USAGE
}

for arg in "$@"; do
  case "$arg" in
    --project)   BASE="$PWD/.claude/skills" ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# Work on the real path, symlinks resolved: another account cannot go through
# /root to reach /root/.claude -> /opt/<project>/.claude, but can reach /opt.
probe="$BASE"
rest=""
while [ ! -e "$probe" ] && [ "$probe" != / ]; do
  rest="/$(basename "$probe")$rest"
  probe="$(dirname "$probe")"
done
real="$(readlink -f "$probe")"
BASE="${real%/}$rest"
TARGET="$BASE/$NAME"

# Owner of the skills folder, or of its closest existing parent.
OWNER="$(stat -c %U "$real")"
GROUP="$(stat -c %G "$real")"

AS=""
if [ "$(id -u)" = 0 ] && [ "$OWNER" != root ]; then
  command -v runuser >/dev/null 2>&1 || { echo "runuser is required to install into $BASE, owned by $OWNER." >&2; exit 1; }
  AS="$OWNER"
fi

# Run a command as the owner of the skills folder when root works in someone else's folder.
as_owner() {
  if [ -n "$AS" ]; then
    runuser -u "$AS" -- env HOME="$(getent passwd "$AS" | cut -d: -f6)" "$@"
  else
    "$@"
  fi
}

# Read the origin straight from the file: no git command runs inside a tree we may not own.
is_our_clone() {
  [ -d "$TARGET/.git" ] && [ ! -L "$TARGET" ] &&
    git -C / config --file "$TARGET/.git/config" --get remote.origin.url 2>/dev/null |
      grep -qi "github.com[:/]$REPO\(\.git\)\{0,1\}$"
}

# A clone root left in another account's folder is handed over to that account.
if [ -n "$AS" ] && is_our_clone && [ "$(stat -c %U "$TARGET")" = root ]; then
  chown -R "$OWNER:$GROUP" -- "$TARGET"
  echo "handed:    $TARGET now belongs to $OWNER"
fi

# Someone else manages this folder (shared by several accounts).
if [ "$(id -u)" != 0 ] && [ -d "$TARGET" ] && [ ! -L "$TARGET" ] && [ "$(stat -c %u "$TARGET")" != "$(id -u)" ]; then
  manager="$(stat -c %U "$TARGET")"
  if [ ! -r "$TARGET/.git/config" ] || [ ! -r "$TARGET/SKILL.md" ]; then
    echo "$TARGET belongs to $manager and is not readable by $(id -un): run this installer once as root to hand it over." >&2
    exit 1
  fi
  if is_our_clone; then
    echo "managed:   $TARGET is kept up to date by $manager (shared folder)"
    exit 0
  fi
fi

if [ "$MODE" = uninstall ]; then
  if [ ! -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
    echo "not installed: $TARGET"
    exit 0
  fi
  if ! is_our_clone; then
    echo "refusing to remove $TARGET: it is not a clone of $REPO. Remove it by hand if intended." >&2
    exit 1
  fi
  as_owner rm -rf -- "$TARGET"
  echo "removed: $TARGET"
  exit 0
fi

command -v git >/dev/null 2>&1 || { echo "git is required but not installed." >&2; exit 1; }

if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
  if ! is_our_clone; then
    echo "refusing to overwrite $TARGET: it exists and is not a clone of $REPO." >&2
    echo "Move or remove it, then run this again." >&2
    exit 1
  fi
  if as_owner git -C "$TARGET" diff --quiet && as_owner git -C "$TARGET" diff --cached --quiet; then
    :
  else
    rc=$?
    if [ "$rc" = 1 ]; then
      echo "local changes in $TARGET: not updating, to avoid losing them." >&2
    else
      echo "git could not read $TARGET (see the message above)." >&2
    fi
    exit 1
  fi
  as_owner git -C "$TARGET" pull --ff-only --quiet
  echo "updated:   $TARGET ($(as_owner git -C "$TARGET" rev-parse --short HEAD))"
else
  as_owner mkdir -p "$BASE"
  as_owner git clone --quiet --depth 1 "$URL" "$TARGET"
  echo "installed: $TARGET ($(as_owner git -C "$TARGET" rev-parse --short HEAD))"
fi

[ -n "$AS" ] && echo "as:        $AS, owner of $BASE"

# Verify instead of promising.
if grep -q "^name: $NAME$" "$TARGET/SKILL.md" 2>/dev/null; then
  echo "verified:  SKILL.md is readable and named $NAME"
else
  echo "installation looks broken: $TARGET/SKILL.md missing or misnamed." >&2
  exit 1
fi

echo
echo "Start a new Claude Code session, then run:  /$NAME"
