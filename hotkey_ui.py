#!/usr/bin/env python3
"""Hotkey selector UI components for the Telemetry HUD.

Provides a minimal settings panel that blends into the HUD:
- Action titles (e.g. 'Toggle Visibility (Global)', 'Take Screenshot')
- Modifier checkboxes (Ctrl, Alt, Shift) with neutral check indicators
- Pill button for key recording
- Scrollable list of relevant hotkeys
- Flat 'Back to browser' button
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from hotkey import _KEYS, _MODIFIERS

if getattr(sys, "frozen", False):
    _CONFIG_PATH = Path(sys.executable).resolve().parent / "hotkeys.json"
else:
    _CONFIG_PATH = Path(__file__).resolve().parent / "hotkeys.json"

# Reverse lookup for special virtual keys
_SPECIAL_QT_KEYS: dict[int, str] = {
    int(Qt.Key.Key_Return): "enter",
    int(Qt.Key.Key_Enter): "enter",
    int(Qt.Key.Key_Tab): "tab",
    int(Qt.Key.Key_Backspace): "backspace",
    int(Qt.Key.Key_Delete): "delete",
    int(Qt.Key.Key_Insert): "insert",
    int(Qt.Key.Key_Home): "home",
    int(Qt.Key.Key_End): "end",
    int(Qt.Key.Key_PageUp): "pageup",
    int(Qt.Key.Key_PageDown): "pagedown",
    int(Qt.Key.Key_Left): "left",
    int(Qt.Key.Key_Right): "right",
    int(Qt.Key.Key_Up): "up",
    int(Qt.Key.Key_Down): "down",
    int(Qt.Key.Key_Space): "space",
    int(Qt.Key.Key_Slash): "/",
    int(Qt.Key.Key_Backslash): "\\",
    int(Qt.Key.Key_Minus): "-",
    int(Qt.Key.Key_Equal): "=",
    int(Qt.Key.Key_BracketLeft): "[",
    int(Qt.Key.Key_BracketRight): "]",
    int(Qt.Key.Key_Semicolon): ";",
    int(Qt.Key.Key_Apostrophe): "'",
    int(Qt.Key.Key_Comma): ",",
    int(Qt.Key.Key_Period): ".",
    int(Qt.Key.Key_QuoteLeft): "`",
}

_MODIFIER_KEYS = frozenset({
    int(Qt.Key.Key_Shift),
    int(Qt.Key.Key_Control),
    int(Qt.Key.Key_Alt),
    int(Qt.Key.Key_Meta),
    int(Qt.Key.Key_AltGr),
})


def qt_key_to_name(qt_key: int, text: str = "") -> str | None:
    """Translate a Qt key code or character into hotkey key specification."""
    qt_key = int(qt_key)
    if qt_key in _MODIFIER_KEYS:
        return None
    if qt_key in _SPECIAL_QT_KEYS:
        return _SPECIAL_QT_KEYS[qt_key]
    if int(Qt.Key.Key_0) <= qt_key <= int(Qt.Key.Key_9):
        return chr(ord("0") + (qt_key - int(Qt.Key.Key_0)))
    if int(Qt.Key.Key_A) <= qt_key <= int(Qt.Key.Key_Z):
        return chr(ord("a") + (qt_key - int(Qt.Key.Key_A)))
    if int(Qt.Key.Key_F1) <= qt_key <= int(Qt.Key.Key_F24):
        return f"f{qt_key - int(Qt.Key.Key_F1) + 1}"
    if text and len(text) == 1 and text.isprintable():
        char = text.lower()
        if char in _KEYS or char.isalnum():
            return char
    return None


def decompose_hotkey(spec: str) -> tuple[set[str], str]:
    """Decompose 'ctrl+alt+x' into ({'ctrl', 'alt'}, 'x')."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    modifiers: set[str] = set()
    key = ""
    for p in parts:
        if p in _MODIFIERS:
            standard = "ctrl" if p == "control" else ("win" if p in ("meta", "super") else p)
            modifiers.add(standard)
        else:
            key = p
    return modifiers, key


def format_hotkey(modifiers: set[str], key: str) -> str:
    """Format modifiers set and key into canonical 'ctrl+alt+x'."""
    ordered_mods = []
    for mod in ("ctrl", "alt", "shift", "win"):
        if mod in modifiers:
            ordered_mods.append(mod)
    key_clean = key.strip().lower()
    if not ordered_mods and not key_clean:
        return ""
    if not ordered_mods:
        return key_clean
    if not key_clean or key_clean == "-":
        return "+".join(ordered_mods)
    return f"{'+'.join(ordered_mods)}+{key_clean}"


