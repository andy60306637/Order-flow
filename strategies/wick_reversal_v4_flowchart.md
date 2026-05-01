# Wick Reversal V4 — 策略流程圖

## 主流程

```mermaid
flowchart TD
    START([開始：歷史 K 棒序列]) --> INIT[初始化狀態\nin_position=False\nlong_k0=None / short_k0=None]
    INIT --> LOOP{遍歷每根 K 棒 k_i}

    LOOP -->|有下一根| STEP1{持倉中？}

    %% ── Step 1：持倉管理 ──────────────────────────────────
    STEP1 -->|是| SIDE{方向}
    SIDE -->|Long| EXIT_L[["出場判斷（Long）\n見子圖 A"]]
    SIDE -->|Short| EXIT_S[["出場判斷（Short）\n見子圖 B"]]

    EXIT_L -->|已出場| RESET[重置持倉狀態\nin_position=False\nlong_k0=short_k0=None]
    EXIT_S -->|已出場| RESET
    RESET --> LOOP

    EXIT_L -->|未出場| LOOP
    EXIT_S -->|未出場| LOOP

    %% ── Step 2a：做多 zoom 進場 ───────────────────────────
    STEP1 -->|否| ZOOM_L{long_k0 存在？}
    ZOOM_L -->|否| ZOOM_S
    ZOOM_L -->|是| ZOOM_L_EXP{bars_after > long_zoom_bars?}
    ZOOM_L_EXP -->|是| L_EXPIRE[long_k0 = None\n過期清空] --> ZOOM_S
    ZOOM_L_EXP -->|否| GUARD_L{k.low < k0 實體低點？\n守護線被破}
    GUARD_L -->|是| L_INVALIDATE[long_k0 = None\n守護線失效] --> ZOOM_S
    GUARD_L -->|否| ENTRY_L[["做多進場嘗試\n見子圖 C"]]
    ENTRY_L -->|進場成功| SET_LONG[in_position=True\nside='long'\nlong_k0=short_k0=None]
    SET_LONG --> LOOP
    ENTRY_L -->|進場失敗| ZOOM_S

    %% ── Step 2b：做空 zoom 進場 ───────────────────────────
    ZOOM_S{short_k0 存在？}
    ZOOM_S -->|否| K0_DETECT
    ZOOM_S -->|是| ZOOM_S_EXP{bars_after > short_zoom_bars?}
    ZOOM_S_EXP -->|是| S_EXPIRE[short_k0 = None\n過期清空] --> K0_DETECT
    ZOOM_S_EXP -->|否| GUARD_S{k.high > k0 實體高點？\n守護線被破}
    GUARD_S -->|是| S_INVALIDATE[short_k0 = None\n守護線失效] --> K0_DETECT
    GUARD_S -->|否| ENTRY_S[["做空進場嘗試\n見子圖 D"]]
    ENTRY_S -->|進場成功| SET_SHORT[in_position=True\nside='short'\nlong_k0=short_k0=None]
    SET_SHORT --> LOOP
    ENTRY_S -->|進場失敗| K0_DETECT

    %% ── Step 3：k0 偵測 ──────────────────────────────────
    K0_DETECT{不在持倉中\nenable_long / enable_short}
    K0_DETECT -->|enable_long| IS_K0L[["k0 Long 判定\n見子圖 E"]]
    IS_K0L -->|通過| MARK_L[long_k0 = k_i\n發出 k0_long signal]
    IS_K0L -->|不通過| IS_K0S
    MARK_L --> IS_K0S

    K0_DETECT -->|enable_short| IS_K0S[["k0 Short 判定\n見子圖 F"]]
    IS_K0S -->|通過| MARK_S[short_k0 = k_i\n發出 k0_short signal\n記錄 k0_records]
    IS_K0S -->|不通過| LOOP
    MARK_S --> LOOP

    LOOP -->|結束| RETURN([回傳 signals 列表])
```

---

## 子圖 A — 做多出場

