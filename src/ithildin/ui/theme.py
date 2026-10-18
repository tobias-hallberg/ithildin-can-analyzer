# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Tobias Hallberg
"""Theme tokens, ``ThemeManager`` singleton, and pyqtgraph palette plumbing.

Token palettes are hand-tuned hex strings with semantic names (``bg_0``,
``fg_1``, ``accent_dim`` …). Two variants ship in the box: ``dark``
(default) and ``light``. ``ThemeManager.apply(choice)`` writes a
``QPalette`` and a comprehensive QSS to the live ``QApplication`` and
emits ``theme_changed`` so theme-aware widgets (icons, banners) can
re-tint.

Theme choice persists via ``QSettings("Ithildin", "Ithildin")``. The
sentinel ``"system"`` resolves at apply time from
``QApplication.styleHints().colorScheme()``.

CAN-specific tokens (``rx``, ``tx``) extend the palette with bus traffic
colours used by the Trace view and the pyqtgraph curve cycle.
"""

from __future__ import annotations

from typing import Literal

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ThemeName = Literal["dark", "light", "system"]
ResolvedTheme = Literal["dark", "light"]


# --- Tokens ---------------------------------------------------------------

TOKENS: dict[ResolvedTheme, dict[str, str]] = {
    "dark": {
        # Surfaces, lowest → highest.
        "bg_0": "#15181c",
        "bg_1": "#1a1e23",
        "bg_2": "#20252b",
        "bg_3": "#272d34",
        "bg_hover": "#2a3139",
        "bg_selected": "#1e3a44",
        "bg_selected_strong": "#245662",
        "bg_titlebar": "#1d2228",
        "bg_statusbar": "#0f1216",
        # Borders, outer → inner.
        "border_0": "#0d1014",
        "border_1": "#2a3139",
        "border_2": "#363e47",
        # Foreground, primary → faint.
        "fg_0": "#e6e8ea",
        "fg_1": "#b4b8bd",
        "fg_2": "#7f858c",
        "fg_3": "#555b63",
        # Accent (cyan, per handoff §9).
        "accent": "#4fb3b8",
        "accent_dim": "#2d6e72",
        "accent_bg": "#1b3638",
        # State backgrounds and foregrounds.
        "warn_bg": "#4a380f",
        "info_bg": "#0f2d4a",
        "err_bg": "#3a1414",
        "ok": "#8bc48a",
        "warn": "#e0b86a",
        "err": "#e06c6c",
        "info": "#6aa9e0",
        # CAN-specific traffic colours (Trace).
        "rx": "#7cb6e0",
        "tx": "#e0b87c",
        # Plot curve palette — 6 hues spread across the wheel so two
        # signals are always distinguishable. Translated from the
        # Claude Design handoff OKLCH values
        # (``design_handoff_signal_plot/reference/tokens.css:39-44``):
        # cyan / yellow / red-orange / magenta / green / purple-blue.
        "sig_0": "#4dd0e1",
        "sig_1": "#d4b94e",
        "sig_2": "#e07b54",
        "sig_3": "#d186bf",
        "sig_4": "#70c98b",
        "sig_5": "#8e94d8",
    },
    "light": {
        "bg_0": "#e4e6e8",
        "bg_1": "#f3f4f6",
        "bg_2": "#fafbfc",
        "bg_3": "#eceef1",
        "bg_hover": "#e2e5e9",
        "bg_selected": "#d7ebee",
        "bg_selected_strong": "#bcdbe0",
        "bg_titlebar": "#e9ecef",
        "bg_statusbar": "#dadde1",
        "border_0": "#c2c7cd",
        "border_1": "#d8dce1",
        "border_2": "#ccd1d7",
        "fg_0": "#1c2024",
        "fg_1": "#4a5058",
        "fg_2": "#6c7279",
        "fg_3": "#9ba1a8",
        "accent": "#1e8085",
        "accent_dim": "#4fb3b8",
        "accent_bg": "#d7ebee",
        "warn_bg": "#fef3cd",
        "info_bg": "#cfe1f5",
        "err_bg": "#f7d7d7",
        "ok": "#3a8b3d",
        "warn": "#a6751a",
        "err": "#b23939",
        "info": "#2a6bb8",
        "rx": "#2a6bb8",
        "tx": "#a6751a",
        # Darker variants of the six signal hues (OKLCH L=0.55) so
        # curves stay visible on a near-white plot background.
        "sig_0": "#2391a0",
        "sig_1": "#8a7228",
        "sig_2": "#b94d2e",
        "sig_3": "#983d83",
        "sig_4": "#297b48",
        "sig_5": "#4f51a2",
    },
}