def format_key_display(key: str) -> str:
    """Format key name for capsule pill display."""
    k = key.strip().lower()
    if not k or k == "-":
        return "-"
    if k in ("enter", "return", "ent"):
        return "Ent"
    if k in ("escape", "esc"):
        return "Esc"
    if k == "space":
        return "Space"
    if k == "backspace":
        return "Bksp"
    if k == "tab":
        return "Tab"
    if k == "delete":
        return "Del"
    if k == "arrows":
        return "Arrows"
    if k in ("\\", "\\\\"):
        return "\\"
    if k in ("left", "right", "up", "down"):
        return k.capitalize()
    if len(k) == 1:
        return k.upper() if k.isalnum() else k
    if k.startswith("f") and k[1:].isdigit():
        return k.upper()
    return k.capitalize()


class ModifierToggle(QWidget):
    """Clickable modifier control with label on left and green square checkbox on right."""

    toggled = pyqtSignal(bool)

    def __init__(self, label: str, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = label
        self._checked = bool(checked)
        self._hovered = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(QFont("Segoe UI", 9, QFont.Weight.Medium))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        fm = self.fontMetrics()
        self.setFixedWidth(fm.horizontalAdvance(self._label) + 28)

    @property
    def checked(self) -> bool:
        return self._checked

    @checked.setter
    def checked(self, value: bool) -> None:
        if self._checked != bool(value):
            self._checked = bool(value)
            self.update()

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        text_width = fm.horizontalAdvance(self._label)
        # text_width + spacing (6) + box (18) + margins (4)
        return QSize(text_width + 28, 26)

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._checked = not self._checked
            self.toggled.emit(self._checked)
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        h = self.height()
        box_size = 18.0
        box_y = (h - box_size) / 2.0
        box_x = self.width() - box_size - 2.0

        # Draw label text
        fm = self.fontMetrics()
        text_y = int((h + fm.ascent() - fm.descent()) / 2)
        text_color = QColor("#E2E8F0") if self._hovered or self._checked else QColor("#A0AEC0")
        painter.setPen(text_color)
        painter.drawText(2, text_y, self._label)

        # Draw box
        box_rect = QRectF(box_x, box_y, box_size, box_size)
        if self._checked:
            # Checked: neutral fill with a dark checkmark
            fill = QColor("#D7DEE8")
            painter.setBrush(QBrush(fill))
            painter.setPen(QPen(fill, 1.5))
            painter.drawRoundedRect(box_rect, 3.5, 3.5)

            # Draw checkmark
            path = QPainterPath()
            path.moveTo(box_x + 4.2, box_y + 9.5)
            path.lineTo(box_x + 7.5, box_y + 13.2)
            path.lineTo(box_x + 13.8, box_y + 5.2)

            check_pen = QPen(
                QColor("#161922"),
                2.0,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
            painter.setPen(check_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        else:
            # Unchecked: transparent box with subtle border
            border_color = QColor("#8A93A6") if self._hovered else QColor("#3F4554")
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(border_color, 1.5))
            painter.drawRoundedRect(box_rect, 3.5, 3.5)

        painter.end()


class KeyPillButton(QPushButton):
    """Rounded capsule button that displays key and listens for key presses when active."""

    key_changed = pyqtSignal(str)
    recording_changed = pyqtSignal(bool)

    def __init__(self, key: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._key = key
        self._recording = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(26)
        self.setMinimumWidth(44)
        self._update_appearance()
        self.clicked.connect(self._on_clicked)

    @property
    def key(self) -> str:
        return self._key

    @key.setter
    def key(self, value: str) -> None:
        self._key = value
        self._update_appearance()

    @property
    def recording(self) -> bool:
        return self._recording

    def _update_appearance(self) -> None:
        if self._recording:
            self.setText("...")
            self.setStyleSheet(
                "QPushButton { background-color: rgba(255, 255, 255, 0.10); "
                "color: #E2E8F0; border: 1.5px solid #8A93A6; border-radius: 6px; "
                "font-weight: bold; font-size: 11px; padding: 2px 10px; } "
                "QPushButton:hover { background-color: rgba(255, 255, 255, 0.16); }"
            )
        else:
            display_text = format_key_display(self._key)
            self.setText(display_text)
            self.setStyleSheet(
                "QPushButton { background-color: transparent; color: #E2E8F0; "
                "border: 1px solid rgba(255, 255, 255, 0.18); border-radius: 6px; "
                "font-weight: bold; font-size: 11px; padding: 2px 10px; } "
                "QPushButton:hover { background-color: rgba(255, 255, 255, 0.08); "
                "border-color: rgba(255, 255, 255, 0.30); }"
            )

    def _on_clicked(self) -> None:
        if self._recording:
            self.cancel_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        self._recording = True
        self._update_appearance()
        self.recording_changed.emit(True)

    def cancel_recording(self) -> None:
        self._recording = False
        self._update_appearance()
        self.recording_changed.emit(False)

    def handle_key_stroke(self, qt_key: int, text: str = "") -> bool:
        """Handle incoming key stroke while recording. Returns True if handled."""
        if not self._recording:
            return False

        if qt_key == int(Qt.Key.Key_Escape):
            self.cancel_recording()
            return True

        if qt_key in _MODIFIER_KEYS:
            return True

        name = qt_key_to_name(qt_key, text)
        if name:
            self._key = name
            self._recording = False
            self._update_appearance()
            self.key_changed.emit(self._key)
            self.recording_changed.emit(False)
            return True

        return False

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if self._recording:
            if self.handle_key_stroke(event.key(), event.text()):
                event.accept()
                return
        super().keyPressEvent(event)


class HotkeyRowCard(QFrame):
    """Card container for a single hotkey action."""

    hotkey_changed = pyqtSignal(str, str)  # action_id, new_spec
    recorder_activated = pyqtSignal(object)  # KeyPillButton

    def __init__(
        self,
        action_id: str,
        title: str,
        spec: str,
        is_fixed_key: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.action_id = action_id
        self._title = title
        self._is_fixed_key = is_fixed_key
        self._modifiers, self._key = decompose_hotkey(spec)
        if self._is_fixed_key and not self._key:
            self._key = "arrows"

        self._build_card()

    def _build_card(self) -> None:
        self.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 6, 2, 6)
        layout.setSpacing(8)

        # Title Label
        title_label = QLabel(self._title)
        title_label.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        title_label.setStyleSheet("color: #E2E8F0; background: transparent; border: none;")
        layout.addWidget(title_label)

        # Controls Row: Ctrl, Alt, Shift ... Key Pill
        controls_layout = QHBoxLayout()
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(16)

        self._ctrl_toggle = ModifierToggle("Ctrl", "ctrl" in self._modifiers, self)
        self._ctrl_toggle.toggled.connect(lambda c: self._on_modifier_toggled("ctrl", c))
        controls_layout.addWidget(self._ctrl_toggle)

        self._alt_toggle = ModifierToggle("Alt", "alt" in self._modifiers, self)
        self._alt_toggle.toggled.connect(lambda c: self._on_modifier_toggled("alt", c))
        controls_layout.addWidget(self._alt_toggle)

        self._shift_toggle = ModifierToggle("Shift", "shift" in self._modifiers, self)
        self._shift_toggle.toggled.connect(lambda c: self._on_modifier_toggled("shift", c))
        controls_layout.addWidget(self._shift_toggle)

        controls_layout.addStretch(1)

        self._key_pill = KeyPillButton(self._key, self)
        if self._is_fixed_key:
            self._key_pill.setEnabled(False)
            self._key_pill.setToolTip("Fixed to Arrow keys (Left, Up, Right, Down)")
        else:
            self._key_pill.key_changed.connect(self._on_key_changed)
            self._key_pill.recording_changed.connect(self._on_recording_changed)
        controls_layout.addWidget(self._key_pill)

        layout.addLayout(controls_layout)

        # Status / Error Label
        self._status_label = QLabel("")
        self._status_label.setFont(QFont("Segoe UI", 8))
        self._status_label.setStyleSheet("color: #F87171; background: transparent; border: none;")
        self._status_label.setVisible(False)
        layout.addWidget(self._status_label)

    @property
    def key_pill(self) -> KeyPillButton:
        return self._key_pill

    def _on_modifier_toggled(self, modifier: str, checked: bool) -> None:
        if checked:
            self._modifiers.add(modifier)
        else:
            self._modifiers.discard(modifier)
        self._emit_change()

    def _on_key_changed(self, new_key: str) -> None:
        self._key = new_key
        self._emit_change()

    def _on_recording_changed(self, active: bool) -> None:
        if active:
            self.recorder_activated.emit(self._key_pill)

    def _emit_change(self) -> None:
        spec = format_hotkey(self._modifiers, self._key)
        self.hotkey_changed.emit(self.action_id, spec)

    def set_error(self, message: str | None) -> None:
        if message:
            self._status_label.setText(message)
            self._status_label.setStyleSheet("color: #F87171; background: transparent; border: none;")
            self._status_label.setVisible(True)
        else:
            self._status_label.setText("")
            self._status_label.setVisible(False)

    def update_spec(self, spec: str) -> None:
        self._modifiers, key = decompose_hotkey(spec)
        if not self._is_fixed_key:
            self._key = key
            self._key_pill.key = key
        self._ctrl_toggle.checked = "ctrl" in self._modifiers
        self._alt_toggle.checked = "alt" in self._modifiers
        self._shift_toggle.checked = "shift" in self._modifiers


class SettingsView(QWidget):
    """The Settings panel containing hotkey selectors and a 'Back to browser' button."""

    hotkey_changed = pyqtSignal(str, str)  # action_id, new_spec
    back_requested = pyqtSignal()

    def __init__(
        self,
        hotkeys: dict[str, str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._hotkey_cards: dict[str, HotkeyRowCard] = {}
        self._active_recorder: KeyPillButton | None = None
        self._build_ui(hotkeys)

    def _build_ui(self, hotkeys: dict[str, str]) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 8)
        main_layout.setSpacing(8)

        # Solid black background for the settings panel
        self.setObjectName("settingsPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "#settingsPanel { background-color: rgba(0, 0, 0, 230); border-radius: 8px; }"
        )

        # Scroll Area for Hotkey Rows
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_area.setStyleSheet(
            "QScrollArea { background: transparent; border: none; } "
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0px; } "
            "QScrollBar::handle:vertical { background: #353A47; min-height: 20px; border-radius: 3px; } "
            "QScrollBar::handle:vertical:hover { background: #5A6478; } "
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; } "
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
        )

        cards_container = QWidget()
        cards_container.setStyleSheet("background: transparent;")
        cards_layout = QVBoxLayout(cards_container)
        cards_layout.setContentsMargins(0, 0, 4, 0)
        cards_layout.setSpacing(12)

        # Hotkeys definition for Telemetry HUD
        options = [
            ("hide", "Toggle Visibility (Global)", hotkeys.get("hide", "ctrl+alt+c"), False),
            ("click_through", "Toggle Click-Through", hotkeys.get("click_through", "ctrl+alt+x"), False),
            ("screenshot", "Take Screenshot", hotkeys.get("screenshot", "ctrl+alt+s"), False),
            ("quit", "Quit Application", hotkeys.get("quit", "ctrl+alt+q"), False),
            ("move", "Move HUD (Arrow Keys)", hotkeys.get("move", "ctrl+alt"), True),
        ]

        for action_id, title, spec, is_fixed in options:
            card = HotkeyRowCard(action_id, title, spec, is_fixed_key=is_fixed, parent=cards_container)
            card.hotkey_changed.connect(self._on_card_changed)
            card.recorder_activated.connect(self._on_recorder_activated)
            cards_layout.addWidget(card)
            self._hotkey_cards[action_id] = card

        cards_layout.addStretch(1)
        scroll_area.setWidget(cards_container)
        main_layout.addWidget(scroll_area, 1)

        # Flat back button
        self._back_btn = QPushButton("Back to browser")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.setFixedHeight(30)
        self._back_btn.setFont(QFont("Segoe UI", 9, QFont.Weight.Medium))
        self._back_btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: #E2E8F0; "
            "border: none; border-radius: 6px; } "
            "QPushButton:hover { background-color: rgba(255, 255, 255, 0.08); } "
            "QPushButton:pressed { background-color: rgba(255, 255, 255, 0.14); }"
        )
        self._back_btn.clicked.connect(self._on_back_clicked)
        main_layout.addWidget(self._back_btn)

    @property
    def active_recorder(self) -> KeyPillButton | None:
        return self._active_recorder

    def _on_recorder_activated(self, pill: KeyPillButton) -> None:
        if self._active_recorder is not None and self._active_recorder != pill:
            self._active_recorder.cancel_recording()
        self._active_recorder = pill

    def cancel_recording(self) -> None:
        if self._active_recorder is not None:
            self._active_recorder.cancel_recording()
            self._active_recorder = None

    def _on_card_changed(self, action_id: str, new_spec: str) -> None:
        self.hotkey_changed.emit(action_id, new_spec)

    def _on_back_clicked(self) -> None:
        self.cancel_recording()
        self.back_requested.emit()

    def set_card_error(self, action_id: str, error: str | None) -> None:
        if action_id in self._hotkey_cards:
            self._hotkey_cards[action_id].set_error(error)

    def update_card_spec(self, action_id: str, spec: str) -> None:
        if action_id in self._hotkey_cards:
            self._hotkey_cards[action_id].update_spec(spec)


def load_hotkeys_config() -> dict[str, str]:
    """Load persisted hotkey configuration from disk if available."""
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def save_hotkeys_config(config: dict[str, str]) -> None:
    """Save hotkey configuration to disk."""
    try:
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass
