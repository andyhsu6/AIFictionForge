#!/bin/bash
# omo-switch.sh — OpenCode AI provider 一键切换（commandcode ↔ opencode-go）
# 用法:
#   ./omo-switch.sh status           查看当前生效 provider 与配置文件
#   ./omo-switch.sh commandcode      切到 commandcode（DeepSeek V4 Flash）
#   ./omo-switch.sh opencode-go      切到 opencode-go（注意: 周限额!）
# 切换后需重启 OpenCode 生效。
set -euo pipefail

OMO_JSONC="$HOME/.omo/omo.jsonc"
OPENCODE_JSON="$HOME/.config/opencode/opencode.json"
TS=$(date +%Y%m%d_%H%M%S)

# 两套 provider 的 model 值
CC_MODEL="commandcode/deepseek/deepseek-v4-flash"
OG_MODEL="opencode-go/deepseek-v4-flash"

# omo.jsonc 需要替换的 model 值（agents + categories 全部）
# opencode.json 需要替换的 model 值（顶层 + agent 块）

usage() {
  echo "用法: ./omo-switch.sh {status|commandcode|opencode-go}"
  echo "  切换后重启 OpenCode 生效"
}

backup() {
  cp "$OMO_JSONC" "$OMO_JSONC.bak.$TS" 2>/dev/null || true
  cp "$OPENCODE_JSON" "$OPENCODE_JSON.bak.$TS" 2>/dev/null || true
}

switch_all() {
  local from="$1" to="$2"
  backup
  # omo.jsonc: 替换所有 model 值
  sed -i '' "s|$from|$to|g" "$OMO_JSONC"
  # opencode.json: 替换所有 model 值（顶层 model + agent 块 + provider 内 model key）
  sed -i '' "s|$from|$to|g" "$OPENCODE_JSON"
  echo "✅ 已切换: $from → $to"
  echo "   (备份: $OMO_JSONC.bak.$TS, $OPENCODE_JSON.bak.$TS)"
  echo "   请重启 OpenCode 生效"
}

current() {
  local cc_omo cc_oc og_omo og_oc
  cc_omo=$(grep -c "$CC_MODEL" "$OMO_JSONC" 2>/dev/null || true)
  og_omo=$(grep -c "$OG_MODEL" "$OMO_JSONC" 2>/dev/null || true)
  cc_oc=$(grep -c "$CC_MODEL" "$OPENCODE_JSON" 2>/dev/null || true)
  og_oc=$(grep -c "$OG_MODEL" "$OPENCODE_JSON" 2>/dev/null || true)
  echo "=== 当前 provider 配置状态 ==="
  echo "omo.jsonc:      commandcode ×$cc_omo  |  opencode-go ×$og_omo"
  echo "opencode.json:  commandcode ×$cc_oc  |  opencode-go ×$og_oc"
  if [ "$cc_omo" -gt 0 ] && [ "$cc_oc" -gt 0 ]; then
    echo "→ 当前生效: commandcode"
  elif [ "$og_omo" -gt 0 ] && [ "$og_oc" -gt 0 ]; then
    echo "→ 当前生效: opencode-go ⚠️ 注意周限额"
  else
    echo "→ 状态不一致，建议手动检查"
  fi
}

case "${1:-}" in
  status)
    current
    ;;
  commandcode)
    # 双向替换: 不管当前是哪个，最终都是 commandcode
    switch_all "$OG_MODEL" "$CC_MODEL"
    # 同时移除 disabled_providers 里的 opencode-go（如存在）
    ;;
  opencode-go)
    switch_all "$CC_MODEL" "$OG_MODEL"
    ;;
  *)
    usage
    exit 1
    ;;
esac