# --- Resolution -----------------------------------------------------------


def resolve(choice: ThemeName) -> ResolvedTheme:
    """Collapse ``"system"`` to a concrete ``"dark"`` or ``"light"`` palette."""
    if choice == "dark":
        return "dark"
    if choice == "light":
        return "light"
    app = QApplication.instance()
    scheme: Qt.ColorScheme | None = None
    if isinstance(app, QApplication):
        try:
            scheme = app.styleHints().colorScheme()
        except AttributeError:
            scheme = None
    if scheme == Qt.ColorScheme.Light:
        return "light"
    return "dark"


def tokens_for(choice: ThemeName) -> dict[str, str]:
    return TOKENS[resolve(choice)]


# --- Qt palette + stylesheet ---------------------------------------------


def _palette_for(tokens: dict[str, str]) -> QPalette:
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(tokens["bg_1"]))
    p.setColor(QPalette.ColorRole.WindowText, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.Base, QColor(tokens["bg_2"]))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(tokens["bg_1"]))
    p.setColor(QPalette.ColorRole.Text, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.Button, QColor(tokens["bg_3"]))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.BrightText, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(tokens["fg_2"]))
    p.setColor(QPalette.ColorRole.Highlight, QColor(tokens["bg_selected_strong"]))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(tokens["bg_3"]))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(tokens["fg_0"]))
    p.setColor(QPalette.ColorRole.Mid, QColor(tokens["fg_2"]))
    p.setColor(QPalette.ColorRole.Light, QColor(tokens["bg_3"]))
    p.setColor(QPalette.ColorRole.Midlight, QColor(tokens["bg_2"]))
    p.setColor(QPalette.ColorRole.Dark, QColor(tokens["bg_0"]))
    p.setColor(QPalette.ColorRole.Shadow, QColor(tokens["border_0"]))
    p.setColor(QPalette.ColorRole.Link, QColor(tokens["accent"]))
    p.setColor(QPalette.ColorRole.LinkVisited, QColor(tokens["accent_dim"]))
    try:
        p.setColor(QPalette.ColorRole.Accent, QColor(tokens["accent"]))
    except AttributeError:
        pass
    disabled = QPalette.ColorGroup.Disabled
    p.setColor(disabled, QPalette.ColorRole.Text, QColor(tokens["fg_3"]))
    p.setColor(disabled, QPalette.ColorRole.WindowText, QColor(tokens["fg_3"]))
    p.setColor(disabled, QPalette.ColorRole.ButtonText, QColor(tokens["fg_3"]))
    return p


