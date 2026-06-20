#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<EOF
bewater-flow 卸载

用法:
  ./uninstall.sh [<目标目录>] [--claude|--codebuddy|--lingma] [--yes|-y]

  删 .bewater/，删除前交互确认（--yes/-y 跳过）。
  默认一并清理 .claude/ 下 bewater-* 的 skills/agents，--codebuddy/--lingma 切到对应平台目录。
  --claude 与默认行为等效（可显式传入）。
EOF
}

TARGET=""
# 与 install 对称：默认清理 .claude/ 下 bewater-* skills/agents，避免残留。
PLATFORM="claude"
SKIP_CONFIRM=""

for arg in "$@"; do
    case "$arg" in
        --help|-h)
            usage
            exit 0
            ;;
        --claude|--codebuddy|--lingma) PLATFORM="${arg#--}" ;;
        --yes|-y) SKIP_CONFIRM="yes" ;;
        --*) echo "未知选项: $arg" >&2; usage >&2; exit 1 ;;
        *) TARGET="$arg" ;;
    esac
done

TARGET="${TARGET:-$(pwd)}"

# 防御：拒绝根目录或系统目录，避免 rm -rf 误操作系统根。
case "$TARGET" in
    ""|"/"|"/usr"|"/usr/"|"/bin"|"/sbin")
        echo "拒绝将根目录或系统目录作为卸载目标: $TARGET" >&2
        exit 1
        ;;
esac

echo "==> 卸载 bewater-flow → $TARGET"

if [[ -d "$TARGET/.bewater" ]]; then
    if [[ "$SKIP_CONFIRM" != "yes" ]]; then
        # read 在非交互（stdin 关闭/EOF）时返回非零，视作「取消」并以 0 退出，
        # 避免 CI 中静默退出码 1 造成误判。
        if ! read -r -p "将删除 $TARGET/.bewater（含全部需求规划与运行时数据），确认？[y/N] " ans; then
            echo "  非交互环境未确认，已取消。传入 --yes/-y 可跳过确认。" >&2
            exit 0
        fi
        case "$ans" in
            y|Y|yes) ;;
            *) echo "  已取消。"; exit 0 ;;
        esac
    fi
    rm -rf "$TARGET/.bewater"
    echo "  ✓ 已删除 .bewater/"
else
    echo "  (未找到 .bewater/，跳过)"
fi

if [[ -n "$PLATFORM" ]]; then
    dest="$TARGET/.$PLATFORM"
    rm -rf "$dest"/skills/bewater-* "$dest"/agents/bewater-*.md
    echo "  ✓ 已清理 .$PLATFORM 下 bewater-* skills/agents"
fi

echo "==> 卸载完成 ✓"
