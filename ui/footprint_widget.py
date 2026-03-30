"""
Footprint Chart Widget — 視覺優化版 4.0

【視覺語意（固定唯一）】
  ‣ bid_vol  = taker buy  = 買方主動 → 青色系（淡/中/亮）
  ‣ ask_vol  = taker sell = 賣方主動 → 紅色系（淡/中/亮）
  ‣ 顏色亮度 = 優勢強度（不只方向，也代表強弱）
  ‣ 黃框      = POC（近 15 根高亮，更舊的細暗框）
  ‣ POC 虛線  = 近 8 根 POC 的水平延伸線（唯一用途）
  ‣ 角標小方塊 = 單格 3:1 imbalance（非 stacked）
  ‣ 側邊細條   = stacked imbalance（≥ 3 格連續）

【4 種顯示模式】
  BidxAsk    ─ 左=賣量, 右=買量；dominance 配色
  Delta      ─ 中央 = delta（bid−ask）；dominance 配色
  Volume     ─ 中央 = 成交量；量能 heat 配色（冷→暖）
  Imbalance  ─ 同 BidxAsk 佈局；imbalance cell 加亮框提示

【圖層順序（由下至上）】
  ① 格底色（3 段強弱 or 量能 heat）
  ② 格邊框（極細極淡）
  ③ POC 框線（近端亮金 / 遠端暗金）
  ④ Imbalance 模式亮框（僅 Imbalance 模式）
  ⑤ 格內文字（依 mode + zoom band）
  ⑥ 角標 imbalance（小方塊；Imbalance 模式隱藏，改用亮框）
  ⑦ Stacked 側條
  ⑧ Wick 影線
  ⑨ POC extension lines（虛線，極淡；最近 8 根）

【Zoom LOD（tick 在螢幕的像素高度）】
  < 8px   ─ Z_NONE：純色塊，無文字
  8-17px  ─ Z_SINGLE：單欄或雙欄簡略文字（無分隔線）
  ≥ 18px  ─ Z_FULL：完整雙欄 bid×ask / delta / volume + 分隔線

【強弱分級門檻】
  dom = |bid_ratio - 0.5| × 2  ∈ [0, 1]
  dom < 0.30 → 弱（淡色）
  dom < 0.70 → 中
  dom ≥ 0.70 → 強（亮色）
"""
from __future__ import annotations

import datetime
from typing import List, Optional, Set, Tuple

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import Qt

import config
from core.data_types import FootprintCandle


# ── 顏色主題 ──────────────────────────────────────────────────────────────────
_C_NEUTRAL     = QtGui.QColor( 38,  42,  55, 120)   # 中性格底（最淡、皮底輔助）

# ── 買方優勢（bid_vol > ask_vol）─ 3 段青色 ───────────────────────────────────────
_C_BULL_WEAK   = QtGui.QColor( 28, 100,  92, 105)   # 淡青（輕微優勢）
_C_BULL_MID    = QtGui.QColor( 35, 152, 140, 165)   # 中青（明顯優勢）
_C_BULL_STRONG = QtGui.QColor( 38, 215, 198, 220)   # 亮青（極端優勢）

# ── 賣方優勢（ask_vol > bid_vol）─ 3 段紅色 ───────────────────────────────────────
_C_BEAR_WEAK   = QtGui.QColor(115,  40,  40, 105)   # 淡紅
_C_BEAR_MID    = QtGui.QColor(188,  58,  58, 165)   # 中紅
_C_BEAR_STRONG = QtGui.QColor(239,  83,  80, 220)   # 亮紅

# ── POC 框線（近/ 遠雙策略）────────────────────────────────────────────────────────────────
_C_POC_BRIGHT  = QtGui.QColor(255, 215,   0, 220)   # 近 N 根高亮 gold
_C_POC_DIM     = QtGui.QColor(165, 135,   0,  70)   # 更舊: 暗金細框，不強引注意
_POC_RECENT_N  = 15                                  # 最近幾根 = 高亮 POC

