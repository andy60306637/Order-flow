"""
ui/pipeline_studio.py — Pipeline 策略設計室（第四主頁面）
"""
from __future__ import annotations

import inspect
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QComboBox,
    QGroupBox, QTextEdit, QSplitter, QFrame, QScrollArea,
    QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from strategies import STRATEGY_REGISTRY
from strategies.pipeline.strategy import MultiPipelineStrategy


# ── Stage visual metadata ──────────────────────────────────────────────────────

# cls_name → (header_hex, body_hex)
_STYLE: dict[str, tuple[str, str]] = {
    "PositionGateStage":    ("#1b5e20", "#2e7d32"),
    "CooldownStage":        ("#1b5e20", "#388e3c"),
    "RegimeStage":          ("#0d47a1", "#1565c0"),
    "AlphaStage":           ("#4a148c", "#6a1b9a"),
    "EntryManagementStage": ("#bf360c", "#d84315"),
    "VolumeAreaStage":      ("#37474f", "#546e7a"),
    "RRStage":              ("#004d40", "#00695c"),
    "FeeCoverRatioStage":   ("#b71c1c", "#c62828"),
    "FeeStage":             ("#b71c1c", "#c62828"),
    "TickFactorStage":      ("#4e342e", "#6d4c41"),
}
_DEFAULT_STYLE = ("#263238", "#37474f")

_GATE_CLS = {"PositionGateStage", "CooldownStage"}

_KIND_BADGE: dict[str, str] = {
    "RegimeStage":          "STAGE 1 ▸ Regime",
    "AlphaStage":           "STAGE 2 ▸ Alpha",
    "EntryManagementStage": "STAGE 3 ▸ Entry",
    "VolumeAreaStage":      "STAGE 3b ▸ Volume",
    "RRStage":              "STAGE 4 ▸ RR",
    "FeeCoverRatioStage":   "STAGE 4b ▸ FeeCover",
    "FeeStage":             "STAGE 4b ▸ Fee",
    "TickFactorStage":      "STAGE ▸ TickFactor",
}


def _style(stage) -> tuple[str, str]:
    return _STYLE.get(type(stage).__name__, _DEFAULT_STYLE)


# ── Parameter summary (card body) ─────────────────────────────────────────────

def _param_lines(stage) -> list[str]:
    cls = type(stage).__name__

    if cls == "RegimeStage":
        lines: list[str] = []
        for comp in (getattr(stage, "components", None) or []):
            lines.append(f"  ● {type(comp).__name__}")
        abd = getattr(stage, "allowed_by_dimension", {}) or {}
        for dim, vals in abd.items():
            dim_s = dim.replace("market_vol_regime", "vol_regime")
            lines.append(f"    {dim_s}: {', '.join(sorted(vals))}")
        return lines

    if cls == "AlphaStage":
        lines = [f"  mode: {getattr(stage, 'mode', '?')}"]
        for m in (getattr(stage, "modules", None) or []):
            lines.append(f"  ● {getattr(m, 'name', type(m).__name__)}")
        return lines

    # Generic: scalar attrs only
    skip = {"name"}
    lines = []
    for k, v in vars(stage).items():
        if k.startswith("_") or k in skip:
            continue
        if isinstance(v, bool):
            lines.append(f"  {k}: {v}")
        elif isinstance(v, (int, float)):
            lines.append(f"  {k}: {v}")
        elif isinstance(v, str) and v:
            lines.append(f"  {k}: {v}")
    return lines[:7]


# ── Full detail text (left panel) ─────────────────────────────────────────────

def _full_detail(stage) -> str:
    cls = type(stage).__name__
    parts = [f"▌ {cls}", ""]

    doc = (type(stage).__doc__ or "").strip()
    if doc:
        first_para = doc.split("\n\n")[0]
        cleaned = "\n".join(ln.strip() for ln in first_para.splitlines()).strip()
        parts += [cleaned, ""]

    parts.append("── Parameters ─────────────────────────────")

    if cls == "RegimeStage":
        comps = getattr(stage, "components", []) or []
        abd   = getattr(stage, "allowed_by_dimension", {}) or {}
        parts.append(f"components ({len(comps)}):")
        for c in comps:
            parts.append(f"  [{type(c).__name__}]")
            for ck, cv in vars(c).items():
                if ck.startswith("_"):
                    continue
                if isinstance(cv, (int, float, bool, str)):
                    parts.append(f"    {ck} = {cv}")
        parts += ["", "allowed_by_dimension:"]
        for dim, vals in abd.items():
            parts.append(f"  {dim}: {sorted(vals)}")
        return "\n".join(parts)

    if cls == "AlphaStage":
        mods = getattr(stage, "modules", []) or []
        parts.append(f"mode      = {getattr(stage, 'mode', '?')}")
        parts.append(f"min_score = {getattr(stage, 'min_score', '?')}")
        parts.append(f"weights   = {getattr(stage, 'weights', [])}")
        parts += [f"", f"modules ({len(mods)}):"]
        for m in mods:
            parts.append(f"  [{type(m).__name__}]  name={getattr(m,'name','?')}")
            for mk, mv in vars(m).items():
                if mk.startswith("_"):
                    continue
                if isinstance(mv, (int, float, bool, str)):
                    parts.append(f"    {mk} = {mv}")
        return "\n".join(parts)

    # Generic
    for k, v in vars(stage).items():
        if k.startswith("_"):
            continue
        if isinstance(v, (int, float, bool, str)):
            parts.append(f"{k} = {v}")
        elif v is None:
            parts.append(f"{k} = None")
        elif isinstance(v, list):
            parts.append(f"{k} ({len(v)} items):")
            for item in v:
                n = getattr(item, "name", None) or type(item).__name__
                parts.append(f"  {n}")
        elif hasattr(v, "__dict__"):
            parts.append(f"{k}:")
            for vk, vv in vars(v).items():
                if not vk.startswith("_") and isinstance(vv, (int, float, bool, str, type(None))):
                    parts.append(f"  {vk} = {vv}")

    return "\n".join(parts)