```mermaid
flowchart TD
    A_IN([持倉 Long，處理 k_i]) --> A_MODE{有 tick 資料？}

    A_MODE -->|Tick 模式| A_TICK[遍歷每筆 tick\n累計 cum_vol / cum_buy_vol / cum_delta]
    A_TICK --> A_SL_T{price <= stop_price？}
    A_SL_T -->|是| A_EXIT_SL[出場 SL / TS\nfill=tick price]
    A_SL_T -->|否| A_TR_T{trailing 中？}
    A_TR_T -->|是| A_NEXT_T[繼續下一 tick]
    A_TR_T -->|否| A_TP_T{price >= target？}
    A_TP_T -->|否| A_NEXT_T
    A_TP_T -->|是| A_DELTA_T{cum_delta > 0？}
    A_DELTA_T -->|是 動能延續| A_TRAIL_T[切 Trailing\nstop = target\ntd_consec = 0]
    A_DELTA_T -->|否 動能反轉| A_EXIT_TP[出場 TP]
    A_TRAIL_T --> A_NEXT_T
    A_NEXT_T -->|下一 tick| A_SL_T
    A_NEXT_T -->|棒末| A_TD_T{trailing 且 cum_delta <= 0？}
    A_TD_T -->|是| A_TD_INC[td_consec += 1]
    A_TD_INC --> A_TD_CHK{td_consec >= long_td_consec_bars？}
    A_TD_CHK -->|是| A_EXIT_TD[出場 TD\n@ k.close]
    A_TD_CHK -->|否| A_HOLD
    A_TD_T -->|否| A_TD_RESET[td_consec = 0] --> A_HOLD

    A_MODE -->|Bar 模式| A_BAR_SL{k.low <= stop_price？}
    A_BAR_SL -->|是| A_BAR_EXIT_SL[出場 SL / TS]
    A_BAR_SL -->|否| A_BAR_TR{trailing 中？}
    A_BAR_TR -->|是| A_BAR_DELTA{kline_delta <= 0？}
    A_BAR_DELTA -->|是| A_BAR_INC[td_consec += 1]
    A_BAR_INC --> A_BAR_CHK{>= long_td_consec_bars？}
    A_BAR_CHK -->|是| A_BAR_TD[出場 TD]
    A_BAR_CHK -->|否| A_HOLD
    A_BAR_DELTA -->|否| A_BAR_RESET[td_consec = 0] --> A_HOLD
    A_BAR_TR -->|否| A_BAR_TP{k.high >= target？}
    A_BAR_TP -->|否| A_HOLD
    A_BAR_TP -->|是| A_BAR_D2{kline_delta > 0？}
    A_BAR_D2 -->|是 動能延續| A_BAR_TRAIL[切 Trailing\nstop = target]
    A_BAR_D2 -->|否| A_BAR_EXIT_TP[出場 TP]
    A_BAR_TRAIL --> A_HOLD

    A_HOLD([本棒未出場，繼續])
```

---

## 子圖 B — 做空出場（鏡像）

```mermaid
flowchart TD
    B_IN([持倉 Short，處理 k_i]) --> B_MODE{有 tick 資料？}

    B_MODE -->|Tick 模式| B_SL{price >= stop_price？}
    B_SL -->|是| B_EXIT_SL[出場 SL / TS]
    B_SL -->|否| B_TR{trailing 中？}
    B_TR -->|是| B_NEXT[繼續下一 tick]
    B_TR -->|否| B_TP{price <= target？}
    B_TP -->|否| B_NEXT
    B_TP -->|是| B_D{cum_delta < 0？}
    B_D -->|是 下跌動能| B_TRAIL[切 Trailing\nstop = target]
    B_D -->|否| B_EXIT_TP[出場 TP]
    B_TRAIL --> B_NEXT
    B_NEXT -->|棒末| B_TD{trailing 且 cum_delta >= 0？}
    B_TD -->|是| B_INC[td_consec += 1]
    B_INC --> B_CHK{>= short_td_consec_bars？}
    B_CHK -->|是| B_EXIT_TD[出場 TD @ k.close]
    B_CHK -->|否| B_HOLD
    B_TD -->|否| B_RST[td_consec = 0] --> B_HOLD

    B_MODE -->|Bar 模式| B_BAR_SL{k.high >= stop_price？}
    B_BAR_SL -->|是| B_BAR_SL_OUT[出場 SL / TS]
    B_BAR_SL -->|否| B_BAR_TR{trailing？}
    B_BAR_TR -->|是| B_BAR_D{kline_delta >= 0？}
    B_BAR_D -->|是| B_BAR_INC[td_consec += 1]
    B_BAR_INC --> B_BAR_CHK{>= short_td_consec_bars？}
    B_BAR_CHK -->|是| B_BAR_TD[出場 TD]
    B_BAR_CHK -->|否| B_HOLD
    B_BAR_D -->|否| B_BAR_RST[td_consec = 0] --> B_HOLD
    B_BAR_TR -->|否| B_BAR_TP{k.low <= target？}
    B_BAR_TP -->|否| B_HOLD
    B_BAR_TP -->|是| B_BAR_D2{kline_delta < 0？}
    B_BAR_D2 -->|是| B_BAR_TRAIL[切 Trailing\nstop = target]
    B_BAR_D2 -->|否| B_BAR_TP_OUT[出場 TP]
    B_BAR_TRAIL --> B_HOLD

    B_HOLD([本棒未出場，繼續])
```

---

## 子圖 C — 做多進場條件

