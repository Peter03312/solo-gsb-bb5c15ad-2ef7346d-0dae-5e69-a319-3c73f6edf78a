#!/usr/bin/env bash
# 本轮用法端到端示例: 普通分配锁定表 (会务方按论文锁定 0~2 名评审人)
#   录入 -> 无锁定预演 -> 提交完整锁定表 (携带所见修订号) -> 相同表重试幂等 ->
#   过期/未知/重复锁定被拒且不改表 -> 预演与发布保留锁定槽位
#   (单人锁定时专长要求由搭档满足) -> 锁定人被停用: 预演说明具体冲突且
#   不可完整分配, 发布 422 且当前发布版不变, 锁定不被悄悄释放 ->
#   重新启用恢复 -> 补位不受锁定约束 -> 空表清除全部锁定 -> 查看锁定表
# 建议对新库运行: ./examples/locks.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
ORG="X-Organizer-Key: ${ORGANIZER_KEY:-dev-organizer-key}"
j() { python3 -c 'import sys, json; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

echo "== 1. 录入评审人 (R3 擅长 DB, 其余擅长 AI) 与论文 =="
for spec in 'R1 r1-secret Inst-A AI' 'R2 r2-secret Inst-B AI' 'R3 r3-secret Inst-C DB' 'R4 r4-secret Inst-D AI'; do
  set -- $spec
  curl -s -o /dev/null -X POST "$BASE/reviewers" -H "$ORG" -H 'Content-Type: application/json' -d "{
    \"reviewer_id\": \"$1\", \"credential\": \"$2\", \"topics\": [\"$4\"],
    \"institution\": \"$3\", \"capacity\": 2, \"avoid_papers\": []}"
done
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P1", "manuscript": "P1 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-X"]}'
curl -s -o /dev/null -X POST "$BASE/papers" -H "$ORG" -H 'Content-Type: application/json' -d '{
  "paper_id": "P2", "manuscript": "P2 匿名稿全文...", "topics": ["AI"], "institutions": ["Univ-Y"]}'

echo "== 2. 无锁定预演 (纯优化次序下的方案) =="
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["plan"]'

echo "== 3. 提交完整锁定表: P1 锁定 R3 (单人, R3 不擅长 AI, 专长由搭档满足); P2 锁定 R1+R2 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X PUT "$BASE/assignment/locks" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R3\"], \"P2\": [\"R1\", \"R2\"]}}"; echo

echo "== 3b. 相同锁定表重试 -> changed=false, 不推进修订号 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X PUT "$BASE/assignment/locks" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P2\": [\"R2\", \"R1\"], \"P1\": [\"R3\"]}}"; echo

echo "== 3c. 过期修订号 -> 409; 未知评审人 -> 404; 重复锁定 -> 422 (均不改表) =="
curl -s -o /dev/null -w '过期修订号       -> HTTP %{http_code}\n' -X PUT "$BASE/assignment/locks" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d '{"base_revision": 0, "locks": {"P1": ["R1"]}}'
curl -s -o /dev/null -w '未知评审人       -> HTTP %{http_code}\n' -X PUT "$BASE/assignment/locks" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"GHOST-R\"]}}"
curl -s -o /dev/null -w '重复锁定         -> HTTP %{http_code}\n' -X PUT "$BASE/assignment/locks" \
  -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {\"P1\": [\"R1\", \"R1\"]}}"

echo "== 4. 预演: 锁定槽位保留 (P1 必含 R3, P2 恰为 R1+R2), 剩余位置仍按既有约束与优化次序 =="
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'plan': d['plan'], 'locks': d['locks'], 'ratio': d['max_used_capacity_ratio']}"

echo "== 5. 发布 (携带当前资料修订号; 过期修订号会 409) =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/assignment/publish" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV}" | j "{'ok': d['ok'], 'serial': d['serial'], 'plan': d['plan']}"

echo "== 6. 锁定人 R3 被停用: 预演说明具体冲突 (locked_reviewer_disabled) 并标为不可完整分配 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/reviewers/R3/status" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"R3\", \"base_revision\": $REV, \"active\": false, \"reason\": \"长期休假暂停本轮评审\"}" \
  | j "{'active': d['active'], 'changed': d['changed']}"
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" \
  | j "{'feasible': d['feasible'], 'lock_problems': d['lock_problems'], 'unassigned': d['unassigned']}"

echo "== 6b. 发布拒绝 (422) 且当前发布版不变; 锁定不被悄悄释放 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -o /dev/null -w '强行发布         -> HTTP %{http_code}\n' -X POST "$BASE/assignment/publish" \
  -H "$ORG" -H 'Content-Type: application/json' -d "{\"base_revision\": $REV}"
curl -s "$BASE/assignment" -H "$ORG" | j "{'serial': d['serial'], 'plan': d['plan']}"
curl -s "$BASE/assignment/locks" -H "$ORG" | j 'd["locks"]'

echo "== 7. 重新启用 R3: 预演恢复可行, 锁定槽位照常保留 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X POST "$BASE/reviewers/R3/status" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"reviewer_id\": \"R3\", \"base_revision\": $REV, \"active\": true, \"reason\": \"假期结束恢复评审\"}" \
  | j "{'active': d['active'], 'changed': d['changed']}"
curl -s -X POST "$BASE/assignment/dry-run" -H "$ORG" | j 'd["plan"]'

echo "== 8. 锁定表仅约束普通分配: 补位仍只固定已确认槽位 (R1 确认 P2 后, P2 的锁定对不再被固定) =="
curl -s -X POST "$BASE/reviewer/assignments/P2/decision" \
  -H 'X-Reviewer-Id: R1' -H 'X-Reviewer-Credential: r1-secret' \
  -H 'Content-Type: application/json' -d '{"paper_id": "P2", "decision": "confirm"}' \
  | j "{'state': d['state'], 'changed': d['changed']}"
curl -s -X POST "$BASE/assignment/backfill/dry-run" -H "$ORG" \
  | j "{'fixed': d['fixed'], 'plan': d['plan']}"

echo "== 9. 空表清除全部锁定; 会务方查看当前锁定表 =="
REV=$(curl -s "$BASE/meta" -H "$ORG" | j 'd["revision"]')
curl -s -X PUT "$BASE/assignment/locks" -H "$ORG" -H 'Content-Type: application/json' \
  -d "{\"base_revision\": $REV, \"locks\": {}}"; echo
curl -s "$BASE/assignment/locks" -H "$ORG" | j 'd'