# ── Stage Card Widget ─────────────────────────────────────────────────────────

class StageCard(QFrame):
    """Clickable card representing one pipeline stage."""

    clicked = pyqtSignal(object)

    def __init__(self, stage, badge_text: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._stage = stage
        header_c, body_c = _style(stage)

        self.setObjectName("StageCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(420)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        self.setStyleSheet(f"""
            QFrame#StageCard {{
                background-color: {body_c};
                border: 1px solid {header_c};
                border-radius: 8px;
            }}
            QFrame#StageCard:hover {{
                border: 2px solid #90caf9;
            }}
        """)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header strip
        hdr = QFrame()
        hdr.setFixedHeight(34)
        hdr.setStyleSheet(f"""
            QFrame {{
                background-color: {header_c};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                border-bottom-left-radius: 0;
                border-bottom-right-radius: 0;
            }}
        """)
        hr = QHBoxLayout(hdr)
        hr.setContentsMargins(10, 0, 10, 0)

        badge_lbl = QLabel(badge_text)
        badge_lbl.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        badge_lbl.setStyleSheet("color: rgba(255,255,255,160); background: transparent;")

        name_lbl = QLabel(getattr(stage, "name", type(stage).__name__))
        name_lbl.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        name_lbl.setStyleSheet("color: #ffffff; background: transparent;")
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        hr.addWidget(badge_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
        hr.addStretch()
        hr.addWidget(name_lbl, alignment=Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(hdr)

        # Body
        body = QFrame()
        body.setStyleSheet("QFrame { background: transparent; }")
        bv = QVBoxLayout(body)
        bv.setContentsMargins(12, 8, 12, 10)
        bv.setSpacing(2)

        params = _param_lines(stage)
        if params:
            for line in params:
                lbl = QLabel(line)
                lbl.setFont(QFont("Consolas", 8))
                lbl.setStyleSheet("color: #e0e0e0; background: transparent;")
                bv.addWidget(lbl)
        else:
            lbl = QLabel("  (no scalar params)")
            lbl.setFont(QFont("Consolas", 8))
            lbl.setStyleSheet("color: #757575; background: transparent;")
            bv.addWidget(lbl)

        root.addWidget(body)

    def mousePressEvent(self, event):
        self.clicked.emit(self._stage)
        super().mousePressEvent(event)


class _Arrow(QLabel):
    def __init__(self, parent=None):
        super().__init__("↓", parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(420, 26)
        self.setStyleSheet("color: #546e7a; font-size: 16px; background: transparent;")


# ── Pipeline Studio (main page) ───────────────────────────────────────────────

class PipelineStudio(QWidget):
    """Pipeline 策略設計室 — 視覺化 Pipeline 各階段。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._cache: dict[str, MultiPipelineStrategy] = {}
        self._setup_ui()
        self._populate_combo()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Toolbar
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("策略:"))
        self._combo = QComboBox()
        self._combo.setMinimumWidth(300)
        self._combo.setFont(QFont("Segoe UI", 10))
        self._combo.currentIndexChanged.connect(self._on_strategy_changed)
        toolbar.addWidget(self._combo)
        toolbar.addStretch()
        hint = QLabel("點擊 Stage 卡片查看詳細參數")
        hint.setStyleSheet("color: #607d8b; font-size: 11px;")
        toolbar.addWidget(hint)
        root.addLayout(toolbar)

        # Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, stretch=1)

        # ── Left: overview + detail ───────────────────────────
        left = QWidget()
        left.setMinimumWidth(240)
        left.setMaximumWidth(400)
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 6, 0)
        lv.setSpacing(6)

        ov_grp = QGroupBox("Pipeline 概覽")
        ov_lay = QVBoxLayout(ov_grp)
        self._overview_lbl = QLabel()
        self._overview_lbl.setFont(QFont("Consolas", 9))
        self._overview_lbl.setWordWrap(True)
        self._overview_lbl.setAlignment(Qt.AlignmentFlag.AlignTop)
        ov_lay.addWidget(self._overview_lbl)
        lv.addWidget(ov_grp)

        det_grp = QGroupBox("Stage 詳細資訊")
        det_lay = QVBoxLayout(det_grp)
        self._detail = QTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setFont(QFont("Consolas", 9))
        self._detail.setStyleSheet(
            "QTextEdit { background: #0d1117; color: #c9d1d9; border: none; }"
        )
        self._detail.setPlaceholderText("← 點擊右側 Stage 卡片")
        det_lay.addWidget(self._detail)
        lv.addWidget(det_grp, stretch=1)

        splitter.addWidget(left)

        # ── Right: scrollable pipeline canvas ────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: #0d1117; }")
        self._canvas = QWidget()
        self._canvas.setStyleSheet("QWidget { background: #0d1117; }")
        self._flow = QVBoxLayout(self._canvas)
        self._flow.setContentsMargins(24, 24, 24, 24)
        self._flow.setSpacing(0)
        self._flow.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        scroll.setWidget(self._canvas)
        splitter.addWidget(scroll)

        splitter.setSizes([300, 700])

    # ── Data ──────────────────────────────────────────────────

    def _populate_combo(self):
        self._combo.blockSignals(True)
        self._combo.clear()
        for name, cls in STRATEGY_REGISTRY.items():
            if inspect.isclass(cls) and issubclass(cls, MultiPipelineStrategy):
                self._combo.addItem(name, userData=name)
        self._combo.blockSignals(False)
        if self._combo.count() > 0:
            self._load(self._combo.currentData())

    def _on_strategy_changed(self, _: int):
        key = self._combo.currentData()
        if key:
            self._load(key)

    def _load(self, name: str):
        if name not in self._cache:
            cls = STRATEGY_REGISTRY.get(name)
            if cls is None:
                return
            try:
                self._cache[name] = cls()
            except Exception as exc:
                self._overview_lbl.setText(f"Error: {exc}")
                return
        self._render(self._cache[name])

    def _render(self, inst: MultiPipelineStrategy):
        # Clear canvas
        while self._flow.count():
            item = self._flow.takeAt(0)
            if w := item.widget():
                w.deleteLater()

        runner = getattr(inst, "_runner", None) or getattr(inst, "runner", None)
        defs   = getattr(runner, "_defs", None) or getattr(runner, "defs", []) if runner else []
        if not defs:
            self._overview_lbl.setText("No pipelines found.")
            return

        all_stages = [s for d in defs for s in self._stages(d.pipeline)]
        stage_count = len(all_stages)
        gate_count  = sum(1 for s in all_stages if type(s).__name__ in _GATE_CLS)

        self._overview_lbl.setText(
            f"Pipelines: {len(defs)}\n"
            f"Stages: {stage_count}  (Gates: {gate_count})\n\n"
            + "\n".join(f"● {d.name}  w={d.allocation_weight}" for d in defs)
        )

        for pi, pdef in enumerate(defs):
            if len(defs) > 1:
                sep = QLabel(f"── {pdef.name} (weight={pdef.allocation_weight}) ──")
                sep.setFont(QFont("Segoe UI", 9))
                sep.setStyleSheet(
                    "QLabel { color: #7986cb; margin: 10px 0 4px 0; background: transparent; }"
                )
                sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
                sep.setFixedWidth(420)
                self._flow.addWidget(sep, alignment=Qt.AlignmentFlag.AlignHCenter)

            stages = self._stages(pdef.pipeline)
            gate_n = 0
            for i, stage in enumerate(stages):
                cls_n = type(stage).__name__
                if cls_n in _GATE_CLS:
                    gate_n += 1
                    badge = f"GATE {gate_n}"
                else:
                    badge = _KIND_BADGE.get(cls_n, "STAGE")

                card = StageCard(stage, badge)
                card.clicked.connect(self._on_stage_click)
                self._flow.addWidget(card, alignment=Qt.AlignmentFlag.AlignHCenter)
                if i < len(stages) - 1:
                    self._flow.addWidget(_Arrow(), alignment=Qt.AlignmentFlag.AlignHCenter)

        self._flow.addStretch()

    @staticmethod
    def _stages(pipeline) -> list:
        val = getattr(pipeline, "stages", None)
        if isinstance(val, (list, tuple)):
            return list(val)
        try:
            return list(pipeline)
        except TypeError:
            return []

    def _on_stage_click(self, stage):
        self._detail.setPlainText(_full_detail(stage))
