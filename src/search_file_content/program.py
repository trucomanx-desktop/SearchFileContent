#!/usr/bin/python3

"""
File Searcher - PyQt5 application to search files by content
"""

import os
import sys
import signal
import subprocess

from pathlib import Path
from typing import Optional

from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer
)
from PyQt5.QtGui import (
    QFont, QColor, QPalette, QSyntaxHighlighter, QTextCharFormat, QIcon, QDesktopServices
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QTabWidget, QListWidget,
    QListWidgetItem, QTextEdit, QProgressBar, QSplitter,
    QCheckBox, QGroupBox, QScrollArea, QFrame, QToolButton,
    QSizePolicy, QSpacerItem, QMessageBox, QFileDialog,
    QPlainTextEdit, QStatusBar, QToolBar, QAction
)

from PyQt5.QtCore import Qt, QUrl

import search_file_content.about as about
import search_file_content.modules.configure as configure 
from search_file_content.modules.resources import resource_path

from search_file_content.modules.wabout    import show_about_window
from search_file_content.desktop import create_desktop_file, create_desktop_directory, create_desktop_menu


# ---------- Path to config file ----------
CONFIG_PATH = os.path.join( os.path.expanduser("~"),
                            ".config", 
                            about.__package__, 
                            "config.json" )

DEFAULT_CONTENT={   
    "toolbar_configure": "Configure",
    "toolbar_configure_tooltip": "Open the configure Json file of program GUI",
    "toolbar_about": "About",
    "toolbar_about_tooltip": "About the program",
    "toolbar_coffee": "Coffee",
    "toolbar_coffee_tooltip": "Buy me a coffee (TrucomanX)",
    "window_width": 1024,
    "window_height": 800
}

configure.verify_default_config(CONFIG_PATH,default_content=DEFAULT_CONTENT)

CONFIG=configure.load_config(CONFIG_PATH)

# ---------------------------------------


MATCH_HL = "#4a3f00"
MATCH_FG = "#f1fa8c"

DEFAULT_IGNORED_EXTS = [".npy", ".pyc", ".bin", ".exe", ".png", ".jpg", ".jpeg"]


# ─────────────────────────── WORKERS ──────────────────────────────

class FileScanWorker(QThread):
    """Thread 1: recursively scan directory and apply path filters."""
    progress    = pyqtSignal(int, int)   # current, total_estimate
    file_found  = pyqtSignal(str)
    finished    = pyqtSignal(list)
    error       = pyqtSignal(str)

    def __init__(self, directory: str, filters: dict):
        super().__init__()
        self.directory = directory
        self.filters   = filters
        self._abort    = False

    def abort(self):
        self._abort = True

    def run(self):
        try:
            results = []
            count   = 0
            root    = Path(self.directory)

            if not root.exists():
                self.error.emit(f"Directory not found: {self.directory}")
                return

            for path in root.rglob("*"):
                if self._abort:
                    break
                if not path.is_file():
                    continue

                fp = str(path)

                # Filter: ends with
                if self.filters.get("ends_enabled"):
                    suffix = self.filters.get("ends_text", "").strip()
                    if suffix and not fp.endswith(suffix):
                        continue

                # Filter: contains in path
                if self.filters.get("contains_enabled"):
                    needle = self.filters.get("contains_text", "").strip()
                    if needle and needle not in fp:
                        continue

                # Filter: ignored extensions
                if self.filters.get("ext_enabled"):
                    ignored = self.filters.get("ignored_exts", [])
                    ext = path.suffix.lower()
                    if ignored and ext in [e.lower() for e in ignored]:
                        continue

                results.append(fp)
                count += 1
                self.file_found.emit(fp)
                if count % 50 == 0:
                    self.progress.emit(count, count + 1)

            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))


