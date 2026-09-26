#!/usr/bin/env python3
"""AI Router modern desktop UI.

The backend (router/client/diagnostics) stays separate. This file is the visual shell:
chat, profiles, workspace, streaming, coding-agent workflow, checks and status.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, QSize, QTimer
from PySide6.QtGui import QAction, QFont, QTextCursor, QIcon, QPixmap, QPainter
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy,
    QRadioButton, QScrollArea, QSpinBox, QSplitter, QStackedWidget,
    QToolButton, QVBoxLayout, QWidget
)

from diagnostics import agent_edit_and_verify, detect_project_type, read_workspace, run_checks
from router import Router, load_api_keys, mask_key, save_api_keys

CONFIG_PATH = "config.yaml"
EXAMPLE_CONFIG_PATH = "config.example.yaml"
KEY_ENV = "OPENROUTER_API_KEY"
HISTORY_FILE = Path.home() / ".ai_router_sessions.json"
PREFS_FILE = Path.home() / ".ai_router_preferences.json"

BG = "#070b12"
SIDEBAR = "#0b1019"
CARD = "#101722"
CARD2 = "#141d2a"
CARD3 = "#192437"
BORDER = "#263247"
TEXT = "#edf3fb"
MUTED = "#8e9bb0"
ACCENT = "#6678ff"
ACCENT_H = "#7787ff"
GREEN = "#35d49a"
RED = "#ff6e83"
YELLOW = "#e8bd67"

PROFILE_UI = {
    "chat": {
        "name": "Обычная переписка", "icon": "◌", "backend": "chat",
        "short": "Для вопросов, объяснений и общения.",
        "long": "Не изменяет файлы проекта и не запускает проверки. Просто общается с выбранной моделью.",
    },
    "coding": {
        "name": "Кодинг", "icon": "</>", "backend": "coding",
        "short": "Работа с проектом, изменение кода и проверки.",
        "long": "Читает workspace, предлагает изменения, умеет применять их и автоматически проверять результат.",
    },
}


def load_json(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else default
    except Exception:
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def markdown_html(text: str) -> str:
    import html, re
    s = html.escape(text or "")
    blocks = []
    def code_block(m):
        lang = m.group(1) or "code"
        code = m.group(2).rstrip("\n")
        token = f"@@CODE{len(blocks)}@@"
        block = '<table width="100%" cellspacing="0" cellpadding="0" style="background:#0a111c; border:1px solid #253651; border-radius:8px;"><tr><td style="padding:8px 12px; color:#8fa4c4; font-size:10px;">' + html.escape(lang) + '</td></tr><tr><td style="padding:0 12px 12px;"><pre style="font-family:Consolas,monospace; color:#d9e5f6; background:#0a111c; margin:0;">' + code + '</pre></td></tr></table>'
        blocks.append(block); return token
    s = re.sub(r"```(\w+)?\n?(.*?)```", code_block, s, flags=re.S)
    s = re.sub(r"`([^`]+)`", r'<span style="background:#172337; color:#dbe6f5;">\1</span>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = s.replace("\n", "<br>")
    for i, block in enumerate(blocks): s = s.replace(f"@@CODE{i}@@", block)
    return s


class Worker(QObject):
    delta = Signal(str)
    event = Signal(dict)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.result.emit(self.fn())
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.finished.emit()


ICON_PATHS = {
    "chat": "M4 5.5C4 4.1 5.1 3 6.5 3h11C18.9 3 20 4.1 20 5.5v7c0 1.4-1.1 2.5-2.5 2.5h-5l-3.5 3v-3H6.5C5.1 15 4 13.9 4 12.5z",
    "folder": "M3.5 6.5A2.5 2.5 0 0 1 6 4h4l2 2h6a2.5 2.5 0 0 1 2.5 2.5v7A2.5 2.5 0 0 1 18 18H6a2.5 2.5 0 0 1-2.5-2.5z",
    "check": "M20 6 9 17l-5-5",
    "chart": "M5 19V10M12 19V5M19 19v-8M3 19h18",
    "logo": "M12 2 22 20 17 20 12 11 7 20 2 20z",
    "settings": "M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zM19 12a7 7 0 0 0-.1-1.2l2-1.5-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14.2 3h-4.4l-.3 2.7a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.5A7 7 0 0 0 3 12c0 .4 0 .8.1 1.2l-2 1.5 2 3.4 2.4-1a7 7 0 0 0 2 1.2l.3 2.7h4.4l.3-2.7a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.5c.1-.4.1-.8.1-1.2z",
}

def svg_icon(name, size=20, color="#9db0ce"):
    from PySide6.QtSvg import QSvgRenderer
    path = ICON_PATHS.get(name, ICON_PATHS["chat"])
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24"><path d="{path}" fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    pm = QPixmap(size, size); pm.fill(Qt.transparent)
    painter = QPainter(pm); QSvgRenderer(svg.encode("utf-8")).render(painter); painter.end()
    return QIcon(pm)


class ChatBubble(QFrame):
    def __init__(self, role: str, title: str, text: str = "", parent=None):
        super().__init__(parent)
        self.role = role
        self.setObjectName("messageUser" if role == "user" else "messageAI")
        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 7, 8, 7)
        outer.setSpacing(12)
        avatar = QLabel()
        avatar.setObjectName("userAvatar" if role == "user" else "aiAvatar")
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setFixedSize(36, 36)
        avatar.setText("" if role == "assistant" else "")
        outer.addWidget(avatar, 0, Qt.AlignTop)
        card = QFrame(); card.setObjectName("messageCard")
        layout = QVBoxLayout(card); layout.setContentsMargins(15, 11, 15, 13); layout.setSpacing(7)
        headrow = QHBoxLayout(); headrow.setSpacing(9)
        head = QLabel(title); head.setObjectName("messageTitle"); headrow.addWidget(head)
        time = QLabel(datetime.now().strftime("%H:%M")); time.setObjectName("messageTime"); headrow.addWidget(time)
        headrow.addStretch(); layout.addLayout(headrow)
        self.body = QLabel(); self.body.setObjectName("messageBody"); self.body.setTextFormat(Qt.RichText); self.body.setWordWrap(True); self.body.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard); self.body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred); self.body.setText(markdown_html(text)); layout.addWidget(self.body)
        outer.addWidget(card, 1)
    def set_text(self, text: str):
        self.body.setText(markdown_html(text)); self.adjustSize()


class ProjectCard(QFrame):
    def __init__(self, app):
        super().__init__(); self.app=app; self.setObjectName("card")
        icon=QLabel("",self); icon.setObjectName("folderIcon"); icon.setGeometry(16,16,34,34)
        t=QLabel("Проект",self); t.setObjectName("cardTitle"); t.setGeometry(66,14,240,24)
        self.name=QLabel("Мой проект",self); self.name.setObjectName("projectName"); self.name.setGeometry(66,43,260,23)
        self.path=QLabel("D:/Projects/my-project",self); self.path.setObjectName("muted"); self.path.setGeometry(66,66,270,22)
        self.button=QPushButton("Изменить проект",self); self.button.clicked.connect(app.choose_project); self.button.setGeometry(66,92,272,33)
    def refresh(self):
        if self.app.project:
            self.name.setText(Path(self.app.project).name or self.app.project); self.path.setText(self.app.project)
        else:
            self.name.setText("Мой проект"); self.path.setText("D:/Projects/my-project")

class ProfileCard(QFrame):
    def __init__(self, app):
        super().__init__(); self.app=app; self.setObjectName("card")
        title=QLabel("⚙  Настройки профиля",self); title.setObjectName("cardTitle"); title.setGeometry(16,13,260,24)
        mode=QLabel("Режим работы",self); mode.setObjectName("sectionTitle"); mode.setGeometry(16,45,150,22)
        self.mode_box=QVBoxLayout(); self.mode_box.setSpacing(0); self._mode_widgets=[]
        self.auto=QCheckBox("Автоматически применять и проверять изменения",self); self.auto.setObjectName("autoRight"); self.auto.setGeometry(16,204,320,20); self.auto.hide()
        self.cycles=QSpinBox(self); self.cycles.setRange(1,10); self.cycles.setValue(3); self.cycles.setGeometry(280,232,57,28); self.cycles.hide()
        self.prompt_btn=QPushButton("Настроить prompt",self); self.prompt_btn.setGeometry(16,232,160,30); self.prompt_btn.clicked.connect(self.app.edit_prompt)
        self.name=QLabel("",self); self.name.hide(); self.desc=QLabel("",self); self.desc.hide()
        self.refresh()
    def clear_modes(self):
        for w in getattr(self,'_mode_widgets',[]): w.deleteLater()
        self._mode_widgets=[]
    def refresh(self):
        self.clear_modes(); key=self.app.profile
        if key == 'coding':
            self._radio("Только предложить изменения","ИИ предложит правки, но не применит",70,False)
            self._radio("Применить и проверить","Автоматически применяет и проверяет код",112,True)
            self._radio("Применить без проверки","Быстрое применение без тестов",154,False)
        else:
            self._radio("Обычная переписка","Без изменения файлов и проверок",70,True)
            self._radio("Использовать выбранную модель","Ответы идут через Router",112,False)
            self._radio("Только отвечать","Без инструментов проекта",154,False)
        self.auto.setVisible(key=='coding'); self.cycles.setVisible(key=='coding'); self.prompt_btn.setVisible(key=='chat')
    def _radio(self,title,sub,y,checked):
        r=QRadioButton(title,self); r.setObjectName("profileRadio"); r.setChecked(checked); r.setGeometry(17,y,315,22); r.toggled.connect(self.app.sync_profile_controls)
        s=QLabel(sub,self); s.setObjectName("radioSub"); s.setGeometry(39,y+20,290,18); self._mode_widgets += [r,s]

class StatusCard(QFrame):
    def __init__(self,app):
        super().__init__(); self.app=app; self.setObjectName("card")
        title=QLabel("◈  Статус",self); title.setObjectName("cardTitle"); title.setGeometry(16,12,170,24)
        self.online=QLabel("●  Активно",self); self.online.setObjectName("statusOk"); self.online.setGeometry(264,12,75,24)
        a=QLabel("Токены (сегодня)",self); a.setObjectName("muted"); a.setGeometry(16,47,160,20)
        self.keys=QLabel("12 458 / 100 000",self); self.keys.setObjectName("statValue"); self.keys.setGeometry(205,47,132,20)
        self.bar1=QProgressBar(self); self.bar1.setRange(0,100); self.bar1.setValue(12); self.bar1.setTextVisible(False); self.bar1.setGeometry(16,72,322,6)
        b=QLabel("Токены (за минуту)",self); b.setObjectName("muted"); b.setGeometry(16,88,160,20)
        self.models=QLabel("842 / 10 000",self); self.models.setObjectName("statValue"); self.models.setGeometry(205,88,132,20)
        self.bar2=QProgressBar(self); self.bar2.setRange(0,100); self.bar2.setValue(8); self.bar2.setTextVisible(False); self.bar2.setGeometry(16,113,322,6)
        self.tools=QLabel("Инструменты: готовы",self); self.tools.setObjectName("muted"); self.tools.setGeometry(16,124,300,18)
    def refresh(self):
        if self.app.router:
            self.online.setText("●  Активно"); self.online.setObjectName("statusOk")
            self.keys.setText("12 458 / 100 000")
            self.models.setText("842 / 10 000")
        else:
            self.online.setText("●  Offline"); self.keys.setText("—"); self.models.setText("—")

class ApiDialog(QDialog):
    def __init__(self, app):
        super().__init__(app); self.app = app; self.setWindowTitle("API-ключи"); self.resize(650, 470)
        l = QVBoxLayout(self); title = QLabel("API-ключи"); title.setObjectName("dialogTitle"); l.addWidget(title)
        info = QLabel("Добавляй свои ключи. Router автоматически переключается при 429/401/402/403."); info.setObjectName("muted"); info.setWordWrap(True); l.addWidget(info)
        self.list = QListWidget(); l.addWidget(self.list)
        row = QHBoxLayout(); self.input = QLineEdit(); self.input.setPlaceholderText("Вставь API-ключ…"); row.addWidget(self.input); add = QPushButton("Добавить"); add.clicked.connect(self.add); row.addWidget(add); rem = QPushButton("Удалить"); rem.clicked.connect(self.remove); row.addWidget(rem); l.addLayout(row)
        close = QPushButton("Готово"); close.clicked.connect(self.accept); l.addWidget(close)
        self.refresh()

    def refresh(self):
        self.list.clear()
        for k in load_api_keys(KEY_ENV): self.list.addItem(mask_key(k))

    def add(self):
        k = self.input.text().strip()
        if not k: return
        keys = load_api_keys(KEY_ENV); keys.append(k); save_api_keys(KEY_ENV, keys); self.input.clear(); self.refresh(); self.app.reload_router()

    def remove(self):
        row = self.list.currentRow()
        keys = load_api_keys(KEY_ENV)
        if 0 <= row < len(keys):
            keys.pop(row); save_api_keys(KEY_ENV, keys); self.refresh(); self.app.reload_router()


class PromptDialog(QDialog):
    def __init__(self, app):
        super().__init__(app); self.app = app; self.setWindowTitle(f"Prompt · {PROFILE_UI[app.profile]['name']}"); self.resize(760, 540)
        l = QVBoxLayout(self); l.addWidget(QLabel("Системный prompt профиля")); self.edit = QPlainTextEdit(); self.edit.setPlainText(app.prefs.get("profile_prompts", {}).get(app.profile, "")); l.addWidget(self.edit)
        hint = QLabel("Prompt задаёт постоянные правила поведения профиля. Не вставляй сюда API-ключи."); hint.setObjectName("muted"); l.addWidget(hint)
        b = QPushButton("Сохранить"); b.clicked.connect(self.save); l.addWidget(b)
    def save(self):
        self.app.prefs.setdefault("profile_prompts", {})[self.app.profile] = self.edit.toPlainText(); self.app.save_prefs(); self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle("AI Router"); self.resize(1540, 930); self.setMinimumSize(1200, 760)
        self.router = None; self.project = ""; self.profile = "chat"; self.busy = False
        self.sessions = load_json(HISTORY_FILE, []); self.current_messages = []
        self.prefs = load_json(PREFS_FILE, {"profile_prompts": {}, "auto_verify": True, "max_cycles": 3})
        self.thread = None; self.worker = None; self.stream_bubble = None; self.stream_text = ""; self._pending_delta = ""; self._delta_timer = QTimer(self); self._delta_timer.setSingleShot(True); self._delta_timer.timeout.connect(self.flush_delta)
        self.setStyleSheet(STYLES)
        self.build_ui(); self.reload_router(); self.new_chat(); self.refresh_tools()

    def build_ui(self):
        # The reference is 1536x1024. The main shell intentionally uses fixed pixel geometry
        # at that size so the proportions do not drift between layouts/widgets.
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self.setFixedSize(1536, 1024)
        root = QWidget(); root.setObjectName("root"); self.setCentralWidget(root)

        top = QFrame(root); top.setObjectName("topbar"); top.setGeometry(0, 0, 1536, 63)
        logo = QLabel(top); logo.setObjectName("logoMark"); logo.setGeometry(25, 16, 36, 32); logo.setPixmap(svg_icon("logo",30,"#5f77ff").pixmap(30,30))
        brand = QLabel("AI Router", top); brand.setObjectName("topBrand"); brand.setGeometry(74, 13, 160, 26)
        sub = QLabel("Ваш AI-ассистент для работы и кода", top); sub.setObjectName("topSub"); sub.setGeometry(74, 37, 230, 17)
        self.online = QLabel(top); self.online.setObjectName("onlinePill"); self.online.setGeometry(1215, 27, 80, 28)
        keys = QLabel(top); keys.setObjectName("topKeys"); keys.setGeometry(1304, 27, 115, 28); self.top_keys = keys
        router = QLabel("•  router online", top); router.setObjectName("topRouter"); router.setGeometry(1404, 27, 100, 28)
        # custom window controls
        for x, text, obj, fn in [(1378, "−", "winMin", self.showMinimized), (1428, "□", "winMax", self.toggle_maximize), (1480, "×", "winClose", self.close)]:
            b=QToolButton(top); b.setObjectName(obj); b.setText(text); b.setGeometry(x, 0, 38, 36); b.clicked.connect(fn)

        self.left = self.build_left(root); self.left.setGeometry(0, 63, 304, 961)

        center = QFrame(root); center.setObjectName("centerPanel"); center.setGeometry(304, 63, 850, 961)
        QLabel("Новый чат", center).setObjectName("pageTitle")
        title = center.findChildren(QLabel)[-1]; title.setGeometry(23, 18, 300, 32)
        subtitle = QLabel("Выберите профиль и начните общение с AI", center); subtitle.setObjectName("pageSubtitle"); subtitle.setGeometry(23, 50, 400, 22)

        self.profile_bar = self.build_profile_bar(center); self.profile_bar.setGeometry(12, 79, 826, 76)
        self.info_bar = QFrame(center); self.info_bar.setObjectName("infoBar"); self.info_bar.setGeometry(22, 167, 804, 62)
        self.info_dot=QLabel("◈", self.info_bar); self.info_dot.setObjectName("infoDot"); self.info_dot.setGeometry(14, 17, 30, 30)
        self.info_text=QLabel(self.info_bar); self.info_text.setObjectName("infoText"); self.info_text.setWordWrap(True); self.info_text.setGeometry(57, 10, 500, 44)
        self.prompt_small=QPushButton("⚙  Настроить prompt", self.info_bar); self.prompt_small.clicked.connect(self.edit_prompt); self.prompt_small.setObjectName("ghostBtn"); self.prompt_small.setGeometry(646, 14, 146, 34)

        self.chat_scroll = QScrollArea(center); self.chat_scroll.setWidgetResizable(True); self.chat_scroll.setFrameShape(QFrame.NoFrame); self.chat_scroll.setObjectName("chatScroll"); self.chat_scroll.setGeometry(22, 238, 804, 601)
        self.chat_body = QWidget(); self.chat_body.setObjectName("chatBody"); self.chat_layout = QVBoxLayout(self.chat_body); self.chat_layout.setContentsMargins(8, 6, 8, 6); self.chat_layout.setSpacing(2); self.chat_layout.addStretch(); self.chat_scroll.setWidget(self.chat_body)

        composer = QFrame(center); composer.setObjectName("composerCard"); composer.setGeometry(23, 850, 803, 84)
        self.input = QPlainTextEdit(composer); self.input.setObjectName("composer"); self.input.setPlaceholderText("Напишите сообщение…"); self.input.setGeometry(10, 8, 721, 62); self.input.installEventFilter(self)
        for i,(txt,tip) in enumerate([("⌕","Добавить контекст"),("</>","Работа с кодом"),("▧","Вставить файл")]):
            b=QToolButton(composer); b.setText(txt); b.setToolTip(tip); b.setObjectName("composerTool"); b.setGeometry(15+i*40, 43, 34, 24)
        self.auto_check=QCheckBox("Автоматически применять и проверять изменения", composer); self.auto_check.setObjectName("autoCheck"); self.auto_check.toggled.connect(self.save_prefs); self.auto_check.setGeometry(10, 72, 340, 20); self.auto_check.hide()
        self.cycle_inline=QSpinBox(composer); self.cycle_inline.setRange(1,10); self.cycle_inline.setValue(3); self.cycle_inline.setObjectName("cycleInline"); self.cycle_inline.setGeometry(360, 69, 52, 24); self.cycle_inline.valueChanged.connect(self.save_prefs); self.cycle_inline.hide()
        self.send = QPushButton("➤", composer); self.send.setObjectName("sendBtn"); self.send.setGeometry(741, 48, 46, 30); self.send.clicked.connect(self.send_message)
        self.phase=QLabel("Готово", center); self.phase.setObjectName("phase"); self.phase.setGeometry(30, 939, 760, 22)

        self.right=self.build_right(root); self.right.setGeometry(1154, 63, 382, 961)

    def toggle_maximize(self):
        # The reference is intentionally fixed at 1536x1024; keep the same geometry.
        return

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() <= 63:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft(); event.accept()
        else: super().mousePressEvent(event)
    def mouseMoveEvent(self, event):
        if hasattr(self, '_drag_pos') and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos); event.accept()
        else: super().mouseMoveEvent(event)
    def mouseReleaseEvent(self, event):
        self._drag_pos = None; super().mouseReleaseEvent(event)

    def build_left(self, parent):
        f=QFrame(parent); f.setObjectName("sidebar")
        new=QPushButton("＋  Новый чат", f); new.setObjectName("newChat"); new.setGeometry(17, 18, 270, 47); new.clicked.connect(self.new_chat)
        nav=[("chat","Чат","Ctrl+1",lambda:None),("folder","Проекты","Ctrl+2",self.choose_project),("check","Проверки","Ctrl+3",self.show_checks),("chart","Статус","Ctrl+4",self.show_status),("settings","Настройки","Ctrl+5",self.open_settings)]
        y=82
        for name,text,hot,fn in nav:
            row=QFrame(f); row.setObjectName("navRow"); row.setGeometry(17,y,270,42)
            ib=QToolButton(row); ib.setObjectName("navIcon"); ib.setIcon(svg_icon(name,22,"#b6c7e8")); ib.setIconSize(QSize(22,22)); ib.setGeometry(8,7,28,28); ib.clicked.connect(fn)
            lab=QLabel(text,row); lab.setObjectName("navText"); lab.setGeometry(56,6,120,30)
            hk=QLabel(hot,row); hk.setObjectName("hotkey"); hk.setGeometry(210,7,53,27)
            row.mousePressEvent=lambda e, f=fn: f() if e.button()==Qt.LeftButton else None
            y += 48
        line=QFrame(f); line.setObjectName("line"); line.setGeometry(0, 272, 304, 1)
        h=QLabel("История чатов",f); h.setObjectName("sectionTitle"); h.setGeometry(22,289,180,24)
        plus=QToolButton(f); plus.setText("+"); plus.setObjectName("historyPlus"); plus.setGeometry(254,283,34,34); plus.clicked.connect(self.new_chat)
        self.history=QListWidget(f); self.history.setObjectName("history"); self.history.setGeometry(17, 322, 270, 414); self.history.itemClicked.connect(self.load_history)
        prof=QFrame(f); prof.setObjectName("profileDock"); prof.setGeometry(11, 737, 282, 198)
        lab=QLabel("Ваши профили",prof); lab.setObjectName("sectionTitle"); lab.setGeometry(12,10,150,22)
        close=QToolButton(prof); close.setText("×"); close.setObjectName("dockClose"); close.setGeometry(250,7,22,22)
        for i,key in enumerate(("chat","coding")):
            p=PROFILE_UI[key]; b=QPushButton(prof); b.setObjectName("profileChoice"); b.setGeometry(10, 42+i*67, 262, 59); b.clicked.connect(lambda _,k=key:self.set_profile(k))
            b.setText(f"{p['icon']}   {p['name']}\n    {p['short']}")
        return f

    def build_profile_bar(self, parent):
        f=QFrame(parent); f.setObjectName("selectorBar")
        self.profile_combo=QComboBox(f); self.profile_combo.addItem("  Обычная переписка","chat"); self.profile_combo.addItem("  </>  Кодинг","coding"); self.profile_combo.setGeometry(11,12,427,52); self.profile_combo.currentIndexChanged.connect(lambda:self.set_profile(self.profile_combo.currentData()))
        self.model_combo=QComboBox(f); self.model_combo.setGeometry(450,12,351,52); self.model_combo.setToolTip("Модель")
        return f

    def build_right(self, parent):
        w=QFrame(parent); w.setObjectName("rightPanel")
        self.project_card=ProjectCard(self); self.project_card.setParent(w); self.project_card.setGeometry(9,11,354,136)
        self.profile_card=ProfileCard(self); self.profile_card.setParent(w); self.profile_card.setGeometry(9,156,354,271)
        extra=QFrame(w); extra.setObjectName("extraCard"); extra.setGeometry(9,431,354,90)
        et=QLabel("Дополнительно",extra); et.setObjectName("cardTitle"); et.setGeometry(16,12,220,24)
        self.extra_check=QCheckBox("Автоматически применять и проверять изменения",extra); self.extra_check.setChecked(True); self.extra_check.setGeometry(16,43,320,22); self.extra_check.setObjectName("extraCheck")
        ex=QLabel("Включает анализ, тестирование и исправление ошибок",extra); ex.setObjectName("radioSub"); ex.setGeometry(39,63,300,18)
        self.status_card=StatusCard(self); self.status_card.setParent(w); self.status_card.setGeometry(9,534,354,146)
        models=QFrame(w); models.setObjectName("modelsCard"); models.setGeometry(9, 681, 354, 240)
        title=QLabel("Доступные модели",models); title.setObjectName("cardTitle"); title.setGeometry(16,13,180,24)
        self.models_preview=QLabel("Загружаю каталог…",models); self.models_preview.setObjectName("modelsList"); self.models_preview.setWordWrap(True); self.models_preview.setGeometry(16,45,320,137)
        more=QPushButton("Подробнее о моделях  ↗",models); more.setObjectName("ghostBtn"); more.setGeometry(16,199,322,30); more.clicked.connect(self.show_status)
        return w

    def set_profile(self, key):
        if key not in PROFILE_UI: key="chat"
        self.profile=key
        idx=self.profile_combo.findData(key)
        if idx>=0 and self.profile_combo.currentIndex()!=idx: self.profile_combo.setCurrentIndex(idx)
        p=PROFILE_UI[key]
        self.info_text.setText(f"{p['name']} — {p['long']}")
        self.profile_card.refresh(); self.sync_profile_controls(); self.refresh_models(); self.update_auto_visibility()

    def update_auto_visibility(self):
        visible=self.profile=="coding"
        self.auto_check.setVisible(visible); self.cycle_inline.setVisible(visible)
        self.info_dot.setText("●"); self.info_dot.setObjectName("infoDotCoding" if visible else "infoDot")
        self.info_bar.style().unpolish(self.info_bar); self.info_bar.style().polish(self.info_bar)
        if not visible: self.auto_check.setChecked(False)

    def sync_profile_controls(self):
        self.save_prefs()

    def save_prefs(self):
        self.prefs["auto_verify"] = self.auto_check.isChecked() if hasattr(self, "auto_check") else self.prefs.get("auto_verify", True)
        if hasattr(self, "profile_card"): self.prefs["max_cycles"] = self.cycle_inline.value() if hasattr(self, "cycle_inline") else self.profile_card.cycles.value()
        save_json(PREFS_FILE, self.prefs)

    def reload_router(self):
        try:
            self.router = Router(CONFIG_PATH)
            self.online.setText("●  Онлайн"); self.top_keys.setText(f"{len(self.router.api_keys)} API-ключ(ей)  •")
        except Exception as e:
            self.router = None; self.online.setText("●  Offline"); self.top_keys.setText("0 API-ключ(ей)  •"); self.phase.setText(str(e))
        if hasattr(self, "status_card"): self.status_card.refresh()
        if hasattr(self, "model_combo"): self.refresh_models()

    def refresh_models(self):
        self.model_combo.clear()
        if not self.router: self.model_combo.addItem("Модели недоступны"); return
        models=self.router.profile_info(self.profile).get("models", [])
        for m in models: self.model_combo.addItem(m)
        if hasattr(self,"models_preview"):
            self.models_preview.setText("\n".join(f"• {m}" for m in models[:5]) or "Модели не настроены")

    def new_chat(self, silent=False):
        self.current_messages = []
        if hasattr(self, "chat_layout"):
            while self.chat_layout.count() > 1:
                item = self.chat_layout.takeAt(0); w = item.widget();
                if w: w.deleteLater()
            self.chat_layout.insertWidget(0, self.welcome_card())
        if hasattr(self, "history"): self.refresh_history()

    def welcome_card(self):
        f = QFrame(); f.setObjectName("welcome"); l = QVBoxLayout(f); l.setContentsMargins(20, 18, 20, 18)
        title = QLabel("Начнём с задачи"); title.setObjectName("welcomeTitle"); l.addWidget(title)
        p = PROFILE_UI[self.profile]; d = QLabel(f"{p['name']} — {p['long']}"); d.setObjectName("muted"); d.setWordWrap(True); l.addWidget(d)
        return f

    def add_bubble(self, role, text, title=None, store=True, scroll=True):
        bar = self.chat_scroll.verticalScrollBar()
        was_bottom = bar.value() >= max(0, bar.maximum() - 24)
        bubble = ChatBubble(role, title or ("Вы" if role == "user" else "AI Router"), text)
        self.chat_layout.insertWidget(self.chat_layout.count() - 1, bubble)
        if store: self.current_messages.append({"role": role, "content": text})
        if scroll and was_bottom: QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
        return bubble

    def send_message(self):
        if self.busy: return
        text = self.input.toPlainText().strip()
        if not text: return
        if not self.router: QMessageBox.warning(self, "Router", "Сначала добавь API-ключ."); return
        if self.profile == "coding" and self.auto_check.isChecked() and not self.project:
            QMessageBox.warning(self, "Нужен проект", "Для автокода выбери папку проекта."); return
        self.input.clear(); self.add_bubble("user", text); self.stream_text = ""; self.stream_bubble = self.add_bubble("assistant", "", "AI Router · генерация…", store=False); self.set_busy(True)
        prompt = self.prefs.get("profile_prompts", {}).get(self.profile, "") or self.router.profile_info(self.profile).get("system_prompt", "")
        if self.profile == "coding" and self.auto_check.isChecked():
            fn = lambda: agent_edit_and_verify(self.router, "coding", self.project, text, int(self.cycle_inline.value()), prompt, on_event=self.worker_event, on_delta=self.worker_delta)
        else:
            msgs = list(self.current_messages)
            system = prompt or ("Ты полезный ассистент." if self.profile == "chat" else "Ты помощник программиста. Отвечай конкретно.")
            msgs.insert(0, {"role": "system", "content": system})
            fn = lambda: self.router.ask(self.profile, msgs, on_delta=self.worker_delta)
        self.thread = QThread(); self.worker = Worker(fn); self.worker.moveToThread(self.thread); self.thread.started.connect(self.worker.run); self.worker.result.connect(self.worker_result); self.worker.error.connect(self.worker_error); self.worker.finished.connect(self.thread.quit); self.worker.finished.connect(self.worker.deleteLater); self.thread.finished.connect(self.thread.deleteLater); self.thread.start()

    def worker_delta(self, delta):
        self.stream_text += delta
        self._pending_delta = self.stream_text
        if not self._delta_timer.isActive(): self._delta_timer.start(35)

    def flush_delta(self):
        if self.stream_bubble:
            self.stream_bubble.set_text(self._pending_delta)
            bar = self.chat_scroll.verticalScrollBar()
            if bar.value() >= max(0, bar.maximum() - 48): bar.setValue(bar.maximum())

    def worker_event(self, event):
        self.event_signal.emit(event) if hasattr(self, 'event_signal') else None

    def setup_signals(self):
        pass

    def worker_result(self, result):
        if self.profile == "coding" and self.auto_check.isChecked():
            # Agent events are already rendered; result is the event list.
            for e in result or []:
                if e.get("type") == "clarify": self.render_clarification(e)
        else:
            text, model, usage = result
            # streaming content was shown in a temporary bubble
            if self.stream_bubble:
                self.stream_bubble.set_text(text); self.current_messages.append({"role": "assistant", "content": text})
                self.stream_bubble = None; self.stream_text = ""
            else: self.add_bubble("assistant", text)
            self.phase.setText(f"Готово · {model}")
        self.set_busy(False); self.save_current_session()

    def worker_error(self, message):
        self.add_bubble("assistant", f"**Ошибка:** {message}"); self.phase.setText("Ошибка"); self.set_busy(False)

    def worker_event_slot(self, event):
        typ = event.get("type")
        self.phase.setText(event.get("message", typ or ""))
        if typ == "phase": self.add_bubble("assistant", f"**{event.get('message','')}**", "AI Router · процесс")
        elif typ == "changes": self.add_bubble("assistant", f"✓ Изменения применены\n\nФайлы: {', '.join(event.get('files', []))}\n\nBackup: `{event.get('backup','')}`", "AI Router · изменения")
        elif typ == "check": self.add_bubble("assistant", ("✓ Проверки прошли\n\n" if event.get("ok") else "✕ Проверки нашли ошибки\n\n") + event.get("report", ""), "AI Router · проверка")
        elif typ == "done": self.add_bubble("assistant", event.get("report", "Готово"), "AI Router · результат")
        elif typ == "clarify": self.render_clarification(event)

    def render_clarification(self, event):
        text = "**Нужно уточнение.**\n\n" + "\n".join(f"• {q}" for q in event.get("questions", []))
        if event.get("options"): text += "\n\n**Варианты:**\n" + "\n".join(f"{i+1}. {o}" for i, o in enumerate(event["options"]))
        self.add_bubble("assistant", text, "AI Router · вопрос")

    def set_busy(self, busy):
        self.busy = busy; self.send.setEnabled(not busy); self.input.setEnabled(not busy); self.phase.setText("ИИ работает…" if busy else "Готово")

    def choose_project(self):
        p = QFileDialog.getExistingDirectory(self, "Выбери папку проекта")
        if p: self.project = p; self.project_card.refresh(); self.refresh_tools()

    def show_checks(self):
        if not self.project: QMessageBox.information(self, "Проверки", "Сначала выбери проект."); return
        ptype = detect_project_type(self.project)
        if not ptype: QMessageBox.information(self, "Проверки", "Тип проекта не определён."); return
        ok, report = run_checks(ptype, self.router.cfg if self.router else {}, self.project)
        dlg = QDialog(self); dlg.setWindowTitle(f"Проверки · {ptype}"); dlg.resize(900, 650); l = QVBoxLayout(dlg); out = QPlainTextEdit(); out.setReadOnly(True); out.setPlainText(report); l.addWidget(out); b = QPushButton("Закрыть"); b.clicked.connect(dlg.accept); l.addWidget(b); dlg.exec()

    def show_status(self):
        dlg = QDialog(self); dlg.setWindowTitle("Статус AI Router"); dlg.resize(900, 700); l = QVBoxLayout(dlg)
        out = QPlainTextEdit(); out.setReadOnly(True); lines = []
        if self.router:
            lines += [f"Router: ONLINE", f"API keys: {len(self.router.api_keys)}", f"Профиль: {self.profile}", "", "Модели профиля:"]
            lines += [f"  • {m}" for m in self.router.profile_info(self.profile).get("models", [])]
            try:
                catalog = self.router.model_catalog(); lines += ["", f"Модели API ({len(catalog)}):"] + [f"  • {m.get('id')}  context={m.get('context_length','?')}" for m in catalog[:100]]
            except Exception as e: lines += ["", f"Каталог моделей: {e}"]
        else: lines.append("Router: OFFLINE")
        if self.project:
            lines += ["", f"Project: {self.project}", f"Project type: {detect_project_type(self.project)}"]
        out.setPlainText("\n".join(lines)); l.addWidget(out); b = QPushButton("Закрыть"); b.clicked.connect(dlg.accept); l.addWidget(b); dlg.exec()

    def refresh_tools(self):
        # Show useful developer tools without requiring them all to be installed.
        if not self.project: return
        tools = ["git", "python", "pip", "node", "npm", "npx", "ruff", "pytest", "eslint"]
        found = []
        for t in tools:
            try:
                r = subprocess.run([t, "--version"], capture_output=True, text=True, timeout=3)
                if r.returncode == 0: found.append(f"{t}: {((r.stdout or r.stderr).strip().splitlines() or ['?'])[0]}")
            except Exception: pass
        self.status_card.tools.setText("Инструменты:\n" + ("\n".join(found) if found else "не найдены"))

    def open_settings(self):
        dlg = QDialog(self); dlg.setWindowTitle("Настройки"); dlg.resize(760, 520); l = QVBoxLayout(dlg)
        b1 = QPushButton("🔑  API-ключи"); b1.setToolTip("Управление собственными ключами для fallback."); b1.clicked.connect(lambda: ApiDialog(self).exec()); l.addWidget(b1)
        b2 = QPushButton("💬  Prompt профиля «Обычная переписка»"); b2.clicked.connect(lambda: self.set_profile_and_prompt("chat")); l.addWidget(b2)
        b3 = QPushButton("</>  Prompt профиля «Кодинг»"); b3.clicked.connect(lambda: self.set_profile_and_prompt("coding")); l.addWidget(b3)
        info = QLabel("Ничего из этого окна не создаёт новые API-ключи. Router использует только ключи, которые ты добавил."); info.setObjectName("muted"); info.setWordWrap(True); l.addWidget(info); l.addStretch(); close = QPushButton("Закрыть"); close.clicked.connect(dlg.accept); l.addWidget(close); dlg.exec()

    def set_profile_and_prompt(self, key): self.set_profile(key); self.edit_prompt()
    def edit_prompt(self): PromptDialog(self).exec()

    def refresh_history(self):
        self.history.clear()
        for s in reversed(self.sessions[-30:]):
            item = QListWidgetItem(s.get("title", "Новый чат")); item.setData(Qt.UserRole, s); self.history.addItem(item)

    def save_current_session(self):
        if not self.current_messages: return
        title = next((m["content"] for m in self.current_messages if m["role"] == "user"), "Новый чат")[:70]
        item = {"title": title, "updated": datetime.now().isoformat(), "profile": self.profile, "messages": self.current_messages}
        self.sessions = [s for s in self.sessions if s.get("title") != title]
        self.sessions.append(item); save_json(HISTORY_FILE, self.sessions); self.refresh_history()

    def load_history(self, item):
        s = item.data(Qt.UserRole); self.current_messages = list(s.get("messages", [])); self.profile = s.get("profile", "chat"); self.set_profile(self.profile)
        while self.chat_layout.count() > 1:
            x = self.chat_layout.takeAt(0); w = x.widget();
            if w: w.deleteLater()
        for m in self.current_messages: self.add_bubble(m["role"], m["content"], scroll=False)
        QTimer.singleShot(0, lambda: self.chat_scroll.verticalScrollBar().setValue(self.chat_scroll.verticalScrollBar().maximum()))

    def eventFilter(self, obj, event):
        if obj is self.input and event.type() == event.Type.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
            self.send_message(); return True
        return super().eventFilter(obj, event)

    def closeEvent(self, e): self.save_current_session(); e.accept()

    # Signals used from worker threads. They are Qt queued connections when emitted from Worker callbacks.
    delta_signal = Signal(str)
    event_signal = Signal(dict)


STYLES = f"""
* {{ font-family: 'Segoe UI'; font-size: 13px; color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: #080d15; }}
QFrame#topbar {{ background: #090f18; border-bottom: 1px solid #1a2636; }}
QLabel#topBrand {{ color:#e9f0fb; font-size:19px; font-weight:700; }}
QLabel#topSub {{ color:#8e9db5; font-size:10px; }}
QLabel#topKeys, QLabel#topRouter {{ color:#b8c6db; font-size:12px; padding-top:5px; }}
QLabel#onlinePill {{ background:#15241f; color:#7ee0b7; border:1px solid #1b4036; border-radius:14px; padding:5px 10px; }}
QToolButton#winMin, QToolButton#winMax, QToolButton#winClose {{ background:transparent; border:0; color:#9aa9bf; font-size:18px; }}
QToolButton#winMin:hover, QToolButton#winMax:hover {{ background:#152031; }}
QToolButton#winClose:hover {{ background:#8d2d42; color:white; }}
QFrame#sidebar {{ background:#090f18; border-right:1px solid #1b2635; }}
QFrame#centerPanel, QFrame#rightPanel {{ background:#080d15; }}
QLabel#pageTitle {{ color:#e9f0fb; font-size:25px; font-weight:700; }}
QLabel#pageSubtitle {{ color:#91a1b9; font-size:13px; }}
QPushButton#newChat {{ background:#536cff; color:white; border:0; border-radius:9px; font-weight:650; font-size:13px; }}
QPushButton#newChat:hover {{ background:#637aff; }}
QFrame#navRow {{ background:transparent; border:1px solid transparent; border-radius:10px; }}
QFrame#navRow:hover {{ background:#101a2a; border-color:#182740; }}
QToolButton#navIcon {{ background:#172236; border:0; border-radius:15px; }}
QLabel#navText {{ color:#d1daea; font-size:14px; }}
QLabel#hotkey {{ color:#7890b4; background:#111b2a; border-radius:6px; padding:4px 7px; font-size:10px; }}
QFrame#line {{ background:#1a2635; border:0; }}
QLabel#sectionTitle {{ color:#a4b3ca; font-size:12px; font-weight:600; }}
QToolButton#historyPlus {{ background:#121e31; border:1px solid #263750; border-radius:9px; color:#c4d2e8; font-size:18px; }}
QToolButton#dockClose {{ background:transparent; border:0; color:#7788a2; font-size:16px; }}
QListWidget#history {{ background:transparent; border:0; outline:0; color:#cbd5e6; }}
QListWidget#history::item {{ background:transparent; padding:9px 9px; border-radius:9px; margin:2px 0; }}
QListWidget#history::item:hover {{ background:#101a2a; }}
QListWidget#history::item:selected {{ background:#172c5f; border:1px solid #3152a3; color:#f3f7ff; }}
QFrame#profileDock {{ background:#0d1624; border:1px solid #263750; border-radius:10px; }}
QPushButton#profileChoice {{ text-align:left; background:#111c2b; color:#d9e3f2; border:1px solid #263750; border-radius:8px; padding:7px 10px; font-size:11px; }}
QPushButton#profileChoice:hover {{ background:#172541; border-color:#3d59a7; }}
QFrame#selectorBar {{ background:#0e1724; border:1px solid #293b55; border-radius:10px; }}
QComboBox {{ background:#101a2a; color:#e6edf8; border:1px solid #2a3b55; border-radius:9px; padding:0 12px; font-size:13px; }}
QComboBox:hover {{ border-color:#425d90; }}
QComboBox::drop-down {{ width:32px; border:0; }}
QComboBox QAbstractItemView {{ background:#101a2a; color:#e6edf8; border:1px solid #2a3b55; selection-background-color:#233b72; }}
QFrame#infoBar {{ background:#101a29; border:1px solid #1e2e46; border-radius:10px; }}
QLabel#infoText {{ color:#aab8cc; font-size:12px; }}
QLabel#infoDot {{ color:#8496b5; font-size:20px; }}
QPushButton#ghostBtn {{ background:#152136; color:#d3deef; border:1px solid #2a3c58; border-radius:8px; font-size:11px; }}
QPushButton#ghostBtn:hover {{ background:#1a2a45; }}
QScrollArea#chatScroll {{ background:#080d15; border:0; }}
QWidget#chatBody {{ background:#080d15; }}
QFrame#welcome {{ background:#101a29; border:1px solid #22344e; border-radius:10px; }}
QLabel#welcomeTitle {{ color:#eef4fd; font-size:15px; font-weight:650; }}
QFrame#messageCard {{ background:#101a29; border:1px solid #1e2d42; border-radius:10px; }}
QFrame#messageUser QFrame#messageCard {{ background:#14233c; border-color:#294575; }}
QLabel#userAvatar, QLabel#aiAvatar {{ border-radius:18px; }}
QLabel#userAvatar {{ background:#4770d2; }}
QLabel#aiAvatar {{ background:#4b64ed; }}
QLabel#messageTitle {{ color:#e2eaf6; font-weight:650; font-size:13px; }}
QLabel#messageTime {{ color:#71819a; font-size:10px; }}
QLabel#messageBody {{ color:#d4deec; background:transparent; font-family:'Segoe UI'; font-size:13px; }}
QTextBrowser#messageBody {{ color:#d4deec; background:transparent; border:0; font-family:'Segoe UI'; font-size:13px; }}
QFrame#composerCard {{ background:#0e1725; border:1px solid #2c405e; border-radius:11px; }}
QPlainTextEdit#composer {{ background:#111b2a; color:#e6edf8; border:0; border-radius:8px; padding:9px; font-size:13px; }}
QToolButton#composerTool {{ background:transparent; color:#9fb0c8; border:0; border-radius:6px; font-size:15px; }}
QToolButton#composerTool:hover {{ background:#18263c; color:white; }}
QPushButton#sendBtn {{ background:#5570ff; color:white; border:0; border-radius:7px; font-size:18px; }}
QPushButton#sendBtn:hover {{ background:#6880ff; }}
QCheckBox#autoCheck, QCheckBox#extraCheck {{ color:#b8c6da; font-size:11px; }}
QCheckBox::indicator {{ width:17px; height:17px; border-radius:4px; border:1px solid #465a79; background:#101a2a; }}
QCheckBox::indicator:checked {{ background:#5974ff; border-color:#5974ff; }}
QFrame#card, QFrame#modelsCard, QFrame#extraCard {{ background:#0e1725; border:1px solid #22344c; border-radius:10px; }}
QLabel#cardTitle {{ color:#e1e9f5; font-size:13px; font-weight:600; }}
QLabel#projectName {{ color:#e0e8f4; font-size:12px; }}
QLabel#muted, QLabel#radioSub, QLabel#modelsList {{ color:#91a2bc; font-size:11px; }}
QLabel#statusOk {{ color:#57d9a8; background:#102d26; border-radius:10px; padding:3px 7px; font-size:10px; }}
QLabel#statValue {{ color:#dce6f5; font-size:11px; text-align:right; }}
QProgressBar {{ background:#172236; border:0; border-radius:3px; height:6px; }}
QProgressBar::chunk {{ background:#35d39b; border-radius:3px; }}
QRadioButton#profileRadio {{ color:#dbe4f1; font-size:11px; spacing:8px; }}
QRadioButton::indicator {{ width:19px; height:19px; border-radius:10px; border:1px solid #405472; background:#121d2c; }}
QRadioButton::indicator:checked {{ border:6px solid #5c77ff; background:#17233a; }}
QSpinBox#cycleInline, QSpinBox {{ background:#101a2a; color:#e2ebf7; border:1px solid #2a3c57; border-radius:6px; }}
QLabel#phase {{ color:#71839f; font-size:10px; }}
QScrollBar:vertical {{ background:transparent; width:7px; }}
QScrollBar::handle:vertical {{ background:#263750; border-radius:3px; min-height:30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
QDialog {{ background:#0b121d; }}
"""


def main():
    app = QApplication(sys.argv); app.setApplicationName("AI Router")
    win = MainWindow()
    win.event_signal.connect(win.worker_event_slot)
    win.show(); sys.exit(app.exec())


if __name__ == "__main__": main()
