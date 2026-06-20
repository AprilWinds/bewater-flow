#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<EOF
bewater-flow 安装

用法:
  ./install.sh [<目标项目目录>] [--claude|--codebuddy|--lingma]

  目标目录默认为当前目录。.bewater/（工具+数据）始终安装；
  skills/agents 默认写入 .claude/，--codebuddy/--lingma 切到对应平台目录。
  --claude 与默认行为等效（可显式传入）。
EOF
}

TARGET=""
# 默认平台：claude。不带参数时一并装 .claude/ 下 skills/agents，
# 否则默认只装工具却跑不起来（/bewater-plan、/bewater-build 无从触发）。
PLATFORM="claude"

for arg in "$@"; do
    case "$arg" in
        --help|-h)
            usage
            exit 0
            ;;
        --claude|--codebuddy|--lingma) PLATFORM="${arg#--}" ;;
        --*) echo "未知选项: $arg" >&2; usage >&2; exit 1 ;;
        *) TARGET="$arg" ;;
    esac
done

TARGET="${TARGET:-$(pwd)}"
SRC="$(cd "$(dirname "$0")" && pwd)"

# 防御：拒绝根目录或系统目录，避免误操作系统根。
case "$TARGET" in
    ""|"/"|"/usr"|"/usr/"|"/bin"|"/sbin")
        echo "拒绝将根目录或系统目录作为安装目标: $TARGET" >&2
        exit 1
        ;;
esac

echo "==> 安装 bewater-flow → $TARGET"

# 先拷贝到 .new 临时目录，全部成功后再原子替换旧目录。
# 这样任一步失败都不会留下「flow 在但 scripts 缺失」的半安装态——
# 失败时回滚为 .new 残留（可重跑覆盖），旧工具仍可用。
mkdir -p "$TARGET/.bewater"
NEW="$TARGET/.bewater/.install-new.$$"
rm -rf "$NEW"
# 异常退出（cp 失败等）时清理临时目录，避免残留。成功路径末尾会 rmdir。
trap 'rm -rf "$NEW"' EXIT
mkdir -p "$NEW"
cp "$SRC/flow"         "$NEW/flow"
cp -R "$SRC/scripts"   "$NEW/scripts"
cp -R "$SRC/templates" "$NEW/templates"
chmod +x "$NEW/flow"

# 清理源端可能带入的 __pycache__/.pyc，避免编译产物污染目标项目。
find "$NEW/scripts" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
find "$NEW/scripts" -type f -name '*.pyc' -delete 2>/dev/null || true

# 原子替换：先删旧目录再 mv 新目录到位。mv 是原子操作，窗口极小。
rm -rf "$TARGET/.bewater/scripts" "$TARGET/.bewater/templates"
mv "$NEW/flow"      "$TARGET/.bewater/flow"
mv "$NEW/scripts"   "$TARGET/.bewater/scripts"
mv "$NEW/templates" "$TARGET/.bewater/templates"
rmdir "$NEW" 2>/dev/null || true

echo "  ✓ .bewater/flow"
echo "  ✓ .bewater/scripts"
echo "  ✓ .bewater/templates"

if [[ -n "$PLATFORM" ]]; then
    dest="$TARGET/.$PLATFORM"
    mkdir -p "$dest/skills" "$dest/agents"

    for d in "$SRC"/skills/bewater-*/; do
        [[ -d "$d" ]] || continue
        name="$(basename "$d")"
        rm -rf "$dest/skills/$name"
        cp -R "$d" "$dest/skills/$name"
        echo "  ✓ .$PLATFORM/skills/$name"
    done

    for f in "$SRC"/agents/bewater-*.md; do
        [[ -f "$f" ]] || continue
        cp "$f" "$dest/agents/"
        echo "  ✓ .$PLATFORM/agents/$(basename "$f")"
    done
fi

echo ""
echo "==> 安装完成 ✓"
echo ""
echo "  入口: .bewater/flow（不在 PATH 上，所有命令请用 .bewater/flow 调用）"
echo "  提示: 建议将 .bewater/ 加入项目 .gitignore，避免运行时数据意外提交。"
if [[ -n "$PLATFORM" ]]; then
    echo "  .$PLATFORM/skills/ 与 .$PLATFORM/agents/ 中 bewater-* 条目建议一并忽略。"
fi