class ContentSearchWorker(QThread):
    """Thread 2: search text inside each file collected by Thread 1."""
    progress      = pyqtSignal(int, int)          # done, total
    match_found   = pyqtSignal(str, list)          # filepath, [(lineno, text)]
    finished      = pyqtSignal(int)               # total matches
    error         = pyqtSignal(str)

    def __init__(self, files: list, search_text: str, case_sensitive: bool = True):
        super().__init__()
        self.files          = files
        self.search_text    = search_text
        self.case_sensitive = case_sensitive
        self._abort         = False

    def abort(self):
        self._abort = True

    def run(self):
        try:
            total   = len(self.files)
            matches = 0
            if self.case_sensitive:
                needle = self.search_text
            else:
                needle = self.search_text.lower()

            for i, fp in enumerate(self.files):
                if self._abort:
                    break
                self.progress.emit(i + 1, total)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()

                    hits = []
                    for lineno, line in enumerate(lines, start=1):
                        hay = line if self.case_sensitive else line.lower()
                        if needle in hay:
                            hits.append((lineno, line.rstrip()))

                    if hits:
                        matches += 1
                        self.match_found.emit(fp, hits)

                except (PermissionError, IsADirectoryError, OSError):
                    pass

            self.finished.emit(matches)
        except Exception as e:
            self.error.emit(str(e))


# ─────────────────────────── HIGHLIGHTER ──────────────────────────

class MatchHighlighter(QSyntaxHighlighter):
    def __init__(self, parent, keyword: str, case_sensitive: bool = True):
        super().__init__(parent)
        self.case_sensitive = case_sensitive
        self.keyword = keyword if case_sensitive else keyword.lower()
        self._fmt = QTextCharFormat()
        self._fmt.setBackground(QColor(MATCH_HL))
        self._fmt.setForeground(QColor(MATCH_FG))
        self._fmt.setFontWeight(QFont.Bold)

    def highlightBlock(self, text: str):
        if not self.keyword:
            return
        hay = text if self.case_sensitive else text.lower()
        idx = 0
        while True:
            pos = hay.find(self.keyword, idx)
            if pos == -1:
                break
            self.setFormat(pos, len(self.keyword), self._fmt)
            idx = pos + len(self.keyword)

    def update_keyword(self, kw: str, case_sensitive: bool = True):
        self.case_sensitive = case_sensitive
        self.keyword = kw if case_sensitive else kw.lower()
        self.rehighlight()


# ─────────────────────────── FILTER TAB ───────────────────────────

class ExtensionList(QWidget):
    """Small widget: type an extension and add/remove from a list."""
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        row = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(".log  .bin  .exe …")
        self.edit.returnPressed.connect(self._add)

        btn_add = QPushButton("Add")
        btn_add.setFixedWidth(60)
        btn_add.clicked.connect(self._add)

        btn_rem = QPushButton("Remove")
        btn_rem.setObjectName("danger")
        btn_rem.setFixedWidth(75)
        btn_rem.clicked.connect(self._remove)

        row.addWidget(self.edit)
        row.addWidget(btn_add)
        row.addWidget(btn_rem)
        layout.addLayout(row)

        self.lst = QListWidget()
        self.lst.setFixedHeight(100)
        layout.addWidget(self.lst)
        for ext in DEFAULT_IGNORED_EXTS:
            self.lst.addItem(ext)

    def _add(self):
        txt = self.edit.text().strip()
        if not txt:
            return
        if not txt.startswith("."):
            txt = "." + txt
        txt = txt.lower()
        existing = [self.lst.item(i).text() for i in range(self.lst.count())]
        if txt not in existing:
            self.lst.addItem(txt)
        self.edit.clear()

    def _remove(self):
        for item in self.lst.selectedItems():
            self.lst.takeItem(self.lst.row(item))

    def get_extensions(self) -> list:
        return [self.lst.item(i).text() for i in range(self.lst.count())]


