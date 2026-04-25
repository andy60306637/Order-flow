【VP + K0 宏微觀狙擊策略】規格書**：

📊 核心策略概念
透過 24H Session Volume Profile 找尋價值區間下緣 (VAL)、控制點 (POC) 與上緣 (VAH)。當價格觸碰 VAL 或 VAH 時，使用 wick_reversal_v4_ratio 的 K0 邏輯（動態比例參數）偵測流動性吸收或回踩確認，於下一根 K 棒以 Tick 級別精確入場，並將獲利目標鎖定在 POC 或順勢測幅。

⚙️ 具體規則與補強建議
1. 交易時段限制 (Session Filter)

原定： 亞洲盤、倫敦盤、紐約盤開盤時間內交易。

補強建議： 加密貨幣是 24/7，建議以 UTC 時間明確定義活躍時段。例如：

東京盤 (Asia)：00:00 - 08:00 UTC

倫敦盤 (London)：08:00 - 16:30 UTC

紐約盤 (New York)：13:30 - 21:00 UTC

實務做法：您可以設定策略只在 00:00 ~ 21:00 UTC 允許開倉，避開美股收盤後到隔日亞盤開盤前的死水期。

2. 倉位管理 (Position Limit)

規則： 同時間內最高持倉量 (Max Open Positions) = 1。前一筆訂單（無論止盈或止損）完全出場後，才允許觸發下一個 K0 訊號。

3. Volume Profile 區塊定義 (Fixed Session Profile)

原定： 倫敦盤先開盤，以前一天區塊定義今天。

邏輯修正： 金融市場的一天通常由**亞洲盤（雪梨/東京）**開始。對於加密貨幣，最標準的做法是以 00:00 UTC (台灣時間早上 8 點) 作為日線換日的界線。

規則： 使用 00:00 UTC 到 23:59 UTC 畫出一個完整的 Fixed 24H Volume Profile。「今天的交易，完全依賴昨天一整天的 VAL, POC, VAH 作為防守線與目標價」。每日 00:00 重新快取更新一次這三條線，以極大化回測運算效能。

4. 觸發條件 (K0 確認機制)

規則： 放棄原有的 A, B, C 型態分級。只要求：

做多： K0 的下影線必須「跌破或觸碰」昨日 VAL (或順勢回踩 VAH)，且實體收盤價收回線上。

做空： K0 的上影線必須「突破或觸碰」昨日 VAH (或順勢回踩 VAL)，且實體收盤價收回線下。

一旦成立，下一根 K 棒啟動 Tick 追蹤進場。
k0的吸收、品質定義和wick_revalsal_v4 ratio相同。

5. 獲利出場機制 (Target POC & Trailing)

第一階段 (TP1)： 價格觸碰 POC。到達 POC 時，觸發原策略的 Trailing 機制（平倉 50% 並將停損移至進場價 Break Even）。

第二階段 (TP2)： 交由微觀動能（如 K 棒 Delta 轉弱）或 Trailing Stop 判定最終出場點，放飛剩餘利潤。

6. 手續費過濾器 (Dynamic Cost Filter)

規則： 結合 v4_ratio 邏輯。進場前計算：預期獲利距離 = |進場價 - POC|。若該距離不足以覆蓋手續費與滑價的倍數（例如 < Round_trip_cost * fee_cover_ratio），代表「肉太少（價值區間過窄）」，放棄該次進場。

7. 順勢突破回踩 (Break & Retest) ── 停利點定義

情境： 價格強勢突破 VAH，隨後回檔測試 VAH，出現 Long K0 撐住。此時上方已經沒有 POC 當磁鐵，該如何停利？

建議定義（三選一）：

方案 A（等距測幅 - 推薦）： 既然突破了價值區，市場會尋找下一個同等大小的價值區。做多 TP = VAH + (VAH - VAL)。

方案 B（引線測幅）： 使用 進場價 + (K0_Range * 1.618) 作為保底目標。

方案 C（無目標放飛）： 順勢突破往往是大行情的起點，不設固定 TP，進場後直接啟動 Trailing，直到出現相反方向的動能（Delta < 0）才平倉。

請實作三種的停利機制讓使用者可以透過index切換。

8. 停損點位定義 (Stop Loss Placement) —— 您原本未寫完的部分

規則： 無論是區間內的 Rejection 還是突破後的 Retest，停損邏輯必須嚴格綁定 K0 的極端值，並套用 v4_ratio 的動態讓點。

做多停損 (Long SL)： K0.low - (K0.close * sl_offset_pct)

做空停損 (Short SL)： K0.high + (K0.close * sl_offset_pct)

若進場後價格未達 POC 就跌破/突破此界線，認賠殺出，等待下一個機會。