_QSS_TEMPLATE = """
QMainWindow, QDialog {{ background: {bg_1}; color: {fg_0}; }}

QMenuBar {{
    background: {bg_1};
    color: {fg_0};
    border-bottom: 1px solid {border_1};
}}
QMenuBar::item:selected {{ background: {bg_hover}; }}
QMenuBar::item:disabled {{ color: {fg_3}; }}

QMenu {{
    background: {bg_3};
    color: {fg_0};
    border: 1px solid {border_2};
}}
QMenu::item {{ padding: 4px 24px 4px 20px; }}
QMenu::item:selected {{ background: {bg_hover}; }}
QMenu::item:disabled {{ color: {fg_3}; }}
QMenu::separator {{ height: 1px; background: {border_1}; margin: 4px 8px; }}

QToolBar {{
    background: {bg_1};
    border: 0;
    border-bottom: 1px solid {border_1};
    padding: 2px;
    spacing: 4px;
}}
QToolButton {{
    background: transparent;
    color: {fg_0};
    border: 1px solid transparent;
    border-radius: 3px;
    padding: 4px 8px;
}}
QToolButton:hover {{ background: {bg_hover}; border-color: {border_2}; }}
QToolButton:pressed {{ background: {bg_selected}; }}
QToolButton:checked {{
    background: {bg_selected_strong};
    color: {fg_0};
    border-color: {accent};
}}
QWidget#PaneToggleHeader {{
    background: {bg_titlebar};
    border-bottom: 1px solid {border_1};
}}
QToolButton#PaneToggleButton {{
    background: transparent;
    color: {fg_2};
    text-align: left;
    border: 0;
    border-radius: 0;
    padding: 4px 8px;
    font-size: 8pt;
    font-weight: 600;
}}
QToolButton#PaneToggleButton:hover {{
    background: {bg_3};
    color: {fg_1};
}}
QToolButton#PaneToggleButton:checked {{
    background: transparent;
    color: {fg_2};
}}
QToolButton#PaneToggleButton:checked:hover {{
    background: {bg_3};
    color: {fg_1};
}}
QToolButton#PaneHeaderAction {{
    background: transparent;
    border: 0;
    border-radius: 0;
    padding: 2px 10px;
    color: {fg_2};
}}
QToolButton#PaneHeaderAction:hover {{
    background: {bg_3};
    color: {fg_1};
}}
QToolButton:disabled {{ color: {fg_3}; }}

/* Shared MDI view header strip (ui/view_header.py). Owns the bar chrome
   so Trace / Plot / Record strips read identically: window-tinted
   background with a 1 px bottom divider. WA_StyledBackground is set on
   the widget so this rule paints. */
QWidget#ViewHeader {{
    background: {bg_1};
    border-bottom: 1px solid {border_2};
}}

/* Segmented control — the [Stream | Latest] mode selector in the Trace
   window's toolbar strip. The frame owns the outer border and rounded
   corners; child buttons strip their own border so the pair reads as a
   single unit. A 1 px divider sits on the right button's left edge. */
QFrame#ModeSegment {{
    border: 1px solid {border_2};
    border-radius: 3px;
    background: {bg_2};
}}
/* All four states explicitly reset border/margin so the global
   QToolButton :hover / :checked rules can't sneak a 1 px outline back
   in. */
QFrame#ModeSegment QToolButton,
QFrame#ModeSegment QToolButton:hover,
QFrame#ModeSegment QToolButton:checked,
QFrame#ModeSegment QToolButton:checked:hover {{
    border: 0;
    border-radius: 0;
    margin: 0;
    padding: 2px 10px;
}}
QFrame#ModeSegment QToolButton {{
    background: transparent;
    color: {fg_1};
}}
QFrame#ModeSegment QToolButton:hover {{
    background: {bg_hover};
    color: {fg_0};
}}
QFrame#ModeSegment QToolButton:checked {{
    background: {bg_selected_strong};
    color: {fg_0};
}}
QFrame#ModeSegment QToolButton:checked:hover {{
    background: {bg_selected_strong};
}}
/* The right-hand button in any #ModeSegment gets a 1 px divider on its
   left edge so the seam between the two buttons stays visible. */
QFrame#ModeSegment QToolButton#SegmentRightBtn {{
    border-left: 1px solid {border_2};
}}

QStatusBar {{
    background: {bg_statusbar};
    color: {fg_2};
    border-top: 1px solid {border_1};
    min-height: 22px;
    font-size: 8pt;
}}
QStatusBar::item {{ border: 0; }}

QSplitter::handle {{ background: {bg_0}; }}
QSplitter::handle:horizontal {{ width: 4px; }}
QSplitter::handle:vertical {{ height: 4px; }}

QDockWidget {{
    titlebar-close-icon: none;
    color: {fg_2};
    border: 1px solid {border_1};
}}
QDockWidget::title {{
    background: {bg_titlebar};
    color: {fg_1};
    padding: 4px 10px;
    border-bottom: 1px solid {border_1};
}}
QWidget#LeftDockBody {{
    background: {bg_1};
    border: 0;
    border-right: 1px solid {border_2};
}}

QTreeView, QTableView, QListView {{
    background: {bg_2};
    alternate-background-color: {bg_1};
    color: {fg_0};
    gridline-color: {border_1};
    selection-background-color: {bg_selected};
    selection-color: {fg_0};
    border: 1px solid {border_1};
}}
QTableView::item {{ padding: 4px 8px; }}
QTreeView::item {{ padding: 2px 4px; }}
QListView::item {{ padding: 4px 6px; }}
QHeaderView::section {{
    background: {bg_3};
    color: {fg_1};
    border: 0;
    border-right: 1px solid {border_1};
    border-bottom: 1px solid {border_1};
    padding: 6px 10px;
    font-size: 8pt;
    font-weight: 600;
    letter-spacing: 0.3px;
}}
QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
    background: {bg_selected_strong};
    color: {fg_0};
}}
QTableView::item:hover, QTreeView::item:hover, QListView::item:hover {{
    background: {bg_hover};
}}
QLabel#DbcEmptyHint {{
    background: {bg_2};
    border: 1px solid {border_1};
    color: {fg_3};
    font-style: italic;
    padding: 8px;
}}

QLabel#PaneHeader {{
    background: {bg_1};
    color: {fg_1};
    border-bottom: 1px solid {border_1};
    padding: 6px 10px;
    font-size: 8pt;
    font-weight: 500;
    letter-spacing: 1px;
}}
QWidget#PaneHeaderBar {{
    background: {bg_1};
    border-bottom: 1px solid {border_1};
}}
QLabel#PaneHeaderTitle {{
    background: transparent;
    color: {fg_1};
    padding: 6px 0 6px 10px;
    font-size: 8pt;
    font-weight: 500;
    letter-spacing: 1px;
}}

QTabWidget::pane {{
    background: {bg_1};
    border: 1px solid {border_1};
    top: -1px;
}}
QTabBar {{
    background: {bg_1};
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background: {bg_2};
    color: {fg_2};
    border: 1px solid {border_1};
    border-bottom: none;
    padding: 6px 14px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{ background: {bg_1}; color: {fg_0}; }}
QTabBar::tab:hover:!selected {{ background: {bg_hover}; color: {fg_0}; }}

QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background: {bg_2};
    color: {fg_0};
    border: 1px solid {border_2};
    border-radius: 3px;
    padding: 3px 6px;
    selection-background-color: {bg_selected_strong};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {accent};
}}
QLineEdit:read-only, QTextEdit:read-only, QPlainTextEdit:read-only {{
    background: {bg_1};
    color: {fg_1};
}}
QComboBox::drop-down {{
    border: 0;
    width: 18px;
}}

/* Styling the spin box body switches Qt to stylesheet-drawn sub-controls,
   so the steppers need explicit geometry or they render as unhittable
   slivers. Give them real width and a hover tint. */
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background: {bg_3};
    border: 0;
    border-left: 1px solid {border_2};
    width: 18px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    border-top-right-radius: 3px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    border-top: 1px solid {border_2};
    border-bottom-right-radius: 3px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {bg_hover};
}}
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {{
    background: {bg_selected_strong};
}}
/* A stylesheet-styled sub-control gets no native arrow drawn, so the
   steppers render as blank boxes without an explicit image. */
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{arrow_up_url}");
    width: 10px;
    height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{arrow_down_url}");
    width: 10px;
    height: 10px;
}}
/* ``:off`` = that step is unavailable (value sits at a range limit). Qt
   won't dim a stylesheet-supplied arrow, so swap in a fg_3 render rather
   than dropping the glyph and leaving an empty box. */
QSpinBox::up-arrow:off, QDoubleSpinBox::up-arrow:off {{
    image: url("{arrow_up_dim_url}");
}}
QSpinBox::down-arrow:off, QDoubleSpinBox::down-arrow:off {{
    image: url("{arrow_down_dim_url}");
}}

QPushButton {{
    background: {bg_3};
    color: {fg_0};
    border: 1px solid {border_2};
    border-radius: 3px;
    padding: 4px 12px;
}}
QPushButton:hover {{ background: {bg_hover}; }}
QPushButton:pressed {{ background: {bg_selected_strong}; }}
QPushButton:disabled {{ color: {fg_3}; background: {bg_2}; }}
QPushButton:default {{ border: 1px solid {accent}; }}

QCheckBox, QRadioButton {{ color: {fg_0}; spacing: 6px; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {fg_3}; }}

QToolTip {{
    background: {bg_3};
    color: {fg_0};
    border: 1px solid {border_2};
    padding: 3px 6px;
}}

QScrollBar:vertical {{
    background: {bg_1}; width: 12px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {border_2}; min-height: 24px; border-radius: 3px; margin: 2px;
}}
QScrollBar:horizontal {{
    background: {bg_1}; height: 12px; margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {border_2}; min-width: 24px; border-radius: 3px; margin: 2px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ background: transparent; }}

QMdiArea {{
    background: {bg_0};
    border: 0;
}}

QGroupBox {{
    color: {fg_1};
    border: 1px solid {border_1};
    border-radius: 3px;
    margin-top: 10px;
    padding-top: 6px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: {fg_2};
}}

QProgressBar {{
    background: {bg_2};
    border: 1px solid {border_1};
    border-radius: 2px;
    text-align: center;
    color: {fg_1};
}}
QProgressBar::chunk {{ background: {accent_dim}; }}

/* Banner widgets — tinted via objectName + WA_StyledBackground. */
QWidget#WarnBanner {{
    background: {warn_bg};
    border: 1px solid {warn};
    border-radius: 3px;
}}
QWidget#InfoBanner {{
    background: {info_bg};
    border: 1px solid {info};
    border-radius: 3px;
}}
QWidget#ErrorBanner {{
    background: {err_bg};
    border: 1px solid {err};
    border-radius: 3px;
}}
QLabel#BannerLabel {{
    background: transparent;
    border: none;
    color: {fg_0};
}}

/* Transmit window — status bar (master switch is a custom painted widget). */
QLabel#TxStatusBarOn {{
    background: {accent};
    color: {bg_0};
    padding: 0 12px;
    font-weight: 600;
    letter-spacing: 0.4px;
}}
QLabel#TxStatusBarOff {{
    background: {bg_titlebar};
    color: {fg_2};
    padding: 0 12px;
    letter-spacing: 0.4px;
}}

/* Transmit toolbar. */
QFrame#TxToolbar {{
    background: {bg_1};
    border-bottom: 1px solid {border_1};
}}
QFrame#TxToolbarSep {{
    color: {border_1};
}}
QLabel#TxCountNumber {{
    color: {fg_0};
    font-size: 13pt;
}}
QLabel#TxCountCaption {{
    color: {fg_3};
    font-size: 7pt;
    letter-spacing: 1.2px;
}}
QToolButton#TxBulkBtn {{
    background: {bg_2};
    color: {fg_1};
    border: 1px solid {border_1};
    border-radius: 2px;
    padding: 3px 10px;
    letter-spacing: 0.5px;
}}
QToolButton#TxBulkBtn:hover {{
    background: {bg_3};
}}
QToolButton#TxBulkBtn:disabled {{
    color: {fg_3};
    background: {bg_1};
}}

/* Transmit list pane. */
QFrame#TxListPane {{
    background: {bg_1};
    border-right: 1px solid {border_1};
}}
QFrame#TxListHeader {{
    background: {bg_titlebar};
    border-bottom: 1px solid {border_0};
}}
QLabel#TxListTitle {{
    color: {fg_2};
    letter-spacing: 0.8px;
}}
QLabel#TxListCount {{
    color: {fg_3};
}}
QListView#TxListView {{
    background: {bg_1};
    border: 0;
    outline: 0;
}}

/* Transmit editor pane. */
QFrame#TxEditorPane {{
    background: {bg_0};
}}
QFrame#TxEditorHeader {{
    background: {bg_1};
    border-bottom: 1px solid {border_0};
}}
QLabel#TxEditorTitle {{
    color: {fg_0};
    font-size: 13pt;
    font-weight: 600;
}}
QFrame#TxSignalRow {{
    background: {bg_0};
    border-bottom: 1px solid {border_0};
}}
QLabel#TxSignalName {{
    color: {fg_0};
}}
QLabel#TxKindBadge {{
    color: {fg_3};
    background: transparent;
    border: 1px solid {border_0};
    border-radius: 2px;
    padding: 1px 5px;
    font-size: 8pt;
}}
QLabel#TxEmptyHint {{
    color: {fg_3};
    padding: 40px 0;
    font-style: italic;
}}
"""