class FiltersTab(QWidget):
    def __init__(self):
        super().__init__()
        # Outer layout holds just the scroll area
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        inner = QWidget()
        scroll.setWidget(inner)

        layout = QVBoxLayout(inner)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        # ── Ends-with filter ──
        grp_ends = QGroupBox("Path ends with")
        grp_ends.setCheckable(True)
        grp_ends.setChecked(False)
        grp_ends.setToolTip("Enable this filter")
        v_ends = QVBoxLayout(grp_ends)
        self.edit_ends = QLineEdit()
        self.edit_ends.setPlaceholderText("e.g.  _test.py   or   config.json")
        lbl_ends = QLabel("Match only files whose full path ends with:")
        lbl_ends.setObjectName("subtitle")
        v_ends.addWidget(lbl_ends)
        v_ends.addWidget(self.edit_ends)
        layout.addWidget(grp_ends)

        # ── Contains filter ──
        grp_cont = QGroupBox("Path contains")
        grp_cont.setCheckable(True)
        grp_cont.setChecked(False)
        grp_cont.setToolTip("Enable this filter")
        v_cont = QVBoxLayout(grp_cont)
        self.edit_cont = QLineEdit()
        self.edit_cont.setPlaceholderText("e.g.  /tests/   or   mymodule")
        lbl_cont = QLabel("Match only files whose path contains this substring:")
        lbl_cont.setObjectName("subtitle")
        v_cont.addWidget(lbl_cont)
        v_cont.addWidget(self.edit_cont)
        layout.addWidget(grp_cont)

        # ── Ignored extensions ──
        grp_ext = QGroupBox("Ignore extensions")
        grp_ext.setCheckable(True)
        grp_ext.setChecked(True)
        grp_ext.setToolTip("Enable this filter")
        v_ext = QVBoxLayout(grp_ext)
        lbl_ext = QLabel("Skip files with these extensions:")
        lbl_ext.setObjectName("subtitle")
        self.ext_list = ExtensionList()
        v_ext.addWidget(lbl_ext)
        v_ext.addWidget(self.ext_list)
        layout.addWidget(grp_ext)

        layout.addStretch()

        # ── Case-sensitive ──
        grp_case = QGroupBox("Content search")
        v_case = QVBoxLayout(grp_case)
        self.chk_case = QCheckBox("Case sensitive")
        self.chk_case.setChecked(True)
        v_case.addWidget(self.chk_case)
        layout.insertWidget(layout.count() - 1, grp_case)  # before the stretch

        # keep references for get_filters()
        self.grp_ends = grp_ends
        self.grp_cont = grp_cont
        self.grp_ext  = grp_ext


    def get_filters(self) -> dict:
        return {
            "ends_enabled":    self.grp_ends.isChecked(),
            "ends_text":       self.edit_ends.text(),
            "contains_enabled":self.grp_cont.isChecked(),
            "contains_text":   self.edit_cont.text(),
            "ext_enabled":     self.grp_ext.isChecked(),
            "ignored_exts":    self.ext_list.get_extensions(),
            "case_sensitive":   self.chk_case.isChecked(),
        }


# ─────────────────────────── SEARCH TAB ───────────────────────────