```mermaid
flowchart TD
    C_IN([嘗試做多進場 k_i]) --> C_MODE{有 tick 資料？}

    C_MODE -->|Bar 模式| C_BAR_HI{k.high >= k0 實體高點？}
    C_BAR_HI -->|否| C_FAIL
    C_BAR_HI -->|是| C_BAR_DEFF{整棒 delta_eff\n> long_delta_eff_threshold？}
    C_BAR_DEFF -->|否| C_FAIL
    C_BAR_DEFF -->|是| C_VOL{Vol SMA 通過？\ncur_vol > SMA * mult}
    C_VOL -->|否| C_FAIL
    C_VOL -->|是| C_RISK[計算 entry / stop / risk\nentry = k0_body_high\nstop = k0.low - sl_offset]
    C_RISK --> C_RR[動態 RR\n_resolve_long_rr 分級 A/B/C]
    C_RR --> C_COST{risk 覆蓋手續費？\nrisk >= round_trip_cost * fee_ratio / rr}
    C_COST -->|否| C_FAIL
    C_COST -->|是| C_OK[進場成功\ntarget = entry + risk × RR\n發出 long_entry signal]

    C_MODE -->|Tick 模式| C_TICK_VOL{前棒 Vol SMA 通過？}
    C_TICK_VOL -->|否| C_FAIL
    C_TICK_VOL -->|是| C_TICK_LOOP[遍歷 tick\n累計 cum_vol / cum_buy_vol]
    C_TICK_LOOP --> C_T_GUARD{price < k0 實體低點？\n守護線被破}
    C_T_GUARD -->|是| C_FAIL
    C_T_GUARD -->|否| C_T_CHK{price > k0 實體高點\n且 cum_delta_eff > threshold？}
    C_T_CHK -->|否| C_TICK_LOOP
    C_T_CHK -->|是| C_T_RISK[計算 fill / stop / risk\nfill = tick price\nstop = k0.low - sl_offset]
    C_T_RISK --> C_T_COST{cost filter 通過？}
    C_T_COST -->|否| C_TICK_LOOP
    C_T_COST -->|是| C_OK

    C_FAIL([進場失敗])
    C_OK([進場成功])
```

---

## 子圖 D — 做空進場條件（鏡像）

```mermaid
flowchart TD
    D_IN([嘗試做空進場 k_i]) --> D_MODE{有 tick 資料？}

    D_MODE -->|Bar 模式| D_BAR_LO{k.low <= k0 實體低點？}
    D_BAR_LO -->|否| D_FAIL
    D_BAR_LO -->|是| D_BAR_DEFF{整棒 delta_eff\n< -short_delta_eff_threshold？}
    D_BAR_DEFF -->|否| D_FAIL
    D_BAR_DEFF -->|是| D_VOL{Vol SMA 通過？}
    D_VOL -->|否| D_FAIL
    D_VOL -->|是| D_WTYPE[classify_short_k0_wick\n→ A / B / C]
    D_WTYPE --> D_ENABLED{此 wick_type 已啟用？\nenable_short_wick_x}
    D_ENABLED -->|否| D_FAIL
    D_ENABLED -->|是| D_RISK[entry = k0_body_low\nstop = k0.high + sl_offset\nrr = resolve_short_rr]
    D_RISK --> D_COST{cost filter 通過？}
    D_COST -->|否| D_FAIL
    D_COST -->|是| D_OK[進場成功\ntarget = entry - risk × RR\n發出 short_entry signal]

    D_MODE -->|Tick 模式| D_TICK_VOL{前棒 Vol SMA 通過？}
    D_TICK_VOL -->|否| D_FAIL
    D_TICK_VOL -->|是| D_TICK_LOOP[遍歷 tick\n累計 cum_vol / cum_buy_vol]
    D_TICK_LOOP --> D_T_GUARD{price > k0 實體高點？\n守護線被破}
    D_T_GUARD -->|是| D_FAIL
    D_T_GUARD -->|否| D_T_CHK{price < k0 實體低點\n且 cum_delta_eff < -threshold？}
    D_T_CHK -->|否| D_TICK_LOOP
    D_T_CHK -->|是| D_T_WTYPE[分級 wick_type\n是否啟用？]
    D_T_WTYPE -->|未啟用| D_TICK_LOOP
    D_T_WTYPE -->|啟用| D_T_COST{cost filter 通過？}
    D_T_COST -->|否| D_TICK_LOOP
    D_T_COST -->|是| D_OK

    D_FAIL([進場失敗])
    D_OK([進場成功])
```

---

## 子圖 E — k0 Long 判定