# ── Imbalance ────────────────────────────────────────────────────────────────────────
_C_IMB_BUY     = QtGui.QColor( 38, 200, 185, 190)   # 買方 imbalance 角標
_C_IMB_SELL    = QtGui.QColor(239,  83,  80, 190)   # 賣方 imbalance 角標
_C_STACK_BUY   = QtGui.QColor( 38, 166, 154, 120)   # 買方 stacked 側條（比前更淡更細）
_C_STACK_SELL  = QtGui.QColor(239,  83,  80, 120)   # 賣方 stacked 側條

# ── 格線 / 文字 ─────────────────────────────────────────────────────────────────────────────
_C_BORDER      = QtGui.QColor( 50,  55,  68,  90)   # 格線（極細極淡）
_C_DIVIDER     = QtGui.QColor( 85,  90, 110, 130)   # 中央分隔線
_C_TEXT_SELL   = QtGui.QColor(255, 115, 112)         # 賣量文字
_C_TEXT_BUY    = QtGui.QColor( 60, 220, 200)         # 買量文字
_C_TEXT_NEU    = QtGui.QColor(145, 150, 170)         # delta 中性（更低調）

# ── POC Extension Line ─────────────────────────────────────────────────────────
_C_POC_EXT     = QtGui.QColor(165, 135,   0,  40)   # 極淡暗金虛線
_POC_EXT_N     = 8                                    # 只畫最近 N 根的 POC extension

# ── Volume Heat（Volume 模式專用底色）──────────────────────────────────────────
_C_VOL_COLD    = QtGui.QColor( 30,  35,  55, 100)   # 低量（冷暗）
_C_VOL_WARM    = QtGui.QColor(120, 100,  40, 150)   # 中量
_C_VOL_HOT     = QtGui.QColor(220, 165,  30, 210)   # 高量（亮金）

# ── Imbalance 模式亮框 ───────────────────────────────────────────────────────────
_C_IMB_HL_BUY  = QtGui.QColor( 38, 215, 198, 200)   # 買方 imbalance 亮框
_C_IMB_HL_SELL = QtGui.QColor(239,  83,  80, 200)   # 賣方 imbalance 亮框

# ── 渲染常數 ──────────────────────────────────────────────────────────────────
_IMBALANCE_RATIO = 3.0    # 3:1 視為 imbalanced
_MIN_STACK       = 3      # ≥ 幾格連續算 stacked imbalance
_HALF_W          = 0.44   # 格半寬（x 方向）
_BAR_W           = 0.032  # stacked 側條寬（降低，比前細）
_MARKER_FRAC     = 0.24   # imbalance 角標佔格高比例（稍縮小）

# ── Zoom band ─────────────────────────────────────────────────────────────────
_Z_NONE   = 0   # < 8px/tick  ：純色塊
_Z_SINGLE = 1   # 8-17px/tick ：單值（delta）
_Z_FULL   = 2   # ≥ 18px/tick ：「賣量 ╱ 買量」雙欄


def _cell_color(bid_vol: float, ask_vol: float) -> QtGui.QColor:
    """
    依 bid/ask 比例回傳對應的 3 段強弱顏色。
    bid_vol 為 taker buy（買方主動），ask_vol 為 taker sell（賣方主動）。
      支配度 dom = |bid_ratio - 0.5| * 2  ∈ [0, 1]
      dom < 0.30 → 弱；dom < 0.70 → 中；dom ≥ 0.70 → 強
    """
    total = bid_vol + ask_vol
    if total <= 0.0:
        return _C_NEUTRAL
    bid_r = bid_vol / total
    dom   = abs(bid_r - 0.5) * 2.0   # 支配度 0..1

    if bid_r >= 0.5:                  # 買方優勢（青色系）
        if dom < 0.30:   return _C_BULL_WEAK
        elif dom < 0.70: return _C_BULL_MID
        else:            return _C_BULL_STRONG
    else:                             # 賣方優勢（紅色系）
        if dom < 0.30:   return _C_BEAR_WEAK
        elif dom < 0.70: return _C_BEAR_MID
        else:            return _C_BEAR_STRONG


