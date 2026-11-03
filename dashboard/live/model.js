/* 方向模型（浏览器 + Node 通用）。与 backtest/direction_model.py 的 features() 逐项对应。
   bars: [{t: 开盘时间毫秒, o, h, l, c, v, tb}]，按时间升序，最后一根可以是未走完的 K 线。 */
(function (root) {
  const NYFMT = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "2-digit", hourCycle: "h23" });
  function nyParts(ms) {
    const p = NYFMT.formatToParts(new Date(ms));
    const wd = p.find(x => x.type === "weekday").value;
    return { weekend: wd === "Sat" || wd === "Sun", hour: +p.find(x => x.type === "hour").value };
  }
  const mean = (a, i, n) => { let s = 0; for (let k = i - n + 1; k <= i; k++) s += a[k]; return s / n; };
  function median(a, i, n) {
    const s = a.slice(i - n + 1, i + 1).sort((x, y) => x - y), m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  function ewm(arr, alpha) {           // pandas ewm(adjust=False)，跳过开头的 NaN
    const out = new Array(arr.length).fill(NaN); let prev = NaN;
    for (let i = 0; i < arr.length; i++) {
      const x = arr[i];
      if (isNaN(x)) { out[i] = prev; continue; }
      prev = isNaN(prev) ? x : alpha * x + (1 - alpha) * prev; out[i] = prev;
    }
    return out;
  }
  function rsi6(c) {
    const up = [NaN], dn = [NaN];
    for (let i = 1; i < c.length; i++) { const d = c[i] - c[i - 1]; up.push(Math.max(d, 0)); dn.push(Math.max(-d, 0)); }
    const u = ewm(up, 1 / 6), d = ewm(dn, 1 / 6);
    return u.map((x, i) => d[i] === 0 ? NaN : 100 - 100 / (1 + x / d[i]));
  }
  function kdjJ(b) {
    const rsv = b.map((x, i) => {
      if (i < 8) return 50;
      let lo = Infinity, hi = -Infinity;
      for (let k = i - 8; k <= i; k++) { lo = Math.min(lo, b[k].l); hi = Math.max(hi, b[k].h); }
      return hi === lo ? 50 : (x.c - lo) / (hi - lo) * 100;
    });
    const k = ewm(rsv, 1 / 3), d = ewm(k, 1 / 3);
    return k.map((x, i) => 3 * x - 2 * d[i]);
  }
  function macdHist(c) {
    const f = ewm(c, 2 / 13), s = ewm(c, 2 / 27), dif = f.map((x, i) => x - s[i]), dea = ewm(dif, 2 / 10);
    return dif.map((x, i) => x - dea[i]);
  }
  // 4 小时趋势：UTC 对齐的 4h 收盘价 / 其 25 根均值 - 1，只用已经走完的 4h 桶
  function trend4h(b5, atMs) {
    const H4 = 4 * 3600e3, buckets = new Map();
    for (const x of b5) buckets.set(Math.floor(x.t / H4) * H4, x.c);
    const keys = [...buckets.keys()].filter(k => k + H4 <= atMs).sort((a, b) => a - b);
    if (keys.length < 25) return NaN;
    const last = keys.slice(-25).map(k => buckets.get(k));
    return last[24] / (last.reduce((s, x) => s + x, 0) / 25) - 1;
  }
  function features(b, dayBars, b5ForTrend) {
    const i = b.length - 1, c = b.map(x => x.c), v = b.map(x => x.v);
    if (i < Math.max(dayBars, 99) + 1) return null;
    const f = {};
    for (const k of [1, 3, 12, 48, dayBars]) f["ret_" + k] = Math.log(c[i] / c[i - k]);
    for (const n of [7, 25, 99]) f["dist_ma" + n] = c[i] / mean(c, i, n) - 1;
    f.rsi6 = rsi6(c)[i] / 100 - 0.5;
    f.j = (kdjJ(b)[i] - 50) / 100;
    f.hist = macdHist(c)[i] / c[i];
    const med = median(v, i, dayBars);
    f.vol_ratio = Math.log(Math.max(v[i] / med, 1e-3));
    f.taker_buy_1 = (v[i] ? b[i].tb / v[i] : 0.5) - 0.5;
    let tb12 = 0, v12 = 0; for (let k = i - 11; k <= i; k++) { tb12 += b[k].tb; v12 += v[k]; }
    f.taker_buy_12 = tb12 / v12 - 0.5;
    let hi = -Infinity, lo = Infinity; for (let k = i - dayBars + 1; k <= i; k++) { hi = Math.max(hi, b[k].h); lo = Math.min(lo, b[k].l); }
    f.pos24 = (c[i] - lo) / (hi - lo) - 0.5;
    f.range24 = (hi - lo) / c[i];
    const ny = nyParts(b[i].t);
    f.weekend = ny.weekend ? 1 : 0;
    f.us_hours = !ny.weekend && ny.hour >= 9 && ny.hour < 16 ? 1 : 0;
    f.us_open_hour = !ny.weekend && ny.hour === 9 ? 1 : 0;
    f.trend4h = trend4h(b5ForTrend || b, b[i].t);
    return f;
  }
  function predict(model, f) {
    if (!f) return NaN;
    let z = model.w[0];
    for (let k = 0; k < model.cols.length; k++) {
      let x = f[model.cols[k]];
      if (!isFinite(x)) x = model.mu[k];
      x = Math.max(-5, Math.min(5, (x - model.mu[k]) / model.sd[k]));
      z += model.w[k + 1] * x;
    }
    return 1 / (1 + Math.exp(-z));
  }
  /* 张主任完整版信号（与 backtest/zhang.py signals() 同口径），返回最近 3 根内是否触发及各项状态 */
  function zhang(b, look = 6, volWin = 24) {
    const n = b.length; if (n < 130) return null;
    const c = b.map(x => x.c), v = b.map(x => x.v), J = kdjJ(b), R = rsi6(c), H = macdHist(c);
    const ma = (i, k) => mean(c, i, k);
    const at = i => {
      const m7 = ma(i, 7), m7p = ma(i - 1, 7), m7pp = ma(i - 2, 7), m25 = ma(i, 25), m7p25 = ma(i - 1, 25);
      const crossDn = [0, 1, 2].some(k => ma(i - k, 7) < ma(i - k, 25) && ma(i - k - 1, 7) >= ma(i - k - 1, 25));
      const crossUp = [0, 1, 2].some(k => ma(i - k, 7) > ma(i - k, 25) && ma(i - k - 1, 7) <= ma(i - k - 1, 25));
      const trigS = c[i] < m7 && m7 < m7p && m7p >= m7pp && (m7 > m25 || crossDn);
      const trigL = c[i] > m7 && m7 > m7p && m7p <= m7pp && (m7 < m25 || crossUp);
      const win = a => a.slice(i - look + 1, i + 1);
      const hot = Math.max(...win(J)) > 100 && Math.max(...win(R)) > 70, cold = Math.min(...win(J)) < 0 && Math.min(...win(R)) < 20;
      const hMax = Math.max(...win(H)), hMin = Math.min(...win(H));
      const fadeS = hMax > 0 && H[i] < hMax, fadeL = hMin < 0 && H[i] > hMin;
      const threeDown = v[i] < v[i - 1] && v[i - 1] < v[i - 2];
      const hiW = b.slice(i - look + 1, i + 1), loIdx = hiW.reduce((m, x, k) => x.l < hiW[m].l ? k : m, 0), hiIdx = hiW.reduce((m, x, k) => x.h > hiW[m].h ? k : m, 0);
      const prior = b.slice(i - look - volWin + 1, i - look + 1), priorMaxV = Math.max(...prior.map(x => x.v));
      const newHigh = hiW[hiIdx].h >= Math.max(...prior.map(x => x.h)), newLow = hiW[loIdx].l <= Math.min(...prior.map(x => x.l));
      const volS = (newHigh && hiW[hiIdx].v < 0.9 * priorMaxV) || threeDown, volL = (newLow && hiW[loIdx].v < 0.9 * priorMaxV) || threeDown;
      let above = 0, below = 0; for (let k = i - look; k < i; k++) { if (c[k] > ma(k, 7)) above++; if (c[k] < ma(k, 7)) below++; }
      return { trigS, trigL, hot, cold, fadeS, fadeL, volS, volL, above7: above >= 4, below7: below >= 4,
        shortB: trigS && hot && fadeS && volS && above >= 4, longB: trigL && cold && fadeL && volL && below >= 4,
        J: J[i], R: R[i], threeDown, m7Down: m7 < m7p && m7p >= m7pp, m7Up: m7 > m7p && m7p <= m7pp, hist: H[i], hMax, hMin };
    };
    const now = at(n - 1), recent = [n - 1, n - 2, n - 3].map(at);
    let hi12 = -Infinity, lo12 = Infinity; for (let k = n - 12; k < n; k++) { hi12 = Math.max(hi12, b[k].h); lo12 = Math.min(lo12, b[k].l); }
    return { now, shortRecent: recent.some(x => x.shortB), longRecent: recent.some(x => x.longB), hi12, lo12 };
  }
  /* 2026-11-03 现行监控规格。与 monitor/signal_logic.py 逐项对应，阈值来自 rules_spec.json 的 signals。 */
  const SIGNAL_DEFAULT = {
    five_min_every_sec: 180, anomaly_every_sec: 60, position_every_sec: 900, position_cooldown_sec: 7200,
    bounds: {
      "5m": { rsi_ob: 80, rsi_os: 20, j_ob: 110, j_os: -5 },
      "15m": { rsi_ob: 70, rsi_os: 20, j_ob: 100, j_os: 0 },
      "30m": { rsi_ob: 70, rsi_os: 20, j_ob: 100, j_os: 0 },
      "1h": { rsi_ob: 70, rsi_os: 20, j_ob: 100, j_os: 0 },
      "4h": { rsi_ob: 70, rsi_os: 20, j_ob: 100, j_os: 0 },
    },
    daily: { ma: 99, lookback: 7 }, h4: { ma: 99, lookback: 6 }, mid_ma: 25,
    six: { rsi_touch_ob: 70, rsi_touch_os: 30, j_hot: 100, j_cold: 0, j_look: 6, hist_look: 8, ma_cross_look: 48, div_window: 48, div_vol_frac: 0.9 },
    candle: { doji_body_frac: 0.1, long_shadow_frac: 0.5, top_bars: 6 },
    custom: { closes_above: 6, ma_gap: 0.001, cross_look: 10 },
    anomaly: { vol_mult: 6, vol_median_bars: 120, move_pct: 0.5, three: { body_frac: 0.4, overlap_frac: 0.5, total_pct: 0.3 }, ma7: { surge_pct: 0.1, peak_frac: 0.6, flat_pct: 0.02 } },
    position: { oi_1h_pct: 1.5, funding_abs: 0.03, ls_1h_abs: 0.1, liq_usd: 15000000, liq_side_frac: 0.7 },
    email_kinds: ["共振", "极端", "盘面异动", "仓位异动"],
  };
  const SIX_SHORT = [["s1", "量能顶背离"], ["s2", "J>100后拐头向下"], ["s3", "RSI6触及70"], ["s4", "MACD正柱见顶衰减"], ["s5", "MA7上穿后拐头准备下穿"], ["s6", "顶部长上影+十字星"]];
  const SIX_LONG = [["l1", "量能底背离"], ["l2", "J<0后拐头向上"], ["l3", "RSI6触及30"], ["l4", "MACD负柱向零收敛"], ["l5", "MA7下穿后拐头准备上穿"], ["l6", "底部长下影+十字星"]];
  const CUSTOM_DEF = [["c1", "近6根收盘都在MA7上"], ["c2", "MA7准备上穿或10根内刚上穿MA25"], ["c3", "J>100"], ["c4", "RSI6>70"], ["c5", "近3根量连续递减"], ["c6", "MACD正柱比上一根短"]];
  function smaSeries(closes, n) {
    const out = new Array(closes.length).fill(null); let acc = 0;
    for (let i = 0; i < closes.length; i++) {
      acc += closes[i];
      if (i >= n) acc -= closes[i - n];
      if (i >= n - 1) out[i] = acc / n;
    }
    return out;
  }
  function medianOf(xs) {
    if (!xs.length) return null;
    const s = xs.slice().sort((a, b) => a - b), m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  function biasOf(closes, n, look) {
    const i = closes.length - 1;
    if (i < n - 1 + look) return null;
    const at = end => { let t = 0; for (let k = end - n + 1; k <= end; k++) t += closes[k]; return t / n; };
    const now = at(i), old = at(i - look), slope = now - old, pct = old ? (now / old - 1) * 100 : 0, flat = slope === 0;
    let dir = slope > 0 ? 1 : slope < 0 ? -1 : closes[i] > now ? 1 : closes[i] < now ? -1 : 0;
    return { ma: now, old, slope, pct, dir, flat, bias: dir > 0 ? "偏多" : dir < 0 ? "偏空" : "走平", price: closes[i] };
  }
  function stageOf(bars, n) {
    const c = bars.map(b => b.c);
    if (c.length < n + 1) return null;
    let ma = 0, prev = 0;
    for (let k = c.length - n; k < c.length; k++) ma += c[k];
    for (let k = c.length - n - 1; k < c.length - 1; k++) prev += c[k];
    ma /= n; prev /= n;
    const slope = ma - prev, px = c[c.length - 1];
    const name = px > ma && slope > 0 ? "上涨" : px < ma && slope < 0 ? "下跌" : "过渡震荡";
    return { name, ma, slope, price: px };
  }
  function divSide(bars, side, window, frac) {
    if (bars.length < window) return false;
    const w = bars.slice(-window), prev = w.slice(0, -1), cur = w[w.length - 1], vols = [];
    if (side === "high") {
      if (!(cur.h > Math.max(...prev.map(b => b.h)))) return false;
      for (let i = 1; i < prev.length - 1; i++) if (prev[i].h >= prev[i - 1].h && prev[i].h >= prev[i + 1].h) vols.push(prev[i].v);
      let peak = 0; for (let i = 1; i < prev.length; i++) if (prev[i].h > prev[peak].h) peak = i;
      vols.push(prev[peak].v);
    } else {
      if (!(cur.l < Math.min(...prev.map(b => b.l)))) return false;
      for (let i = 1; i < prev.length - 1; i++) if (prev[i].l <= prev[i - 1].l && prev[i].l <= prev[i + 1].l) vols.push(prev[i].v);
      let peak = 0; for (let i = 1; i < prev.length; i++) if (prev[i].l < prev[peak].l) peak = i;
      vols.push(prev[peak].v);
    }
    const med = medianOf(vols);
    return !!med && cur.v < frac * med;
  }
  function jTurn(j, hot, cold, look, up) {
    const w = j.slice(-look);
    if (w.length < look || w.some(x => x == null || Number.isNaN(x))) return false;
    if (up) return Math.min(...w) < cold && j[j.length - 1] > j[j.length - 2] && j[j.length - 1] > Math.min(...w);
    return Math.max(...w) > hot && j[j.length - 1] < j[j.length - 2] && j[j.length - 1] < Math.max(...w);
  }
  function histDecay(h, look, short) {
    const seg = h.slice(-look);
    if (seg.length < look || seg.some(x => x == null || Number.isNaN(x))) return false;
    if (short) {
      if (seg[look - 1] <= 0) return false;
      let p = 0; for (let i = 1; i < look - 1; i++) if (seg[i] > seg[p]) p = i;
      if (seg[p] <= 0 || seg[look - 1] >= seg[p] || seg[look - 1] >= seg[look - 2]) return false;
      for (let i = p + 1; i < look; i++) if (seg[i] > seg[i - 1]) return false;
      return true;
    }
    if (seg[look - 1] >= 0) return false;
    let p = 0; for (let i = 1; i < look - 1; i++) if (seg[i] < seg[p]) p = i;
    if (seg[p] >= 0 || seg[look - 1] <= seg[p] || seg[look - 1] <= seg[look - 2]) return false;
    for (let i = p + 1; i < look; i++) if (seg[i] < seg[i - 1]) return false;
    return true;
  }
  function crossed(fast, slow, i, up) {
    if ([fast[i], slow[i], fast[i - 1], slow[i - 1]].some(x => x == null)) return false;
    return up ? fast[i] > slow[i] && fast[i - 1] <= slow[i - 1] : fast[i] < slow[i] && fast[i - 1] >= slow[i - 1];
  }
  function maAfterCross(ma7, ma25, look, short) {
    const n = ma7.length, start = Math.max(1, n - look);
    let found = false;
    for (let i = start; i < n; i++) if (crossed(ma7, ma25, i, short)) found = true;
    if (!found || ma7[n - 1] == null || ma25[n - 1] == null || ma7[n - 2] == null) return false;
    if (short) return ma7[n - 1] < ma25[n - 1] || ma7[n - 1] - ma7[n - 2] < 0;
    return ma7[n - 1] > ma25[n - 1] || ma7[n - 1] - ma7[n - 2] > 0;
  }
  function candleExt(bars, spec, top) {
    const cdl = spec.candle;
    if (bars.length < cdl.top_bars) return false;
    const b = bars[bars.length - 1], rng = b.h - b.l;
    if (rng <= 0) return false;
    const body = Math.abs(b.c - b.o), upper = b.h - Math.max(b.o, b.c), lower = Math.min(b.o, b.c) - b.l;
    if (body > cdl.doji_body_frac * rng) return false;
    const window = bars.slice(-cdl.top_bars);
    if (top) return upper >= cdl.long_shadow_frac * rng && upper >= lower && b.h >= Math.max(...window.map(x => x.h));
    return lower >= cdl.long_shadow_frac * rng && lower >= upper && b.l <= Math.min(...window.map(x => x.l));
  }
  function outOf(rsi, j, side, bound) {
    if (rsi == null || j == null || Number.isNaN(rsi) || Number.isNaN(j)) return [false, false];
    if (side === "short") return [rsi > bound.rsi_ob, j > bound.j_ob];
    return [rsi < bound.rsi_os, j < bound.j_os];
  }
  function resonance(oscs, divS, divL, spec) {
    const b = spec.bounds, strict = b["5m"];
    const sideTier = (side, div) => {
      const [r5, j5] = outOf(oscs["5m"].rsi, oscs["5m"].j, side, b["5m"]);
      const [r15, j15] = outOf(oscs["15m"].rsi, oscs["15m"].j, side, b["15m"]);
      const [r30, j30] = outOf(oscs["30m"].rsi, oscs["30m"].j, side, b["30m"]);
      const [r1, j1] = outOf(oscs["1h"].rsi, oscs["1h"].j, side, b["1h"]);
      const [r4, j4] = outOf(oscs["4h"].rsi, oscs["4h"].j, side, b["4h"]);
      const [rs5, js5] = outOf(oscs["5m"].rsi, oscs["5m"].j, side, strict);
      const [rs15, js15] = outOf(oscs["15m"].rsi, oscs["15m"].j, side, strict);
      const via3 = []; if (rs5 && rs15) via3.push("RSI"); if (js5 && js15) via3.push("KDJ");
      const via2 = []; if (r5 && r15) via2.push("RSI"); if (j5 && j15) via2.push("KDJ");
      const via1 = []; if (r5) via1.push("RSI"); if (j5) via1.push("KDJ");
      const t3 = via3.length > 0, t2 = via2.length > 0, t1 = via1.length > 0;
      const funnel = t3 && div ? 4 : t3 ? 3 : t2 ? 2 : t1 ? 1 : 0;
      let via = t3 ? via3 : t2 ? via2 : via1;
      const extRsi = r5 && r15 && r30 && r1, extJ = j5 && j15 && j30 && j1;
      const extVia = []; if (extRsi) extVia.push("RSI"); if (extJ) extVia.push("KDJ");
      const ext4 = (extRsi && r4) || (extJ && j4);
      const extRank = ext4 ? 6 : extVia.length ? 5 : 0;
      const rank = Math.max(funnel, extRank);
      if (extRank >= funnel && extRank) via = extVia;
      if (via.includes("RSI")) via = ["RSI"].concat(via.filter(x => x !== "RSI"));
      return { tier: rank, via, funnel, extreme: extRank };
    };
    return { short: sideTier("short", divS), long: sideTier("long", divL) };
  }
  function tierLabel(side, tier, via) {
    const word = side === "long" ? "做多" : "做空", ob = side === "long" ? "超卖" : "超买", viaTxt = (via && via.length) ? via.join("、") : "指标";
    if (tier <= 0) return "没有共振";
    if (tier === 1) return `观察：5 分钟严格${ob}（${viaTxt}）。只是观察，不是提示${word}`;
    if (tier === 2) return `同指标配对 · 观察：5 分钟和 15 分钟 ${viaTxt} 同时${ob}。只是配对，不是提示${word}`;
    if (tier === 3) return `提示${word}：5 分钟和 15 分钟 ${viaTxt} 都到严格档`;
    if (tier === 4) return `强烈提示${word}：共振，并且有严格量价${side === "long" ? "底" : "顶"}背离`;
    const base = side === "long" ? "大仓位买入信号" : "大仓位做空信号";
    if (tier === 5) return `${base}：同一指标在 5 分、15 分、30 分、1 小时都出区间（${viaTxt}）`;
    return `${base} · 再升一级：4 小时也出区间（${viaTxt}）`;
  }
  function tierSubject(side, tier) {
    const long = side === "long";
    if (tier === 1) return "共振观察";
    if (tier === 2) return "共振配对观察";
    if (tier === 3) return long ? "提示做多" : "提示做空";
    if (tier === 4) return long ? "强烈提示做多" : "强烈提示做空";
    if (tier === 5) return long ? "大仓位买入信号" : "大仓位做空信号";
    if (tier >= 6) return long ? "大仓位买入信号再升一级" : "大仓位做空信号再升一级";
    return "共振";
  }
  function directionPhrase(z) {
    const one = (name, x) => `${name}MA99${x.bias}（斜率 ${x.slope >= 0 ? "+" : ""}${x.slope.toFixed(1)}，${x.pct >= 0 ? "+" : ""}${x.pct.toFixed(2)}%${x.flat ? "，走平，价格在均线" + (x.price > x.ma ? "上" : "下") : ""}）`;
    let rel = "";
    if (z.daily.dir && z.h4.dir && z.daily.dir * z.h4.dir < 0) rel = " 冲突，以日线为准。";
    else if (z.daily.dir && z.daily.dir === z.h4.dir) rel = " 两层同向。";
    return one("日线", z.daily) + "；" + one("4小时", z.h4) + "。" + rel;
  }
  function directionSubject(z) { return `日线MA99${z.daily.bias} · 4小时MA99${z.h4.bias}`; }
  function threeBull(bars, spec) {
    const p = spec.anomaly.three;
    if (bars.length < 3) return false;
    const a = bars[bars.length - 3], b = bars[bars.length - 2], c = bars[bars.length - 1];
    const bull = x => { const rng = x.h - x.l; return x.c > x.o && rng > 0 && (x.c - x.o) >= p.body_frac * rng; };
    if (!(bull(a) && bull(b) && bull(c)) || !(a.c < b.c && b.c < c.c)) return false;
    const ok = (prev, nxt) => nxt.l >= prev.h - p.overlap_frac * (prev.h - prev.l);
    return ok(a, b) && ok(b, c) && a.o > 0 && (c.c / a.o - 1) * 100 >= p.total_pct;
  }
  function oscOf(bars) {
    const closes = bars.map(b => b.c);
    const R = rsi6(closes), J = kdjJ(bars), H = macdHist(closes);
    const last = (a) => a[a.length - 1];
    return { rsi: last(R), j: last(J), hist: H, rsiS: R, jS: J, closes };
  }
  function asItems(def, vals) { return def.map((d, k) => ({ id: d[0], name: d[1], on: !!vals[k] })); }
  function evaluateSignals(bundle, spec, nowMs) {
    spec = spec || SIGNAL_DEFAULT;
    const need = { "5m": 80, "15m": 40, "30m": 40, "1h": 40, "4h": spec.h4.ma + spec.h4.lookback + 1, "1d": spec.daily.ma + spec.daily.lookback + 1 };
    for (const k of Object.keys(need)) if (!(bundle[k] && bundle[k].length >= need[k])) return null;
    const m5 = bundle["5m"];
    const osc = { "5m": oscOf(m5), "15m": oscOf(bundle["15m"]), "30m": oscOf(bundle["30m"]), "1h": oscOf(bundle["1h"]), "4h": oscOf(bundle["4h"]) };
    const six = spec.six, closes = osc["5m"].closes, ma7 = smaSeries(closes, 7), ma25 = smaSeries(closes, 25);
    const divS = divSide(m5, "high", six.div_window, six.div_vol_frac), divL = divSide(m5, "low", six.div_window, six.div_vol_frac);
    const J = osc["5m"].jS, H = osc["5m"].hist, r = osc["5m"].rsi;
    const sOn = [divS, jTurn(J, six.j_hot, six.j_cold, six.j_look, false), r != null && r >= six.rsi_touch_ob, histDecay(H, six.hist_look, true), maAfterCross(ma7, ma25, six.ma_cross_look, true), candleExt(m5, spec, true)];
    const lOn = [divL, jTurn(J, six.j_hot, six.j_cold, six.j_look, true), r != null && r <= six.rsi_touch_os, histDecay(H, six.hist_look, false), maAfterCross(ma7, ma25, six.ma_cross_look, false), candleExt(m5, spec, false)];
    const cu = spec.custom;
    let above = false;
    if (ma7[ma7.length - 1] != null) {
      above = true;
      for (let i = closes.length - cu.closes_above; i < closes.length; i++) if (!(ma7[i] != null && closes[i] > ma7[i])) above = false;
    }
    let prep = false, crossedUp = false;
    if (ma7[ma7.length - 1] != null && ma25[ma25.length - 1] && ma7[ma7.length - 2] != null) {
      const gap = (ma25[ma25.length - 1] - ma7[ma7.length - 1]) / ma25[ma25.length - 1];
      prep = ma7[ma7.length - 1] < ma25[ma25.length - 1] && gap > 0 && gap < cu.ma_gap && (ma7[ma7.length - 1] - ma7[ma7.length - 2]) > 0;
      const from = Math.max(0, closes.length - 1 - cu.cross_look);
      for (let i = from; i < closes.length - 1; i++) if (ma7[i] != null && ma25[i] != null && ma7[i] <= ma25[i]) crossedUp = true;
      crossedUp = ma7[ma7.length - 1] > ma25[ma25.length - 1] && crossedUp;
    }
    const vols = m5.map(b => b.v);
    const cOn = [above, prep || crossedUp, osc["5m"].j > six.j_hot, r != null && r > six.rsi_touch_ob, vols.length >= 3 && vols[vols.length - 1] < vols[vols.length - 2] && vols[vols.length - 2] < vols[vols.length - 3], H[H.length - 1] > 0 && H[H.length - 1] < H[H.length - 2]];
    const oscNow = {};
    for (const k of Object.keys(osc)) oscNow[k] = { rsi: osc[k].rsi, j: osc[k].j };
    const tiers = resonance(oscNow, divS, divL, spec);
    const rankS = tiers.short.tier, rankL = tiers.long.tier;
    let side = null;
    if (rankS > rankL) side = "short";
    else if (rankL > rankS) side = "long";
    else if (rankS) side = (osc["5m"].rsi || 50) >= 50 ? "short" : "long";
    const tier = side === "short" ? rankS : side === "long" ? rankL : 0;
    const via = side ? tiers[side].via : [];
    const daily = biasOf(bundle["1d"].map(b => b.c), spec.daily.ma, spec.daily.lookback);
    const h4 = biasOf(bundle["4h"].map(b => b.c), spec.h4.ma, spec.h4.lookback);
    if (!daily || !h4) return null;
    const stage1 = stageOf(bundle["1h"], spec.mid_ma), stage30 = stageOf(bundle["30m"], spec.mid_ma);
    const closed = m5.slice(0, -1), cc = closed.map(b => b.c);
    const maNow = cc.slice(-7).reduce((s, x) => s + x, 0) / 7, maPrev = cc.slice(-8, -1).reduce((s, x) => s + x, 0) / 7;
    const slope = maPrev ? (maNow / maPrev - 1) * 100 : 0;
    const cur = m5[m5.length - 1];
    let el = 1;
    if (nowMs != null && cur.t) el = (nowMs - cur.t) / 300000;
    const pace = Math.max(0.2, Math.min(1, el > 0 ? el : 1));
    const vNow = cur.v / pace, tail = closed.slice(-24), avg = tail.reduce((s, b) => s + b.v, 0) / tail.length;
    const hot = !!avg && vNow > 2 * avg && vNow > closed[closed.length - 1].v;
    const hi12 = Math.max(...closed.slice(-12).map(b => b.h)), lo12 = Math.min(...closed.slice(-12).map(b => b.l));
    const sixS = asItems(SIX_SHORT, sOn), sixL = asItems(SIX_LONG, lOn), custom = asItems(CUSTOM_DEF, cOn);
    const flags = {};
    for (const x of sixS.concat(sixL, custom)) flags[x.id] = x.on;
    flags.three = threeBull(m5, spec);
    return {
      close: cur.c, bar: cur.t, closedT: closed[closed.length - 1].t,
      daily, h4, conflict: !!(daily.dir && h4.dir && daily.dir * h4.dir < 0),
      stage1h: stage1, stage30, osc: oscNow, divS, divL, sixS, sixL, custom,
      sixSN: sixS.filter(x => x.on).length, sixLN: sixL.filter(x => x.on).length,
      tiers, tier, tierSide: side, via, label: side ? tierLabel(side, tier, via) : "没有共振",
      extreme: tier >= 5 ? side : null, extremePlus: !!(side && tier >= 6),
      prompt: !!(side && tier >= 3),
      withDaily: !!(side && tier >= 3 && ((side === "long" && daily.dir > 0) || (side === "short" && daily.dir < 0))),
      anomS: rankS >= 2 && hot && cur.h >= hi12, anomL: rankL >= 2 && hot && cur.l <= lo12,
      threeBull: flags.three, ma7Slope: slope, ma7: ma7[ma7.length - 1], ma25: ma25[ma25.length - 1], flags,
      long: side === "long" && tier >= 3, short: side === "short" && tier >= 3,
    };
  }
  function stepMa7(state, slope, spec) {
    const p = (spec || SIGNAL_DEFAULT).anomaly.ma7;
    const st = state || {};
    let mode = st.mode || "idle", peak = st.peak || 0, prev = st.prev, reportedSlow = !!st.reported_slow, reportedFlat = !!st.reported_flat;
    const events = [], declining = prev != null && slope < prev;
    const flatText = () => `5 分钟 MA7 斜率接近水平：${slope.toFixed(3)}%/根，已落到 +${p.flat_pct.toFixed(2)}%/根以内。${slope < 0 ? "已转负，注明拐头。" : ""}放缓≠反转。`;
    if ((mode === "idle" || mode === "slowing" || mode === "flat") && slope > p.surge_pct) {
      mode = "surge"; peak = slope; reportedSlow = false; reportedFlat = false;
    } else if (mode === "surge") {
      peak = Math.max(peak, slope);
      if (slope <= p.flat_pct && !reportedFlat) { events.push({ kind: "flat", text: flatText() }); mode = "flat"; reportedFlat = true; }
      else if (slope < p.peak_frac * peak && declining && !reportedSlow) {
        events.push({ kind: "slow", text: `5 分钟 MA7 斜率放缓：峰值 ${peak.toFixed(3)}%/根，现在 ${slope.toFixed(3)}%/根，低于峰值的 ${(p.peak_frac * 100).toFixed(0)}% 并且逐根走低。放缓≠反转。` });
        mode = "slowing"; reportedSlow = true;
      }
    } else if (mode === "slowing" && slope <= p.flat_pct && !reportedFlat) {
      events.push({ kind: "flat", text: flatText() }); mode = "flat"; reportedFlat = true;
    }
    return { state: { mode, peak, prev: slope, reported_slow: reportedSlow, reported_flat: reportedFlat }, events };
  }
  const api = { features, predict, nyParts, zhang, evaluateSignals, resonance, stepMa7, tierLabel, tierSubject, directionPhrase, directionSubject, SIGNAL_DEFAULT };
  if (typeof module !== "undefined") module.exports = api; else root.BTCModel = api;
})(this);