class SearchTab(QWidget):
    def __init__(self, filters_tab: FiltersTab):
        super().__init__()
        self.filters_tab   = filters_tab
        self.status_bar    = None   # set by MainWindow after construction
        self._all_files    = []
        self._results      = {}    # filepath → [(lineno, text)]
        self._scan_worker  = None
        self._search_worker= None
        self._highlighter  = None

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(18, 18, 18, 12)
        root_layout.setSpacing(10)

        # ── Top controls ──
        ctrl_frame = QWidget()
        ctrl_layout = QVBoxLayout(ctrl_frame)
        ctrl_layout.setContentsMargins(0, 0, 0, 0)
        ctrl_layout.setSpacing(8)

        # Directory row
        dir_row = QHBoxLayout()
        lbl_dir = QLabel("Directory")
        lbl_dir.setFixedWidth(70)
        self.edit_dir = QLineEdit()
        self.edit_dir.setPlaceholderText("/path/to/search …")
        btn_browse = QPushButton("Browse")
        btn_browse.setObjectName("secondary")
        btn_browse.setFixedWidth(72)
        btn_browse.clicked.connect(self._browse)
        dir_row.addWidget(lbl_dir)
        dir_row.addWidget(self.edit_dir)
        dir_row.addWidget(btn_browse)
        ctrl_layout.addLayout(dir_row)

        # Search text row
        txt_row = QHBoxLayout()
        lbl_txt = QLabel("Search text")
        lbl_txt.setFixedWidth(70)
        self.edit_text = QLineEdit()
        self.edit_text.setPlaceholderText("Text to find inside files …")
        self.btn_search = QPushButton("Search")
        self.btn_search.setFixedWidth(72)
        self.btn_search.clicked.connect(self._start)
        self.btn_abort = QPushButton("Abort")
        self.btn_abort.setObjectName("danger")
        self.btn_abort.setFixedWidth(60)
        self.btn_abort.setEnabled(False)
        self.btn_abort.clicked.connect(self._abort)
        txt_row.addWidget(lbl_txt)
        txt_row.addWidget(self.edit_text)
        txt_row.addWidget(self.btn_search)
        txt_row.addWidget(self.btn_abort)
        ctrl_layout.addLayout(txt_row)

        root_layout.addWidget(ctrl_frame)


        # ── Splitter: file list | preview ──
        splitter = QSplitter(Qt.Horizontal)

        # Left: file list
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        lbl_files = QLabel("Matched files")
        lbl_files.setObjectName("subtitle")
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("subtitle")
        top_row = QHBoxLayout()
        top_row.addWidget(lbl_files)
        top_row.addStretch()
        top_row.addWidget(self.lbl_count)
        left_layout.addLayout(top_row)
        self.file_list = QListWidget()
        self.file_list.currentItemChanged.connect(self._on_file_selected)
        left_layout.addWidget(self.file_list)

        # Right: preview
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        lbl_prev = QLabel("Preview — matching lines")
        lbl_prev.setObjectName("subtitle")
        right_layout.addWidget(lbl_prev)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        right_layout.addWidget(self.preview)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 5)

        root_layout.addWidget(splitter, stretch=1)

    # ── slots ──────────────────────────────────────────────────────

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select Directory")
        if d:
            self.edit_dir.setText(d)

    def _start(self):
        directory = self.edit_dir.text().strip()
        search_text = self.edit_text.text().strip()

        if not directory:
            QMessageBox.warning(self, "Missing directory", "Please enter a directory to search.")
            return
        if not search_text:
            QMessageBox.warning(self, "Missing search text", "Please enter text to search for.")
            return

        self._all_files = []
        self._results   = {}
        self.file_list.clear()
        self.preview.clear()
        self.lbl_count.setText("")
        self.btn_search.setEnabled(False)
        self.btn_abort.setEnabled(True)

        # Phase 1 – scan files
        self.status_bar.showMessage("Phase 1/2 — Scanning files …")
        self.status_bar.progress.setRange(0, 0)   # indeterminate

        filters = self.filters_tab.get_filters()
        self._scan_worker = FileScanWorker(directory, filters)
        self._scan_worker.file_found.connect(self._on_file_found)
        self._scan_worker.finished.connect(lambda files: self._phase2(files, search_text))
        self._scan_worker.error.connect(self._on_error)
        self._scan_worker.start()

    def _abort(self):
        if self._scan_worker and self._scan_worker.isRunning():
            self._scan_worker.abort()
        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.abort()
        self.status_bar.showMessage("Aborted by user.")
        self._done()

    def _phase2(self, files: list, search_text: str):
        if not files:
            self.status_bar.showMessage("No files found matching path filters.")
            self._done()
            return

        self._all_files = files
        n = len(files)
        self.status_bar.showMessage(f"Phase 2/2 — Searching content in {n} file(s) …")
        self.status_bar.progress.setRange(0, n)
        self.status_bar.progress.setValue(0)

        self._search_worker = ContentSearchWorker(files, search_text,
            case_sensitive=self.filters_tab.get_filters().get('case_sensitive', True))
        self._search_worker.progress.connect(lambda d, t: self.status_bar.progress.setValue(d))
        self._search_worker.match_found.connect(self._on_match_found)
        self._search_worker.finished.connect(self._on_search_done)
        self._search_worker.error.connect(self._on_error)

        # Build / update highlighter
        cs = self.filters_tab.get_filters().get('case_sensitive', True)
        if self._highlighter:
            self._highlighter.update_keyword(search_text, case_sensitive=cs)
        else:
            self._highlighter = MatchHighlighter(self.preview.document(), search_text, case_sensitive=cs)

        self._search_worker.start()

    def _on_file_found(self, fp: str):
        pass   # could show counter; file list built in phase 2

    def _on_match_found(self, fp: str, hits: list):
        self._results[fp] = hits
        short = os.path.basename(fp)
        item = QListWidgetItem(f"  {short}")
        item.setData(Qt.UserRole, fp)
        item.setToolTip(fp)
        self.file_list.addItem(item)
        cnt = self.file_list.count()
        self.lbl_count.setText(f"{cnt} match{'es' if cnt != 1 else ''}")

    def _on_search_done(self, total: int):
        n_files   = len(self._all_files)
        n_matches = total
        self.status_bar.showMessage(
            f"Done — {n_matches} file(s) with matches out of {n_files} scanned."
        )
        self.status_bar.progress.setRange(0, 100)
        self.status_bar.progress.setValue(100)
        self._done()

    def _on_error(self, msg: str):
        QMessageBox.critical(self, "Error", msg)
        self._done()

    def _done(self):
        self.btn_search.setEnabled(True)
        self.btn_abort.setEnabled(False)

    def _on_file_selected(self, current, _previous):
        if current is None:
            return
        fp = current.data(Qt.UserRole)
        hits = self._results.get(fp, [])
        self.preview.clear()

        lines_text = []
        for lineno, line in hits:
            lines_text.append(f"L{lineno:>5}  │  {line}")

        self.preview.setPlainText("\n".join(lines_text))

        if self._highlighter:
            self._highlighter.rehighlight()


