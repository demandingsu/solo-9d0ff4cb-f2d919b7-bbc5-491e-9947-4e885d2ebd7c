#!/usr/bin/env bash
# 端到端自测：对运行中的服务执行完整业务流。
# 用法: bash scripts/e2e.sh [BASE_URL]
set -u
BASE="${1:-http://127.0.0.1:8000}"
PASS=0
FAIL=0

check() { # check <描述> <期望子串> <实际JSON>
  if echo "$3" | grep -q "$2"; then
    echo "  PASS: $1"
    PASS=$((PASS+1))
  else
    echo "  FAIL: $1"
    echo "        期望包含: $2"
    echo "        实际: $3"
    FAIL=$((FAIL+1))
  fi
}

post() { curl -sS -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }
get()  { curl -sS "$BASE$1"; }

echo "== 1. 开场状态 =="
check "两台进度均为 0" '"last_line_no":0' "$(get /api/progress)"
check "暂无稿件" '"total_lines":0' "$(get /api/script)"

echo "== 2. 导演提交首稿 3 行 =="
RESP=$(post /api/script '{"lines":[{"line_no":1,"text":"开场白"},{"line_no":2,"text":"第二句"},{"line_no":3,"text":"第三句"}]}')
check "收稿成功 version=1" '"version":1' "$RESP"

echo "== 3. 两台提词器开场凭 0 领词 =="
R1=$(post /api/prompters/1/lines '{"last_line_no":0}')
check "1号机领第1行" '"line_no":1,"text":"开场白"' "$R1"
R2=$(post /api/prompters/2/lines '{"last_line_no":0}')
check "2号机领第1行" '"line_no":1,"text":"开场白"' "$R2"

echo "== 4. 已领成功的请求重发 -> 幂等原行 =="
R1R=$(post /api/prompters/1/lines '{"last_line_no":0}')
check "重发返回原行" '"line_no":1,"text":"开场白"' "$R1R"
check "重发不推进进度" '"last_line_no":1' "$R1R"

echo "== 5. 跳号拒绝 =="
RG=$(post /api/prompters/2/lines '{"last_line_no":5}')
check "跳号返回409 reason=gap" '"reason":"gap"' "$RG"

echo "== 6. 交错领词 =="
post /api/prompters/1/lines '{"last_line_no":1}' >/dev/null   # p1 -> 2
post /api/prompters/2/lines '{"last_line_no":1}' >/dev/null   # p2 -> 2
post /api/prompters/1/lines '{"last_line_no":2}' >/dev/null   # p1 -> 3
check "p1进度3" '"prompter_id":1,"last_line_no":3' "$(get /api/prompters/1/progress)"
check "p2进度2" '"prompter_id":2,"last_line_no":2' "$(get /api/prompters/2/progress)"

echo "== 7. 校对新稿：与已领行冲突 -> 首处不符报行号，整份退回 =="
RB=$(post /api/script '{"lines":[{"line_no":1,"text":"开场白"},{"line_no":2,"text":"被篡改"},{"line_no":3,"text":"第三句"}]}')
check "报行号2" '"line_no":2' "$RB"
check "reason=line_mismatch" '"reason":"line_mismatch"' "$RB"
check "原稿仍是3行" '"total_lines":3' "$(get /api/script)"
ROLD=$(post /api/prompters/2/lines '{"last_line_no":2}')
check "继续用原稿供词(第3行)" '"line_no":3,"text":"第三句"' "$ROLD"

echo "== 8. 合规换稿（保留已领行原文，追加第4行） =="
ROK=$(post /api/script '{"lines":[{"line_no":1,"text":"开场白"},{"line_no":2,"text":"第二句"},{"line_no":3,"text":"第三句"},{"line_no":4,"text":"新加的第四句"}]}')
check "换稿成功 version=2" '"version":2' "$ROK"
RP=$(post /api/prompters/1/lines '{"last_line_no":3}')
check "p1领到延续行号第4行" '"line_no":4,"text":"新加的第四句"' "$RP"
RID=$(post /api/prompters/2/lines '{"last_line_no":2}')
check "p2重发旧请求幂等第3行" '"line_no":3,"text":"第三句"' "$RID"

echo "== 9. 稿尾报暂无，续稿后用原请求取下一行 =="
TAIL=$(post /api/prompters/1/lines '{"last_line_no":4}')
check "稿尾 status=no_line" '"status":"no_line"' "$TAIL"
check "稿尾进度不动" '"last_line_no":4' "$TAIL"
RCONT=$(post /api/script '{"lines":[{"line_no":1,"text":"开场白"},{"line_no":2,"text":"第二句"},{"line_no":3,"text":"第三句"},{"line_no":4,"text":"新加的第四句"},{"line_no":5,"text":"续稿第五句"}]}')
check "续稿成功 version=3" '"version":3' "$RCONT"
RRESUME=$(post /api/prompters/1/lines '{"last_line_no":4}')
check "原请求领到第5行" '"line_no":5,"text":"续稿第五句"' "$RRESUME"

echo "== 10. 结构校验 =="
RDUP=$(post /api/script '{"lines":[{"line_no":1,"text":"x"},{"line_no":1,"text":"y"}]}')
check "重复行号被拒" '"reason":"duplicate_line_no"' "$RDUP"
RGAP=$(post /api/script '{"lines":[{"line_no":1,"text":"x"},{"line_no":3,"text":"y"}]}')
check "缺号被拒" '"reason":"non_consecutive_line_no"' "$RGAP"

echo
echo "结果: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