def stylesheet_for(tokens: dict[str, str]) -> str:
    # Deferred import: icons.py imports ThemeManager from this module, so a
    # top-level import here would be circular. By the time a stylesheet is
    # built the module graph is settled.
    from ithildin.ui.icons import stylesheet_image_path

    return _QSS_TEMPLATE.format(
        **tokens,
        arrow_up_url=stylesheet_image_path("up", tokens["fg_1"]),
        arrow_down_url=stylesheet_image_path("down", tokens["fg_1"]),
        arrow_up_dim_url=stylesheet_image_path("up", tokens["fg_3"]),
        arrow_down_dim_url=stylesheet_image_path("down", tokens["fg_3"]),
    )


# --- ThemeManager singleton ----------------------------------------------


class ThemeManager(QObject):
    """Owns the current theme choice + applies it to the live QApplication.

    Consumers that paint with hardcoded colours (icons, banners) connect
    to ``theme_changed`` and re-style themselves.
    """

    theme_changed = Signal(dict)  # emitted with the resolved token dict

    _instance: ThemeManager | None = None

    SETTINGS_ORG = "Ithildin"
    SETTINGS_APP = "Ithildin"
    SETTINGS_KEY = "ui/theme"

    def __init__(self) -> None:
        super().__init__()
        self._choice: ThemeName = "system"
        self._system_listener_installed = False

    @classmethod
    def instance(cls) -> ThemeManager:
        if cls._instance is None:
            cls._instance = ThemeManager()
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._instance = None

    # --- public API ---------------------------------------------------

    def load_choice(self) -> ThemeName:
        """Read the persisted choice; default to ``"system"`` on first run."""
        settings = QSettings(self.SETTINGS_ORG, self.SETTINGS_APP)
        choice = settings.value(self.SETTINGS_KEY, "system")
        if choice not in ("dark", "light", "system"):
            choice = "system"
        return choice  # type: ignore[return-value]

    def current_choice(self) -> ThemeName:
        return self._choice

    def current_tokens(self) -> dict[str, str]:
        return tokens_for(self._choice)

    def apply(self, choice: ThemeName, *, persist: bool = True) -> None:
        """Apply ``choice`` to the live QApplication and (optionally) persist.

        Silent no-op when no QApplication exists (tests that don't
        construct one won't crash).
        """
        if choice not in ("dark", "light", "system"):
            choice = "system"
        self._choice = choice
        if persist:
            QSettings(self.SETTINGS_ORG, self.SETTINGS_APP).setValue(self.SETTINGS_KEY, choice)
        tokens = tokens_for(choice)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setPalette(_palette_for(tokens))
            app.setStyleSheet(stylesheet_for(tokens))
            apply_pyqtgraph_theme(tokens)
            self._install_system_listener(app)
        self.theme_changed.emit(tokens)

    def _install_system_listener(self, app: QApplication) -> None:
        """Subscribe once to ``colorSchemeChanged`` so ``"system"`` follows the OS live."""
        if self._system_listener_installed:
            return
        try:
            hints = app.styleHints()
            hints.colorSchemeChanged.connect(self._on_system_color_scheme_changed)
        except AttributeError:
            return
        self._system_listener_installed = True

    def _on_system_color_scheme_changed(self, _scheme: object) -> None:
        if self._choice == "system":
            self.apply("system", persist=False)