```mermaid
flowchart TD
    E_IN([輸入 k 棒]) --> E_RNG{高低差 > 0？}
    E_RNG -->|否| E_FAIL
    E_RNG -->|是| E_VOL{volume >= long_k0_vol_gate？}
    E_VOL -->|否| E_FAIL
    E_VOL -->|是| E_SHAPE["形態檢查\nmid = (high+low)/2\nbody_low = min(open,close)\nlower_wick = body_low - low"]
    E_SHAPE --> E_POS{body_low >= mid？\n實體在上半部}
    E_POS -->|否| E_FAIL
    E_POS -->|是| E_WICK{lower_wick > 0\n且 lower_wick > body？}
    E_WICK -->|否| E_FAIL
    E_WICK -->|是| E_ABS[["下影線吸收確認"]]
    E_ABS --> E_TICK_Q{有 tick 資料？}
    E_TICK_Q -->|是| E_TICK_ABS["篩出 price <= body_low 的 ticks\n計算 wick_vol / wick_delta_eff"]
    E_TICK_ABS --> E_TICK_CHK{"wick_vol / total_vol >= min_vol_ratio\n且 wick_delta_eff <= delta_eff_max？\n（買壓被吸收 → 賣方佔優）"}
    E_TICK_CHK -->|是| E_OK
    E_TICK_CHK -->|否| E_FAIL
    E_TICK_Q -->|否 Bar 模式| E_BAR_ABS{kline_delta <= bar_delta_max？}
    E_BAR_ABS -->|是| E_OK
    E_BAR_ABS -->|否| E_FAIL

    E_FAIL([不是 k0 Long])
    E_OK([是 k0 Long ✓])
```

---

## 子圖 F — k0 Short 判定

```mermaid
flowchart TD
    F_IN([輸入 k 棒]) --> F_RNG{高低差 > 0？}
    F_RNG -->|否| F_FAIL
    F_RNG -->|是| F_VOL{volume >= short_k0_vol_gate？}
    F_VOL -->|否| F_FAIL
    F_VOL -->|是| F_SHAPE["形態檢查\nmid = (high+low)/2\nbody_high = max(open,close)\nupper_wick = high - body_high"]
    F_SHAPE --> F_POS{body_high <= mid？\n實體在下半部}
    F_POS -->|否| F_FAIL
    F_POS -->|是| F_WICK{upper_wick > 0\n且 upper_wick > body？}
    F_WICK -->|否| F_FAIL
    F_WICK -->|是| F_ABS[["上影線吸收確認"]]
    F_ABS --> F_TICK_Q{有 tick 資料？}
    F_TICK_Q -->|是| F_TICK_ABS["篩出 price >= body_high 的 ticks\n計算 wick_vol / wick_delta_eff"]
    F_TICK_ABS --> F_TICK_CHK{"wick_vol / total_vol >= min_vol_ratio\n且 wick_delta_eff >= delta_eff_min？\n（買壓被承接但未突破）"}
    F_TICK_CHK -->|否| F_FAIL
    F_TICK_CHK -->|是| F_WTYPE
    F_TICK_Q -->|否 Bar 模式| F_BAR{kline_delta >= bar_delta_min？}
    F_BAR -->|否| F_FAIL
    F_BAR -->|是| F_WTYPE

    F_WTYPE["classify_short_k0_wick\n→ A: ratio >= wick_a_threshold\n→ B: ratio >= wick_b_threshold\n→ C: 其他"]
    F_WTYPE --> F_ENABLED{此 wick_type 啟用？}
    F_ENABLED -->|否| F_FAIL
    F_ENABLED -->|是| F_REGIME[["regime 過濾\n_short_k0_regime_ok"]]
    F_REGIME --> F_A{wick_type == A？}
    F_A -->|是| F_A_CHK{upper_wick_pct\n>= short_a_min_upper_wick_pct？}
    F_A_CHK -->|否| F_FAIL
    F_A_CHK -->|是| F_OK
    F_A -->|否 B| F_B_CHK{上影線 % >= min_wick_pct\n且 vol >= min_k0_vol\n且 runup >= min_runup_pct？}
    F_B_CHK -->|否| F_FAIL
    F_B_CHK -->|是| F_OK
    F_A -->|C| F_OK

    F_FAIL([不是 k0 Short])
    F_OK([是 k0 Short ✓])
```

---

## 動態 RR 分級總覽

```mermaid
flowchart LR
    WICK_RATIO["wick / body 比值\n（body_floor 兜底）"]
    WICK_RATIO --> GTE_A{">= wick_type_a_threshold\n（預設 4.0）"}
    GTE_A -->|是| TYPE_A["A 級\nLong RR=3.0 / Short RR=4.5"]
    GTE_A -->|否| GTE_B{">= wick_type_b_threshold\n（預設 3.0）"}
    GTE_B -->|是| TYPE_B["B 級\nLong RR=1.5 / Short RR=2.5"]
    GTE_B -->|否| TYPE_C["C 級\nLong RR=2.0 / Short RR=2.0\n（Short C 預設關閉）"]
```