def _volume_heat_color(vol: float, max_vol: float) -> QtGui.QColor:
    """Volume heat — 依據成交量占比分 3 段冷暖色。"""
    if max_vol <= 0 or vol <= 0:
        return _C_VOL_COLD
    r = min(vol / max_vol, 1.0)
    if r < 0.33:
        return _C_VOL_COLD
    if r < 0.66:
        return _C_VOL_WARM
    return _C_VOL_HOT


def _fmt(v: float) -> str:
    """成交量縮短格式（─ / 1.2K / 234 / 12.3 / 0.001）。"""
    if v == 0:
        return "─"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    if v >= 10:
        return f"{v:.0f}"
    if v >= 1:
        return f"{v:.1f}"
    return f"{v:.3f}"


def _fmt_delta(v: float) -> str:
    """Delta 帶正負號格式（+1.2K / -345 / 0）。"""
    if v == 0:
        return "0"
    sign = "+" if v > 0 else ""
    a = abs(v)
    if a >= 1_000_000:
        return f"{sign}{v / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"{sign}{v / 1_000:.1f}K"
    if a >= 10:
        return f"{sign}{v:.0f}"
    if a >= 1:
        return f"{sign}{v:.1f}"
    return f"{sign}{v:.3f}"


def _zoom_band(tick_px: float) -> int:
    """依據 tick 的像素高度決定顯示層級。"""
    if tick_px < 8:
        return _Z_NONE
    if tick_px < 18:
        return _Z_SINGLE
    return _Z_FULL


def _stacked_ranges(
    imb_prices: List[float],
    tick_size: float,
) -> List[Tuple[float, float]]:
    """
    回傳 ≥ _MIN_STACK 個連續（相差 tick_size）imbalanced 格的 (low, high) 區間。
    imb_prices 不需事先排序。
    """
    if len(imb_prices) < _MIN_STACK:
        return []
    eps = tick_size * 0.01
    sorted_p = sorted(imb_prices)
    ranges: List[Tuple[float, float]] = []
    run_start = sorted_p[0]
    run_end   = sorted_p[0]
    run_cnt   = 1
    for i in range(1, len(sorted_p)):
        if abs(sorted_p[i] - sorted_p[i - 1] - tick_size) < eps:
            run_end = sorted_p[i]
            run_cnt += 1
        else:
            if run_cnt >= _MIN_STACK:
                ranges.append((run_start, run_end))
            run_start = sorted_p[i]
            run_end   = sorted_p[i]
            run_cnt   = 1
    if run_cnt >= _MIN_STACK:
        ranges.append((run_start, run_end))
    return ranges


