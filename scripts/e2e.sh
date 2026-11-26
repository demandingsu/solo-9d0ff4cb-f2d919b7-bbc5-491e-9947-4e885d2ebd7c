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

echo "== 11. 舞监设换景停点：校验规则 =="
# 重新交一份含口令的稿件：第 6、7 行文字相同（口令多行匹配场景），第 8 行唯一口令"收尾"。
post /api/script '{"lines":[
  {"line_no":1,"text":"开场白"},{"line_no":2,"text":"第二句"},{"line_no":3,"text":"第三句"},
  {"line_no":4,"text":"新加的第四句"},{"line_no":5,"text":"续稿第五句"},
  {"line_no":6,"text":"重复口令"},{"line_no":7,"text":"重复口令"},{"line_no":8,"text":"收尾"}]}' >/dev/null
RNO=$(post /api/stops '{"cue_text":"没有这句"}')
check "口令0行匹配被拒" '"reason":"cue_not_unique"' "$RNO"
check "match_count=0" '"match_count":0' "$RNO"
RMULTI=$(post /api/stops '{"cue_text":"重复口令"}')
check "口令多行匹配被拒" '"match_count":2' "$RMULTI"
RTAKEN=$(post /api/stops '{"cue_text":"开场白"}')
check "停点行已被领取被拒" '"reason":"line_already_taken"' "$RTAKEN"
RSTOP=$(post /api/stops '{"cue_text":"收尾"}')
check "唯一口令设停点成功201" '"status":"pending"' "$RSTOP"
check "停点定位第8行" '"line_no":8' "$RSTOP"
STOP_ID=$(echo "$RSTOP" | sed -n 's/.*"stop_id":\([0-9]*\).*/\1/p')
check "待放行停点可查" "\"stop_id\":$STOP_ID" "$(get /api/stops/pending)"
RDUPSTOP=$(post /api/stops '{"cue_text":"第三句"}')
check "同时只能有一个待放行停点" '"reason":"pending_stop_exists"' "$RDUPSTOP"

echo "== 12. 一台先到 -> 等候且进度不动；提前/错号放行退回 =="
# 两台当前进度：p1=5、p2=3。先把 p2 追到 5。
post /api/prompters/2/lines '{"last_line_no":3}' >/dev/null
post /api/prompters/2/lines '{"last_line_no":4}' >/dev/null
check "p2进度追到5" '"prompter_id":2,"last_line_no":5' "$(get /api/prompters/2/progress)"
RWAIT=$(post /api/prompters/1/lines '{"last_line_no":5}')
# p1 沿途先领第 6、7 行，再次请求才在第 8 行停点前停下。
RWAIT=$(post /api/prompters/1/lines '{"last_line_no":6}')
check "p1正常领第7行" '"line_no":7' "$RWAIT"
RWAIT=$(post /api/prompters/1/lines '{"last_line_no":7}')
check "p1先到返回waiting" '"status":"waiting"' "$RWAIT"
check "等候时进度不动" '"last_line_no":7' "$RWAIT"
check "回传停点信息" "\"stop_id\":$STOP_ID" "$RWAIT"
[ "$(echo "$RWAIT" | jq -c '.stop.arrived_prompter_ids')" = "[1]" ] \
  && { echo "  PASS: 到台名单含p1"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: 到台名单含p1 -> $RWAIT"; FAIL=$((FAIL+1)); }
RWAIT2=$(post /api/prompters/1/lines '{"last_line_no":7}')
check "先到者重发仍等候" '"status":"waiting"' "$RWAIT2"
REARLY=$(post "/api/stops/$STOP_ID/release" '{}')
check "未到齐提前放行409" '"reason":"not_all_arrived"' "$REARLY"
RWRONG=$(post "/api/stops/999999/release" '{}')
check "编号不符放行409" '"reason":"stop_not_found"' "$RWRONG"
check "停点保持原状" "\"stop_id\":$STOP_ID" "$(get /api/stops/pending)"

echo "== 13. 另一台照常领到停点行，到齐后凭编号放行 =="
ROTHER=$(post /api/prompters/2/lines '{"last_line_no":5}')
check "p2正常领第6行" '"line_no":6' "$ROTHER"
ROTHER=$(post /api/prompters/2/lines '{"last_line_no":6}')
check "p2正常领第7行" '"line_no":7' "$ROTHER"
ROTHER=$(post /api/prompters/2/lines '{"last_line_no":7}')
check "p2照常领到停点行第8行" '"line_no":8,"text":"收尾"' "$ROTHER"
check "p2进度推进到8" '"last_line_no":8' "$ROTHER"
[ "$(get /api/stops/pending | jq -c '.arrived_prompter_ids')" = "[1,2]" ] \
  && { echo "  PASS: 两台到齐"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: 两台到齐"; FAIL=$((FAIL+1)); }
RBLOCK=$(post /api/prompters/2/lines '{"last_line_no":8}')
check "放行前p2后续请求仍等候" '"status":"waiting"' "$RBLOCK"
RREL=$(post "/api/stops/$STOP_ID/release" '{}')
check "凭编号放行成功" '"status":"released"' "$RREL"
check "放行后无待放行停点" 'null' "$(get /api/stops/pending)"
RRESUME1=$(post /api/prompters/1/lines '{"last_line_no":7}')
check "p1原请求领到停点行" '"line_no":8,"text":"收尾"' "$RRESUME1"
check "p1进度推进到8" '"last_line_no":8' "$RRESUME1"
RTAIL=$(post /api/prompters/1/lines '{"last_line_no":8}')
check "稿尾no_line" '"status":"no_line"' "$RTAIL"

echo "== 14. 放行前换稿与领词交错：停点移动/固定/退回 =="
# 续稿（1..8 已领行原文不动，追加 9..11 行）后在第 10 行设新停点，两台都领到第 9 行。
post /api/script '{"lines":[
  {"line_no":1,"text":"开场白"},{"line_no":2,"text":"第二句"},{"line_no":3,"text":"第三句"},
  {"line_no":4,"text":"新加的第四句"},{"line_no":5,"text":"续稿第五句"},
  {"line_no":6,"text":"重复口令"},{"line_no":7,"text":"重复口令"},{"line_no":8,"text":"收尾"},
  {"line_no":9,"text":"第九句"},{"line_no":10,"text":"场歇"},{"line_no":11,"text":"尾声"}]}' >/dev/null