# ─────────────────────────── MAIN WINDOW ──────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        self.setWindowTitle(about.__program_name__)
        self.resize(CONFIG["window_width"], CONFIG["window_height"])
        
        ## Icon
        # Get base directory for icons
        self.icon_path = resource_path("icons", "logo.png")
        self.setWindowIcon(QIcon(self.icon_path)) 
        
        self._create_toolbar()
        self._generate_ui()
        
    def _generate_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 8)
        layout.setSpacing(8)

        # Tabs
        self.tabs = QTabWidget()
        filters_tab = FiltersTab()
        search_tab  = SearchTab(filters_tab)

        self.tabs.addTab(search_tab,  "  Search  ")
        self.tabs.addTab(filters_tab, "  Filters  ")
        layout.addWidget(self.tabs)

        # Status bar with embedded progress bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("Ready — set a directory and search text, then click Search.")

        self.status.progress = QProgressBar()
        self.status.progress.setRange(0, 100)
        self.status.progress.setValue(0)
        self.status.progress.setFixedWidth(200)
        self.status.progress.setFixedHeight(14)
        self.status.progress.setTextVisible(False)
        self.status.addPermanentWidget(self.status.progress)

        search_tab.status_bar = self.status

    def _create_toolbar(self):
        self.toolbar = self.addToolBar("Main")
        self.toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)

        # Adicionar o espaçador
        self.toolbar_spacer = QWidget()
        self.toolbar_spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.toolbar.addWidget(self.toolbar_spacer)
        
        #
        self.configure_action = QAction(QIcon.fromTheme("document-properties"), 
                                        CONFIG["toolbar_configure"], 
                                        self)
        self.configure_action.setToolTip(CONFIG["toolbar_configure_tooltip"])
        self.configure_action.triggered.connect(self.open_configure_editor)
        self.toolbar.addAction(self.configure_action)
        
        #
        self.about_action = QAction(QIcon.fromTheme("help-about"), 
                                    CONFIG["toolbar_about"], 
                                    self)
        self.about_action.setToolTip(CONFIG["toolbar_about_tooltip"])
        self.about_action.triggered.connect(self.open_about)
        self.toolbar.addAction(self.about_action)
        
        # Coffee
        self.coffee_action = QAction(   QIcon.fromTheme("emblem-favorite"), 
                                        CONFIG["toolbar_coffee"], 
                                        self)
        self.coffee_action.setToolTip(CONFIG["toolbar_coffee_tooltip"])
        self.coffee_action.triggered.connect(self.on_coffee_action_click)
        self.toolbar.addAction(self.coffee_action)

        # Conectar ao sinal de mudança de orientação
        self.toolbar.orientationChanged.connect(self.on_update_spacer_policy)
        self.on_update_spacer_policy()

    def on_update_spacer_policy(self):
        """Atualiza a política do espaçador baseado na orientação da toolbar"""
        if self.toolbar.orientation() == Qt.Horizontal:
            # Horizontal: expande na largura
            self.toolbar_spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        else:
            # Vertical: expande na altura
            self.toolbar_spacer.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def _open_file_in_text_editor(self, filepath):
        if os.name == 'nt':  # Windows
            os.startfile(filepath)
        elif os.name == 'posix':  # Linux/macOS
            subprocess.run(['xdg-open', filepath])
        
    def open_configure_editor(self):
        self._open_file_in_text_editor(CONFIG_PATH)

    def open_about(self):
        data={
            "version": about.__version__,
            "package": about.__package__,
            "program_name": about.__program_name__,
            "author": about.__author__,
            "email": about.__email__,
            "description": about.__description__,
            "url_source": about.__url_source__,
            "url_doc": about.__url_doc__,
            "url_funding": about.__url_funding__,
            "url_bugs": about.__url_bugs__
        }
        show_about_window(data,self.icon_path)

    def on_coffee_action_click(self):
        QDesktopServices.openUrl(QUrl("https://ko-fi.com/trucomanx"))