# ── FootprintItem ─────────────────────────────────────────────────────────────
class FootprintItem(pg.GraphicsObject):
    """
    Footprint 核心渲染物件（pyqtgraph GraphicsObject）。
    x 軸 = candle index，y 軸 = price（與 KlineChart 相同座標）。
    渲染結果快取於 QPicture；zoom band 改變時重建。
    """

    def __init__(self, tick_size: float = 1.0) -> None:
        pg.GraphicsObject.__init__(self)
        self._candles:      List[FootprintCandle]    = []
        self._x_positions:  List[int]                = []
        self._tick_size:    float                    = tick_size
        self._mode:         str                      = "BidxAsk"
        self._picture:      Optional[QtGui.QPicture] = None
        self._cur_zband:    int                      = -1
        self._text_cmds:    list                     = []  # (dx,dy,dw,dh,align,text,color,font_px)

    # ── public API ───────────────────────────────────────────────────────────

    def set_mode(self, mode: str) -> None:
        if mode in config.FOOTPRINT_MODES and mode != self._mode:
            self._mode = mode
            self._invalidate()

    def set_tick_size(self, ts: float) -> None:
        if ts != self._tick_size:
            self._tick_size = ts
            self._invalidate()

    def set_candles(self, candles: List[FootprintCandle],
                    x_positions: Optional[List[int]] = None) -> None:
        self._candles = candles
        self._x_positions = (x_positions if x_positions is not None
                             else list(range(len(candles))))
        self._invalidate()
        self.prepareGeometryChange()
        self.informViewBoundsChanged()
        self.update()

    # ── pyqtgraph 介面 ────────────────────────────────────────────────────────

    def boundingRect(self) -> QtCore.QRectF:
        if not self._candles:
            return QtCore.QRectF(0, 0, 1, 1)
        xp = self._x_positions
        lo_x = min(xp) if xp else 0
        hi_x = max(xp) if xp else 0
        valid = [c for c in self._candles if c.levels or c.high > 0]
        if not valid:
            return QtCore.QRectF(lo_x - 0.5, 0, hi_x - lo_x + 1.5, 1)
        lo = min((min(c.levels) if c.levels else c.low)  for c in valid)
        hi = max(
            (max(c.levels) + self._tick_size if c.levels else c.high)
            for c in valid
        )
        mg = self._tick_size * 2.5
        return QtCore.QRectF(lo_x - 0.5, lo - mg, hi_x - lo_x + 1.5, (hi - lo) + mg * 2)

    def paint(self, p: QtGui.QPainter, *args) -> None:
        if not self._candles:
            return
        tick_px = abs(self._tick_size * p.worldTransform().m22())
        band = _zoom_band(tick_px)
        if self._picture is None or band != self._cur_zband:
            self._cur_zband = band
            self._build(tick_px, band)
        self._picture.play(p)

        # ── 文字渲染（螢幕座標）──────────────────────────────────────────
        if self._text_cmds:
            tr = p.worldTransform()
            p.save()
            p.resetTransform()
            font = QtGui.QFont("Consolas")
            cur_fpx = -1
            for (dx, dy, dw, dh, align, text, color, fpx) in self._text_cmds:
                if fpx != cur_fpx:
                    cur_fpx = fpx
                    font.setPixelSize(fpx)
                    p.setFont(font)
                tl = tr.map(QtCore.QPointF(dx, dy))
                br = tr.map(QtCore.QPointF(dx + dw, dy + dh))
                sx = min(tl.x(), br.x())
                sy = min(tl.y(), br.y())
                sw = abs(br.x() - tl.x())
                sh = abs(br.y() - tl.y())
                p.setPen(QtGui.QPen(color))
                p.drawText(QtCore.QRectF(sx, sy, sw, sh), align, text)
            p.restore()

    # ── 內部 ─────────────────────────────────────────────────────────────────

    def _invalidate(self) -> None:
        self._picture   = None
        self._cur_zband = -1
        self._text_cmds = []
        self.update()

    def _build(self, tick_px: float, band: int) -> None:
        """構建所有 K 棒的 QPicture 快取（支援 4 模式 + POC extension）。"""
        pic = QtGui.QPicture()
        pa  = QtGui.QPainter(pic)

        font_px = max(7, min(11, int(tick_px * 0.52)))
        self._text_cmds = []

        ts          = self._tick_size
        half        = _HALF_W
        mode        = self._mode
        border_pen  = pg.mkPen(_C_BORDER,    width=0.2)
        div_pen     = pg.mkPen(_C_DIVIDER,   width=0.3)
        poc_bright  = pg.mkPen(_C_POC_BRIGHT, width=1.5)
        poc_dim     = pg.mkPen(_C_POC_DIM,    width=0.5)

        n_candles   = len(self._candles)
        recent_cut  = max(0, n_candles - _POC_RECENT_N)
        ext_cut     = max(0, n_candles - _POC_EXT_N)

        # Volume 模式：全域最大 level 量（用來歸一化 heat 色）
        global_max_vol = 1.0
        if mode == "Volume":
            for c in self._candles:
                for lv in c.levels.values():
                    if lv.total > global_max_vol:
                        global_max_vol = lv.total

        # POC extension 暫存
        poc_ext_data: List[Tuple[int, float]] = []

        # ── 逐根 K 棒 ──────────────────────────────────────────────────────
        for local_idx, candle in enumerate(self._candles):
            xi = self._x_positions[local_idx]  # kline-aligned x coordinate
            if not candle.levels:
                _draw_bare_candle(pa, xi, candle, half)
                continue

            sorted_prices = sorted(candle.levels.keys())

            # POC
            poc_price = max(sorted_prices, key=lambda pr: candle.levels[pr].total)
            poc_pen   = poc_bright if local_idx >= recent_cut else poc_dim
            if local_idx >= ext_cut:
                poc_ext_data.append((xi, poc_price))

            # Imbalance 分類
            buy_imb:  List[float] = []
            sell_imb: List[float] = []
            for pr in sorted_prices:
                lv = candle.levels[pr]
                b, a = lv.bid_vol, lv.ask_vol
                if a == 0 and b > 0:
                    buy_imb.append(pr)
                elif b == 0 and a > 0:
                    sell_imb.append(pr)
                elif a > 0 and b / a >= _IMBALANCE_RATIO:
                    buy_imb.append(pr)
                elif b > 0 and a / b >= _IMBALANCE_RATIO:
                    sell_imb.append(pr)

            buy_imb_set:  Set[float] = set(buy_imb)
            sell_imb_set: Set[float] = set(sell_imb)

            buy_stk_ranges  = _stacked_ranges(buy_imb,  ts)
            sell_stk_ranges = _stacked_ranges(sell_imb, ts)

            buy_stk_set: Set[float] = set()
            for lo_r, hi_r in buy_stk_ranges:
                for pr in sorted_prices:
                    if lo_r <= pr <= hi_r:
                        buy_stk_set.add(pr)
            sell_stk_set: Set[float] = set()
            for lo_r, hi_r in sell_stk_ranges:
                for pr in sorted_prices:
                    if lo_r <= pr <= hi_r:
                        sell_stk_set.add(pr)

            mk = ts * _MARKER_FRAC

            # ── 逐格渲染 ────────────────────────────────────────────────────
            for pr in sorted_prices:
                lv   = candle.levels[pr]
                rect = QtCore.QRectF(xi - half, pr, 2.0 * half, ts)

                # ① 背景色
                if mode == "Volume":
                    pa.fillRect(rect, _volume_heat_color(lv.total, global_max_vol))
                else:
                    pa.fillRect(rect, _cell_color(lv.bid_vol, lv.ask_vol))

                # ② 格框
                pa.setPen(border_pen)
                pa.drawRect(rect)

                # ③ POC 框
                if pr == poc_price:
                    pa.setPen(poc_pen)
                    pa.drawRect(rect)

                # ④ Imbalance 模式：imbalance cell 亮框（取代角標）
                if mode == "Imbalance":
                    if pr in buy_imb_set:
                        pa.setPen(pg.mkPen(_C_IMB_HL_BUY, width=1.2))
                        pa.drawRect(rect)
                    elif pr in sell_imb_set:
                        pa.setPen(pg.mkPen(_C_IMB_HL_SELL, width=1.2))
                        pa.drawRect(rect)

                # ⑤ 文字（依 mode + zoom band）
                self._collect_cell_text(pa, band, mode, lv, xi, pr, ts, half,
                                        div_pen, font_px)

                # ⑥ 角標 imbalance（Imbalance 模式已用亮框，不重複畫角標）
                if mode != "Imbalance" and band >= _Z_SINGLE:
                    if pr in buy_imb_set and pr not in buy_stk_set:
                        pa.fillRect(
                            QtCore.QRectF(xi + half - mk, pr + ts - mk, mk, mk),
                            _C_IMB_BUY,
                        )
                    elif pr in sell_imb_set and pr not in sell_stk_set:
                        pa.fillRect(
                            QtCore.QRectF(xi - half, pr, mk, mk),
                            _C_IMB_SELL,
                        )

            # ⑥½ K 棒匯總（bid/ask 總量 · delta · volume）
            if band >= _Z_SINGLE and sorted_prices:
                total_bid = sum(lv.bid_vol for lv in candle.levels.values())
                total_ask = sum(lv.ask_vol for lv in candle.levels.values())
                bot_price = min(sorted_prices)
                sum_font_px = max(6, min(10, int(tick_px * 0.42)))
                A = Qt.AlignmentFlag

                if mode in ("BidxAsk", "Imbalance"):
                    row1_y = bot_price - ts * 1.15
                    row1_h = ts * 0.55
                    row2_y = bot_price - ts * 1.75
                    row2_h = ts * 0.55
                    self._text_cmds.append((
                        xi - half, row1_y, half, row1_h,
                        A.AlignRight | A.AlignVCenter, _fmt(total_ask),
                        _C_TEXT_SELL, sum_font_px))
                    self._text_cmds.append((
                        xi, row1_y, half, row1_h,
                        A.AlignLeft | A.AlignVCenter, _fmt(total_bid),
                        _C_TEXT_BUY, sum_font_px))
                    self._text_cmds.append((
                        xi - half, row2_y, 2.0 * half, row2_h,
                        A.AlignCenter, f"Σ{_fmt(total_bid + total_ask)}",
                        _C_TEXT_NEU, sum_font_px))
                elif mode == "Delta":
                    delta = total_bid - total_ask
                    row1_y = bot_price - ts * 1.15
                    row1_h = ts * 0.55
                    row2_y = bot_price - ts * 1.75
                    row2_h = ts * 0.55
                    color = _C_TEXT_BUY if delta > 0 else _C_TEXT_SELL if delta < 0 else _C_TEXT_NEU
                    self._text_cmds.append((
                        xi - half, row1_y, 2.0 * half, row1_h,
                        A.AlignCenter, _fmt_delta(delta),
                        color, sum_font_px))
                    self._text_cmds.append((
                        xi - half, row2_y, half, row2_h,
                        A.AlignRight | A.AlignVCenter, _fmt(total_ask),
                        _C_TEXT_SELL, sum_font_px))
                    self._text_cmds.append((
                        xi, row2_y, half, row2_h,
                        A.AlignLeft | A.AlignVCenter, _fmt(total_bid),
                        _C_TEXT_BUY, sum_font_px))
                elif mode == "Volume":
                    total_vol = total_bid + total_ask
                    row1_y = bot_price - ts * 1.15
                    row1_h = ts * 0.55
                    row2_y = bot_price - ts * 1.75
                    row2_h = ts * 0.55
                    self._text_cmds.append((
                        xi - half, row1_y, 2.0 * half, row1_h,
                        A.AlignCenter, f"Σ{_fmt(total_vol)}",
                        QtGui.QColor(220, 180, 50), sum_font_px))
                    self._text_cmds.append((
                        xi - half, row2_y, half, row2_h,
                        A.AlignRight | A.AlignVCenter, _fmt(total_ask),
                        _C_TEXT_SELL, sum_font_px))
                    self._text_cmds.append((
                        xi, row2_y, half, row2_h,
                        A.AlignLeft | A.AlignVCenter, _fmt(total_bid),
                        _C_TEXT_BUY, sum_font_px))

            # ⑦ Stacked 側條
            for lo_r, hi_r in buy_stk_ranges:
                pa.fillRect(
                    QtCore.QRectF(xi + half, lo_r, _BAR_W, hi_r - lo_r + ts),
                    _C_STACK_BUY,
                )
            for lo_r, hi_r in sell_stk_ranges:
                pa.fillRect(
                    QtCore.QRectF(xi - half - _BAR_W, lo_r, _BAR_W, hi_r - lo_r + ts),
                    _C_STACK_SELL,
                )

            # ⑧ Wick
            if candle.high > 0 and candle.low > 0:
                is_bull  = candle.close >= candle.open
                wick_col = QtGui.QColor(
                    config.COLOR_UP if is_bull else config.COLOR_DOWN
                )
                pa.setPen(pg.mkPen(wick_col, width=1.0))
                top_lv = max(sorted_prices) + ts
                bot_lv = min(sorted_prices)
                if candle.high > top_lv:
                    pa.drawLine(
                        QtCore.QPointF(xi, top_lv),
                        QtCore.QPointF(xi, candle.high),
                    )
                if candle.low < bot_lv:
                    pa.drawLine(
                        QtCore.QPointF(xi, candle.low),
                        QtCore.QPointF(xi, bot_lv),
                    )

        # ⑨ POC extension lines（虛線，極淡；從 POC 往右延伸至最後一根）
        if poc_ext_data and len(self._x_positions) > 1:
            ext_pen = pg.mkPen(_C_POC_EXT, width=0.8,
                               style=Qt.PenStyle.DashLine)
            pa.setPen(ext_pen)
            last_xi = self._x_positions[-1]
            last_x = last_xi + half
            for src_xi, poc_pr in poc_ext_data:
                if src_xi >= last_xi:
                    continue   # 最後一根不畫（無空間延伸）
                y_mid = poc_pr + ts * 0.5
                pa.drawLine(
                    QtCore.QPointF(src_xi + half + 0.02, y_mid),
                    QtCore.QPointF(last_x, y_mid),
                )

        pa.end()
        self._picture = pic

    # ── 單格文字收集（依 mode + zoom band）──────────────────────────────────

    def _collect_cell_text(
        self, pa: QtGui.QPainter, band: int, mode: str,
        lv, idx: int, pr: float, ts: float, half: float,
        div_pen, font_px: int,
    ) -> None:
        if band == _Z_NONE:
            return

        A = Qt.AlignmentFlag

        if mode in ("BidxAsk", "Imbalance"):
            if band == _Z_FULL:
                pad_x  = half * 0.06
                pad_y  = ts   * 0.08
                cell_h = ts - 2.0 * pad_y
                # 分隔線留在 QPicture（幾何圖形可正常縮放）
                pa.setPen(div_pen)
                pa.drawLine(
                    QtCore.QPointF(idx, pr + ts * 0.12),
                    QtCore.QPointF(idx, pr + ts * 0.88),
                )
                self._text_cmds.append((
                    idx - half + pad_x, pr + pad_y, half - pad_x, cell_h,
                    A.AlignRight | A.AlignVCenter, _fmt(lv.ask_vol),
                    _C_TEXT_SELL, font_px))
                self._text_cmds.append((
                    idx + pad_x, pr + pad_y, half - pad_x, cell_h,
                    A.AlignLeft | A.AlignVCenter, _fmt(lv.bid_vol),
                    _C_TEXT_BUY, font_px))
            else:  # Z_SINGLE
                hr = half * 0.96
                self._text_cmds.append((
                    idx - hr, pr, hr, ts,
                    A.AlignRight | A.AlignVCenter, _fmt(lv.ask_vol),
                    _C_TEXT_SELL, font_px))
                self._text_cmds.append((
                    idx, pr, hr, ts,
                    A.AlignLeft | A.AlignVCenter, _fmt(lv.bid_vol),
                    _C_TEXT_BUY, font_px))

        elif mode == "Delta":
            delta = lv.bid_vol - lv.ask_vol
            txt = _fmt_delta(delta)
            color = _C_TEXT_BUY if delta > 0 else _C_TEXT_SELL if delta < 0 else _C_TEXT_NEU
            self._text_cmds.append((
                idx - half, pr, 2.0 * half, ts,
                A.AlignCenter, txt, color, font_px))

        elif mode == "Volume":
            self._text_cmds.append((
                idx - half, pr, 2.0 * half, ts,
                A.AlignCenter, _fmt(lv.total), _C_TEXT_NEU, font_px))


