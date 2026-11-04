#!/usr/bin/env bash
# Install the pr-review skill for OpenCode, Claude Code and Codex.
#
#   integrations/install.sh <project-dir>     into one project
#   integrations/install.sh --global          for your user account
#
# OpenCode and Codex read .agents/skills. Claude Code reads .claude/skills, which gets a symlink, so
# there is one copy to update. Existing files named pr-review are replaced; nothing else is touched.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
target="${1:-}"
[ -n "$target" ] || { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

if [ "$target" = "--global" ]; then
  skills="$HOME/.agents/skills"
  claude_skills="$HOME/.claude/skills"
  opencode_agents="$HOME/.config/opencode/agents"
  claude_agents="$HOME/.claude/agents"
  codex_agents="${CODEX_HOME:-$HOME/.codex}/agents"
else
  [ -d "$target" ] || { echo "no such directory: $target" >&2; exit 2; }
  target="$(cd "$target" && pwd)"
  skills="$target/.agents/skills"
  claude_skills="$target/.claude/skills"
  opencode_agents="$target/.opencode/agents"
  claude_agents="$target/.claude/agents"
  codex_agents="$target/.codex/agents"
fi

mkdir -p "$skills" "$claude_skills" "$opencode_agents" "$claude_agents" "$codex_agents"
rm -rf "$skills/pr-review"
cp -R "$here/skill/pr-review" "$skills/pr-review"
find "$skills/pr-review" -name '__pycache__' -type d -prune -exec rm -rf {} +
if [ ! -L "$claude_skills/pr-review" ] && [ -e "$claude_skills/pr-review" ]; then rm -rf "$claude_skills/pr-review"; fi
# Relative, so a project can commit it and a teammate gets a working link.
ln -sfn "../../.agents/skills/pr-review" "$claude_skills/pr-review"
cp "$here/integrations/opencode/agents/pr-review-worker.md" "$opencode_agents/"
cp "$here/integrations/claude/agents/pr-review-worker.md" "$claude_agents/"
cp "$here/integrations/codex/agents/pr-review-worker.toml" "$codex_agents/"

echo "Installed:"
echo "  skill        $skills/pr-review (OpenCode, Codex), $claude_skills/pr-review (Claude Code)"
echo "  worker agent $opencode_agents, $claude_agents, $codex_agents"
echo
echo "Use it:"
echo "  OpenCode     ask: load the pr-review skill and review this branch against main"
echo "  Claude Code  /pr-review review this branch against main"
echo "  Codex        \$pr-review review this branch against main"