# ─────────────────────────── ENTRY POINT ──────────────────────────

def main():
    signal.signal(signal.SIGINT, signal.SIG_DFL)
       
    '''
    #ensure_mime_type("npy", "application/x-npy", "NumPy array file")
    icon_path=resource_path("icons", "logo.png")
    extras="" # "MimeType=text/vnd.graphviz;" # "MimeType=application/x-npy;"
    
    create_desktop_directory()    
    create_desktop_menu()
    create_desktop_file(os.path.join("~",".local","share","applications"), 
                        program_name=about.__program_name__,
                        extras=extras,
                        icon_path=icon_path)
    
    for n in range(len(sys.argv)):
        if sys.argv[n] == "--autostart":
            create_desktop_directory(overwrite = True)
            create_desktop_menu(overwrite = True)
            create_desktop_file(os.path.join("~",".config","autostart"), 
                                overwrite=True, 
                                program_name=about.__program_name__,
                                extras=extras,
                                icon_path=icon_path)
            return
        if sys.argv[n] == "--applications":
            create_desktop_directory(overwrite = True)
            create_desktop_menu(overwrite = True)
            create_desktop_file(os.path.join("~",".local","share","applications"), 
                                overwrite=True, 
                                program_name=about.__program_name__,
                                extras=extras,
                                icon_path=icon_path)
            return
    '''
    
    app = QApplication(sys.argv)
    app.setApplicationName(about.__package__) 
    
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