# --- pyqtgraph ------------------------------------------------------------


def pyqtgraph_palette(tokens: dict[str, str]) -> dict[str, object]:
    """Imperative settings to apply to pyqtgraph before instantiating PlotWidget.

    Curves cycle through ``curves``. ``axisColor`` and ``grid`` are
    applied per-PlotItem after construction. The plot canvas uses the
    *lightest* surface in a light theme and the *deepest* in a dark
    theme — the convention for analytics tools (white plot area on a
    pale chrome; near-black plot area on a dark chrome).
    """
    is_light = QColor(tokens["bg_0"]).lightness() > 128
    background = tokens["bg_2"] if is_light else tokens["bg_0"]
    return {
        "background": background,
        "foreground": tokens["fg_1"],
        "axisColor": tokens["fg_3"],
        "grid": tokens["border_1"],
        # Six hues spread across the wheel (see ``TOKENS`` /
        # ``sig_0..sig_5``). Old palette mixed in semantic colours
        # (``rx`` / ``tx`` / ``warn``) that ended up too close in hue
        # to tell apart on a plot.
        "curves": [
            tokens["sig_0"],
            tokens["sig_1"],
            tokens["sig_2"],
            tokens["sig_3"],
            tokens["sig_4"],
            tokens["sig_5"],
        ],
    }


def apply_pyqtgraph_theme(tokens: dict[str, str]) -> None:
    """Configure pyqtgraph globals from a token dict.

    Must run **before** the first ``PlotWidget`` is constructed —
    pyqtgraph caches the background colour at widget-creation time.
    Idempotent.
    """
    import pyqtgraph as pg

    pal = pyqtgraph_palette(tokens)
    pg.setConfigOption("background", pal["background"])
    pg.setConfigOption("foreground", pal["foreground"])
    pg.setConfigOption("antialias", True)
