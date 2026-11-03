/* 面板和后台用同一组 K 线，布尔结果必须一致。 */
const fs = require("fs");
const path = require("path");
const M = require("../dashboard/live/model.js");
const spec = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "rules_spec.json"), "utf8")).signals;
const fix = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "signal_case.json"), "utf8"));
const z = M.evaluateSignals(fix.bundle, spec, fix.now_ms);
const got = {
  div_s: z.divS, div_l: z.divL, tier: z.tier, tier_side: z.tierSide,
  six_s: z.sixS.map(x => x.on), six_l: z.sixL.map(x => x.on), custom: z.custom.map(x => x.on),
  three_bull: z.threeBull, daily_dir: z.daily.dir, h4_dir: z.h4.dir,
  stage1h: z.stage1h.name, stage30: z.stage30.name, label: z.label,
  ma7_slope: Math.round(z.ma7Slope * 1e6) / 1e6,
};
const exp = fix.expect;
const bad = [];
for (const k of Object.keys(exp)) {
  if (JSON.stringify(got[k]) !== JSON.stringify(exp[k])) bad.push([k, exp[k], got[k]]);
}
if (bad.length) {
  console.log(JSON.stringify(bad, null, 2));
  process.exit(1);
}
const d = M.SIGNAL_DEFAULT;
if (d.h4.lookback !== spec.h4.lookback || d.daily.lookback !== spec.daily.lookback) process.exit(2);
if (d.anomaly.vol_mult !== spec.anomaly.vol_mult || d.position.liq_usd !== spec.position.liq_usd) process.exit(3);
if (d.bounds["5m"].j_os !== spec.bounds["5m"].j_os || d.six.div_window !== spec.six.div_window) process.exit(4);
console.log("parity ok");
