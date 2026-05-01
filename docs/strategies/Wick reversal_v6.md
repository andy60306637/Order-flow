# Wick reversal v6

交易系統: 15m週期，一次一個倉位，透過wick反轉來反向開倉，只在三個交易時間區段交易(Asia, London, NewYork)，存在入倉手續費計算判斷﹐trailing機制，並結合OHLC 、TICK級別資料組合的判斷機制。

做多

1. wick k棒定義 - k0
    1. 價格型態: wick下影線、實體、實體上方影線
        1. 整根k棒長度:  > ATR(14) * 0.8
        2. 下影線長度>=實體長度 * 4
        3. 上影線長度 < (整根 K 棒長度 * 0.1)
    2. 下影線存在訂單流吸收 or 主動反轉 (phase 2)
        1. 吸收型 Wick (Absorption Wick)
        2. 主動驅動型 Wick (Initiative Wick)
    3. k0的最低點要低於過去N個K棒 ( 訂單流掃描)
        1. k0_low低於過去n個k棒
        2. N 採用 ATR 動態調整，規則如下：
            1. 當前波動率 = ATR(14)
            2. 基準波動率 = SMA_ATR(100) (過去100根ATR的簡單移動平均)
            3. 波動率比值 Ratio = ATR(14) / SMA_ATR(100)
            4. 參數設定：
                - Base_N = 24 (基準看過去 6 小時)
                - Min_N = 12 (下限看過去 3 小時)
                - Max_N = 48 (上限看過去 12 小時)
            5. 最終 N 值公式：
            N = Round( Base_N * Ratio )
            N 必須被限制在 [Min_N, Max_N] 的區間內。
2. 交易計畫
    1. k0 k棒成立後，下一個k棒以tick級別等級作為入場依據, 一旦價格> k0實體最高點則入場
    2. 進場點條件二(phase 2)
        1. 當下 Tick 累積的 `cum_delta_eff` > 閥值 (待定義)
        2. cum_delta_eff 區間: 「Zoom 視窗開啟後，到當下 Tick 為止的累積 Delta」 (phase2)
    3. kill zoom視窗機制: 
        1. 4跟k棒內(不含k0)都視為有效進場點
        2. kill zoom區間內tick價格不得< k0_body_low = min(k0_open, k0_close)
        3. 若當前tick價格 > max_entry_price 則不進場
            1.  max_entry_price = k0_high + (k0_high - k0_low) * a
            2. a = 0.25
    4. 停損: 
        1. stop_loss =k0_low - (k0_high - k0_low) * b
        2. b = 0.1
    5. 初始盈虧比 = 2:1, 並按造trailing機製作移動停利
        1. TP  = Enter + (risk x RR)
            1. RR = 2
            2. Risk =  entry - stop loss
        2. trailing: 
            1. cum_delta: 每根 K 棒內重置，bar-level 累積（不跨棒延續）
            2. 如果達到TP, tick級別cum_delta ≤ 0 立即出場
            3. 如果達到TP, tick級別cum_delta > 0 開啟Trailing，將stop_loss設定為 TP price（target_price），並且直到動能轉弱: cum_delta ≤ 0 出場 
                1. traling 高點回落機制 (Drawdown from Peak) (phase 2)
                    1. 回落至50% or 30% 的處理(phase 2定義)
    6. 成本覆蓋檢查
        1. 使用_risk_covers_cost function
            1. risk * rr >= round_trip_cost * fee_cover_ratio
            2. fee_cover_ratio : 1.2
3. 只在(Asia, London, NewYork)三個交易時間進行交易
4. 做空則為做多的鏡像

---

### ⚠️ 實作撰寫 Code 時的溫馨提醒 (單位換算)

邏輯完全沒問題，但在把這份規格書轉成 Python 程式碼時，請留意以下兩個單位的轉換：

**1. Trailing 停損設定**

- **規格書寫法**：`stop_loss設定為 TP price（target_price）`
- **工程實作**：開啟 Trailing 後，`stop_price` 直接設為 `target_p`（即進場時計算的 TP 價格）。這代表最壞情況下以 TP 價格平倉，保留已達到盈虧比的獲利。
- **注意**：此設計與保本出場（`entry + round_trip_fee`）不同，trailing stop 鎖定的是 TP 水位而非成本線。

**2. 做空 (Short) 的鏡像數學反轉**

- **規格書寫法**：做空則為做多的鏡像。
- **工程實作**：在寫做空的函式時，記得把所有的加減號與大於小於反轉。
    - 做空 `max_entry_price` = `k0_low - (k0_high - k0_low) * a` (不能跌破太深進場)。
    - 做空 `stop_loss` = `k0_high + (k0_high - k0_low) * b`。
    - 做空 `TP` = `Entry - (Risk x RR)`。