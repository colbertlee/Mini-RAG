#!/usr/bin/env bash
#
# 一键发布 Mini-RAG：tag → push → gh release（全自动）
#
# 用法:
#   ./scripts/release.sh                 # 交互式输入版本号 + release notes
#   ./scripts/release.sh v0.2.0          # 直接指定版本号，notes 从 _build/RELEASE_NOTES_v0.2.0.md
#   ./scripts/release.sh v0.2.0 --notes ./path/to/notes.md   # 指定 notes 文件
#
# 前置条件（已在本机配好，见 ~/.workbuddy/skills/github-cn-network-gh）:
#   - git remote origin = git@github.com:colbertlee/Mini-RAG.git (SSH over 443)
#   - gh CLI 可用: shim 在 ~/.local/bin/gh，GH_CONFIG_DIR 已指向 Roaming（已认证 colbertlee）
#   - _build/RELEASE_NOTES_<ver>.md 存在（或用 --notes 指定）
#
# 流程: 1) 校验工作树干净 2) annotated tag 3) push main + tags
#       4) gh release create（带 notes） 5) 汇总输出
set -euo pipefail

REPO="colbertlee/Mini-RAG"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NOTES_FLAG=""
VER=""

# ---- 解析参数 ----
while [[ $# -gt 0 ]]; do
    case "$1" in
        --notes) NOTES_FLAG="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        --*) echo "未知参数: $1" >&2; exit 1 ;;
        *) VER="$1"; shift ;;
    esac
done

# ---- gh 定位 ----
GH="${GH:-}"                       # 允许外部覆盖
if [[ -z "$GH" ]]; then
    for c in ~/.local/bin/gh gh; do
        if command -v "$c" >/dev/null 2>&1; then GH="$c"; break; fi
    done
fi
if [[ -z "$GH" ]]; then
    echo "❌ 找不到 gh。请先按 skill github-cn-network-gh 配置（~/.local/bin/gh shim）。" >&2
    exit 1
fi

echo "⚙️  gh: $GH"
"$GH" auth status >/dev/null 2>&1 || { echo "❌ gh 未认证。请先 auth login。" >&2; exit 1; }

# ---- 版本号（缺省则基于最新 tag 自动推导 patch+1）----
if [[ -z "$VER" ]]; then
    LATEST="$(git tag --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)"
    if [[ -z "$LATEST" ]]; then
        echo "❌ 无现有 tag，无法自动推导版本号。请显式传入，如: $0 v0.1.0" >&2
        exit 1
    fi
    # v0.1.0 -> 0.1.1 -> v0.1.1
    BASE="${LATEST#v}"
    IFS='.' read -r MAJ MIN PAT <<< "$BASE"
    VER="v${MAJ}.${MIN}.$((PAT + 1))"
    echo "ℹ️  最新 tag ${LATEST} → 自动推导 ${VER}（如需改，传参覆盖）"
fi
[[ "$VER" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "❌ 版本号格式应为 vX.Y.Z，收到: $VER" >&2; exit 1; }
TAGNAME="${VER#v}"                 # 去掉 v 用于标题/文件名，保持 v 用于 git tag 更稳可省

# ---- 校验：tag 未占用 ----
if git rev-parse "$VER" >/dev/null 2>&1; then
    echo "❌ tag $VER 已存在。想重发请先删除本地+远程，或用 --notes 复用。" >&2
    exit 1
fi

# ---- 校验：工作树干净（防漏提交）----
if ! git diff --quiet HEAD; then
    echo "❌ 工作树有未提交改动。请先 commit，或 git stash。" >&2
    git status -sb >&2
    exit 1
fi

# ---- release notes 定位 ----
NOTES="${NOTES_FLAG:-$ROOT/_build/RELEASE_NOTES_${VER}.md}"
if [[ ! -f "$NOTES" ]]; then
    echo "❌ Release notes 不存在: $NOTES" >&2
    echo "   请先写 _build/RELEASE_NOTES_${VER}.md，或用 --notes 指定。" >&2
    exit 1
fi

echo ""
echo "🚀 ===== 开始发布 Mini-RAG ${VER} → $REPO ====="
echo "📋  Tag:       $VER"
echo "📄  Notes:     $NOTES"
echo "📡  gh:        $GH"
echo ""

# ---- 1) annotated tag ----
echo "[1/4] 打 annotated tag: $VER"
git tag -a "$VER" -m "Mini-RAG ${VER}" 

# ---- 2) push main + tags ----
echo "[2/4] push main + tags → origin（SSH over 443）..."
git push origin main "$VER"

# ---- 3) gh release create ----
echo "[3/4] 创建 GitHub Release ..."
TITLE="Mini-RAG ${VER}"
# 从 notes 首行 `# Mini-RAG v0.1.0` 提取副标题，保留完整标题
"$GH" release create "$VER" \
    --title "$TITLE" \
    --notes-file "$NOTES" \
    --repo "$REPO"

# ---- 4) 汇总 ----
echo ""
echo "[4/4] ✅ 发布完成！"
"$GH" release view "$VER" --repo "$REPO" --json tagName,name,url 2>/dev/null | python -c \
    "import sys,json; d=json.load(sys.stdin); print(f\"   tag : {d['tagName']}\"); print(f\"   标题: {d['name']}\"); print(f\"   URL : {d['url']}\")" 2>/dev/null \
    || { echo "   (release 已创建，view 细节略)"; }
echo ""
echo "🎉 Mini-RAG ${VER} 已发布: https://github.com/$REPO/releases/tag/$VER"
