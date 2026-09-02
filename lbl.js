const AIC_RULE_LABEL_RE = /^([-*\s]*)[A-Z][A-Z0-9_ ]{1,30}\s*[:：]\s*/;
function aicStripRuleLabels(text) {
  return String(text || "").split("\n")
    .map(ln => ln.replace(AIC_RULE_LABEL_RE, (_m, bullet) => bullet)).join("\n");
}
const cases = [
 ["-OUTLIER : 낙도성 Defective 불량으로 판단된 이력 있음, retest 재현성 확인 및 Contact(환경성 요인) Check 진행"],
 ["-LOW CPK : 산포 개선 및 개발팀 협의로 Spec Margin 확보 검토 필요"],
 ["- FUNC_FAIL: Pattern 확인 및 Margin Test 진행"],
 ["- 강제 0xFF 써서 Fail 된 이력이 있음, Pattern 확인"],
 ["- 가성으로 판단된 이력이 있음"],
 ["- CPK 개선 필요"],
 ["- Retest 로 재현 여부 확인"],
 ["- P1/L1 사례에서 contact open 으로 판정되어 socket 교체 후 회복"],
 ["- 기존 Bin5 에서 Bin15 로 전이된 이력이 있어 PGM 확인 및 time 최적화 검토 필요"],
];
for (const [t] of cases) {
  const out = aicStripRuleLabels(t);
  console.log((out === t ? "유지 " : "라벨제거 ") + "| " + out);
}