# ─────────────────────────────────────────────────────────────────────────────
# 輔助函式
# ─────────────────────────────────────────────────────────────────────────────

def _draw_bare_candle(
    pa: QtGui.QPainter,
    idx: int,
    candle: FootprintCandle,
    half: float,
) -> None:
    """無 Footprint 資料時繪製標準 OHLC K 棒。"""
    if candle.high == candle.low == 0:
        return
    is_bull = candle.close >= candle.open
    col = QtGui.QColor(config.COLOR_UP if is_bull else config.COLOR_DOWN)
    pa.setPen(pg.mkPen(col, width=1))
    pa.setBrush(pg.mkBrush(col))
    pa.drawLine(
        QtCore.QPointF(idx, candle.low),
        QtCore.QPointF(idx, candle.high),
    )
    body_h = abs(candle.close - candle.open) or (candle.high - candle.low) * 0.01
    pa.drawRect(
        QtCore.QRectF(
            idx - half, min(candle.open, candle.close), 2.0 * half, body_h
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
# _TimeAxis — candle index ⇒ 時間字串
# ─────────────────────────────────────────────────────────────────────────────

class _TimeAxis(pg.AxisItem):
    """
    將 candle index 映射到 open_time 時間字串的自訂 x 軸。
    顯示格式依可見時間範圍自動切換：HH:MM 或 MM/DD HH:MM。
    """

    def __init__(self) -> None:
        super().__init__(orientation='bottom')
        self._idx_to_ms: dict[int, int] = {}
        self.setStyle(tickLength=-6, tickTextOffset=4)

    def set_times(self, candles: list,
                  x_positions: Optional[List[int]] = None) -> None:
        if x_positions is not None:
            self._idx_to_ms = {xp: int(c.open_time)
                               for xp, c in zip(x_positions, candles)}
        else:
            self._idx_to_ms = {i: int(c.open_time) for i, c in enumerate(candles)}

    def tickStrings(self, values, scale, spacing) -> list:
        if not self._idx_to_ms:
            return ['' for _ in values]
        ms_vals = list(self._idx_to_ms.values())
        span_ms = max(ms_vals) - min(ms_vals) if len(ms_vals) > 1 else 0
        fmt = '%H:%M' if span_ms < 86_400_000 else '%m/%d %H:%M'
        result = []
        for v in values:
            idx = int(round(v))
            ms  = self._idx_to_ms.get(idx)
            if ms is not None:
                dt = datetime.datetime.fromtimestamp(ms / 1000, tz=config.DISPLAY_TZ)
                result.append(dt.strftime(fmt))
            else:
                result.append('')
        return result


# ─────────────────────────────────────────────────────────────────────────────
# FootprintChart — PlotWidget 包裝
# ─────────────────────────────────────────────────────────────────────────────

class FootprintChart(pg.PlotWidget):
    """
    完整的 Footprint PlotWidget。
    外部 API：
      set_mode(mode)              ─ 切換 BidxAsk / Delta 顯示模式
      set_tick_size(ts)           ─ 更新價格分桶大小
      set_kline_timestamps(ts)   ─ 設定 kline open_time → index 映射
      update_candles(list)        ─ 傳入最新的 FootprintCandle 列表
      get_plot_item()             ─ 取得 PlotItem 供 x 軸連結
    """

    def __init__(self, parent=None) -> None:
        self._time_axis = _TimeAxis()
        super().__init__(
            parent,
            background=config.COLOR_BG,
            axisItems={'bottom': self._time_axis},
        )

        pi = self.getPlotItem()
        pi.showGrid(x=True, y=True, alpha=0.08)
        pi.setLabel("left", "Price")
        pi.hideButtons()
        pi.enableAutoRange(axis='x', enable=False)
        self._time_axis.setHeight(30)   # 為時間文字留足空間

        self._fp_item = FootprintItem()
        pi.addItem(self._fp_item)

        self._kline_ts_to_idx: dict[int, int] = {}

    def set_mode(self, mode: str) -> None:
        self._fp_item.set_mode(mode)

    def set_tick_size(self, ts: float) -> None:
        self._fp_item.set_tick_size(ts)

    def set_kline_timestamps(self, timestamps: List[int]) -> None:
        """Store kline open_time → index mapping for x-axis alignment."""
        self._kline_ts_to_idx = {ts: i for i, ts in enumerate(timestamps)}

    def update_candles(self, candles: List[FootprintCandle]) -> None:
        x_positions = [self._kline_ts_to_idx.get(int(c.open_time), i)
                       for i, c in enumerate(candles)]
        self._fp_item.set_candles(candles, x_positions)
        self._time_axis.set_times(candles, x_positions)

    def get_plot_item(self) -> pg.PlotItem:
        return self.getPlotItem()