post /api/prompters/1/lines '{"last_line_no":8}' >/dev/null   # p1 -> 9
post /api/prompters/2/lines '{"last_line_no":8}' >/dev/null   # p2 -> 9
RSTOP2=$(post /api/stops '{"cue_text":"场歇"}')
STOP2=$(echo "$RSTOP2" | sed -n 's/.*"stop_id":\([0-9]*\).*/\1/p')
check "新停点在第10行" '"line_no":10' "$RSTOP2"
# 两台都未到：口令移到第 11 行（未领行），停点随行移动；已领行 1..9 全部保持原文。
RMOVE=$(post /api/script '{"lines":[
  {"line_no":1,"text":"开场白"},{"line_no":2,"text":"第二句"},{"line_no":3,"text":"第三句"},
  {"line_no":4,"text":"新加的第四句"},{"line_no":5,"text":"续稿第五句"},
  {"line_no":6,"text":"重复口令"},{"line_no":7,"text":"重复口令"},{"line_no":8,"text":"收尾"},
  {"line_no":9,"text":"第九句"},{"line_no":10,"text":"过场改词"},{"line_no":11,"text":"场歇"}]}')
check "未到齐换稿成功" '"status":"accepted"' "$RMOVE"
check "停点移到第11行" '"line_no":11' "$(get /api/stops/pending)"
# 口令不唯一 -> 整稿退回，停点不变。
RBADCUE=$(post /api/script '{"lines":[
  {"line_no":1,"text":"开场白"},{"line_no":2,"text":"第二句"},{"line_no":3,"text":"第三句"},
  {"line_no":4,"text":"新加的第四句"},{"line_no":5,"text":"续稿第五句"},
  {"line_no":6,"text":"重复口令"},{"line_no":7,"text":"重复口令"},{"line_no":8,"text":"收尾"},
  {"line_no":9,"text":"第九句"},{"line_no":10,"text":"场歇"},{"line_no":11,"text":"场歇"}]}')
check "口令重复整稿退回" '"reason":"cue_not_unique"' "$RBADCUE"
check "退回后停点仍在11行" '"line_no":11' "$(get /api/stops/pending)"
# 一台到达后停点固定原行号：p1 领第 10 行后再请求第 11 行 -> waiting。
post /api/prompters/1/lines '{"last_line_no":9}' >/dev/null
RFIXWAIT=$(post /api/prompters/1/lines '{"last_line_no":10}')
check "p1在第11行停点前等候" '"status":"waiting"' "$RFIXWAIT"
# 口令随追加行挪到第 12 行（未领行），但已有一台到达 -> 固定行号冲突，整稿退回。
RFIXED=$(post /api/script '{"lines":[
  {"line_no":1,"text":"开场白"},{"line_no":2,"text":"第二句"},{"line_no":3,"text":"第三句"},
  {"line_no":4,"text":"新加的第四句"},{"line_no":5,"text":"续稿第五句"},
  {"line_no":6,"text":"重复口令"},{"line_no":7,"text":"重复口令"},{"line_no":8,"text":"收尾"},
  {"line_no":9,"text":"第九句"},{"line_no":10,"text":"过场改词"},{"line_no":11,"text":"被挪走的口令"},
  {"line_no":12,"text":"场歇"}]}')
check "一台到后口令移位被拒" '"reason":"stop_line_fixed"' "$RFIXED"
check "固定行号仍是11" '"line_no":11' "$(get /api/stops/pending)"
# 口令保持在第 11 行的换稿允许（仅追加第 12 行），到齐放行后两台用原请求继续。
post /api/script '{"lines":[
  {"line_no":1,"text":"开场白"},{"line_no":2,"text":"第二句"},{"line_no":3,"text":"第三句"},
  {"line_no":4,"text":"新加的第四句"},{"line_no":5,"text":"续稿第五句"},
  {"line_no":6,"text":"重复口令"},{"line_no":7,"text":"重复口令"},{"line_no":8,"text":"收尾"},
  {"line_no":9,"text":"第九句"},{"line_no":10,"text":"过场改词"},{"line_no":11,"text":"场歇"},
  {"line_no":12,"text":"加映尾声"}]}' >/dev/null
post /api/prompters/2/lines '{"last_line_no":9}' >/dev/null
post /api/prompters/2/lines '{"last_line_no":10}' >/dev/null   # p2 领到第 11 行停点
[ "$(get /api/stops/pending | jq -c '.arrived_prompter_ids')" = "[1,2]" ] \
  && { echo "  PASS: 再次到齐"; PASS=$((PASS+1)); } \
  || { echo "  FAIL: 再次到齐"; FAIL=$((FAIL+1)); }
post "/api/stops/$STOP2/release" '{}' >/dev/null
RAFTER=$(post /api/prompters/1/lines '{"last_line_no":10}')
check "放行后p1领到第11行" '"line_no":11,"text":"场歇"' "$RAFTER"
RNEWTAIL=$(post /api/prompters/1/lines '{"last_line_no":11}')
check "p1继续领到第12行" '"line_no":12' "$RNEWTAIL"

echo
echo "结果: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
