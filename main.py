#!/usr/bin/env python3
"""gem5 stats viewer: the sections of stats.txt as a table, one column per kernel.

    pip install PySide6
    python3 main.py [m5out/stats.txt [run.log]]      (Windows: py main.py ...)

Next to this file: stats_viewer.ui (the window; open and edit it in Qt Designer, it is loaded
at run time, so keep the objectNames) and stats_sections.py (the parser, shared with the
command-line tool, so both show the same numbers).

run.log is the benchmark output with the "section n = frame f stage" lines. Without it the
sections are only numbered and "Per stage" is off. When you open a stats.txt, the viewer looks
for simout, simout.txt or run.log in the same folder and takes the first one with that legend.
"""
import csv
import math
import os
import re
import sys

from PySide6.QtCore import (QAbstractTableModel, QFile, QIODevice, QModelIndex,
                            QSortFilterProxyModel, Qt)
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QGroupBox,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPushButton, QRadioButton, QTableView, QToolButton)

import stats_sections as ss

HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "stats_viewer.ui")
SORT_ROLE = Qt.UserRole
LEGEND_FILES = ("simout", "simout.txt", "run.log")

# One regex per presetCombo item, in the same order as in the .ui (None = no preset filter).
# If you add an item in Designer, add its regex here; an item without one shows all stats.
PRESETS = [
    None,                                                                   # All stats
    r"^system\.cpu\.(numCycles|committedInsts|committedOps|ipc|cpi)(::0)?$",  # Core
    r"dispatchStallReason::",                                               # Topdown
    r"(?i)(cache|l2|l3|tlb).*(miss|hit|access)",                            # Memory
    r"committedInstType_\d+::Vector",                                       # Vector mix
    r"committedInstType_\d+::VectorWholeRegister",                          # Spills
]


def is_zero(v):
    return v is None or v != v or v == 0


class StatsModel(QAbstractTableModel):
    """Rows: stat names. Column 0: the name; then one column per shown kernel or section."""

    def __init__(self):
        super().__init__()
        self.names, self.headers, self.tips, self.columns, self.desc = [], [], [], [], {}

    def set_table(self, names, headers, tips, columns, desc):
        self.beginResetModel()
        self.names, self.headers, self.tips, self.columns, self.desc = names, headers, tips, columns, desc
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.names)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 1 + len(self.columns)

    def value(self, row, col):
        """Raw number of a value cell (col >= 1), None if the stat is not in that column."""
        return self.columns[col - 1].get(self.names[row])

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r, c = index.row(), index.column()
        name = self.names[r]
        if c == 0:
            if role in (Qt.DisplayRole, SORT_ROLE):
                return name
            if role == Qt.ToolTipRole:
                return f"{name}\n{self.desc[name]}" if self.desc.get(name) else name
            return None
        v = self.value(r, c)
        if role == Qt.DisplayRole:
            return ss.fmt(v)
        if role == Qt.TextAlignmentRole:
            return Qt.AlignRight | Qt.AlignVCenter
        if role == Qt.ToolTipRole:
            return "not in this section" if v is None else repr(v)
        if role == SORT_ROLE:
            return v
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return "stat" if section == 0 else self.headers[section - 1]
        if role == Qt.ToolTipRole and section > 0:
            return self.tips[section - 1]
        return None


class StatsFilter(QSortFilterProxyModel):
    """Preset AND search on the name; hide zeros on the values; numeric sort."""

    def __init__(self):
        super().__init__()
        self.preset = self.search = None
        self.hide_zeros = True
        self.setSortRole(SORT_ROLE)

    def set_filters(self, preset, search, hide_zeros):
        if hasattr(self, "endFilterChange"):            # Qt >= 6.10 (invalidateFilter deprecated)
            self.beginFilterChange()
            self.preset, self.search, self.hide_zeros = preset, search, hide_zeros
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:                                           # older Qt
            self.preset, self.search, self.hide_zeros = preset, search, hide_zeros
            self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        m = self.sourceModel()
        name = m.names[row]
        if self.preset is not None and not self.preset.search(name):
            return False
        if self.search is not None and not self.search.search(name):
            return False
        if self.hide_zeros and m.columns:
            if all(is_zero(col.get(name)) for col in m.columns):
                return False
        return True

    def lessThan(self, left, right):
        a, b = left.data(SORT_ROLE), right.data(SORT_ROLE)
        if isinstance(a, str) or isinstance(b, str):
            return str(a).lower() < str(b).lower()
        key = lambda v: -math.inf if v is None or v != v else v      # missing / nan first
        return key(a) < key(b)


class Viewer:
    def __init__(self, ui_path=UI_FILE):
        loader = QUiLoader()
        f = QFile(ui_path)
        if not f.open(QIODevice.ReadOnly):
            raise SystemExit(f"cannot open {ui_path}")
        self.w = loader.load(f)
        f.close()
        if self.w is None:
            raise SystemExit(f"{ui_path}: {loader.errorString()}")

        c = self._child
        self.statsPathEdit = c(QLineEdit, "statsPathEdit")
        self.logPathEdit = c(QLineEdit, "logPathEdit")
        self.fileInfoLabel = c(QLabel, "fileInfoLabel")
        self.kernelList = c(QListWidget, "kernelList")
        self.kernelsGroup = c(QGroupBox, "kernelsGroup")
        self.perStageRadio = c(QRadioButton, "perStageRadio")
        self.perSectionRadio = c(QRadioButton, "perSectionRadio")
        self.presetCombo = c(QComboBox, "presetCombo")
        self.searchEdit = c(QLineEdit, "searchEdit")
        self.regexCheck = c(QCheckBox, "regexCheck")
        self.hideZerosCheck = c(QCheckBox, "hideZerosCheck")
        self.table = c(QTableView, "statsTable")
        self.countLabel = c(QLabel, "countLabel")

        self.model = StatsModel()
        self.proxy = StatsFilter()
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)
        header = self.table.horizontalHeader()
        header.setSectionsMovable(True)
        # Header clicks cycle ascending -> descending -> none; none = stats.txt order, which keeps
        # related stats (one cache, one stall reason list) together.
        if hasattr(header, "setSortIndicatorClearable"):  # Qt >= 6.1
            header.setSortIndicatorClearable(True)
        header.sortIndicatorChanged.connect(self.on_sort_changed)
        self.unsort()

        self.sections, self.legend, self.names, self.desc = [], {}, [], {}
        self.stage_data = None          # ss.by_stage(...) result, computed once per load
        self.checked = None             # names ticked in kernelList, kept across reloads

        c(QToolButton, "browseStatsButton").clicked.connect(self.browse_stats)
        c(QToolButton, "browseLogButton").clicked.connect(self.browse_log)
        c(QPushButton, "loadButton").clicked.connect(self.load)
        c(QPushButton, "selectAllButton").clicked.connect(lambda: self.check_all(True))
        c(QPushButton, "selectNoneButton").clicked.connect(lambda: self.check_all(False))
        c(QPushButton, "copyButton").clicked.connect(self.copy_rows)
        c(QPushButton, "exportCsvButton").clicked.connect(self.export_csv)
        c(QAction, "actionOpenStats").triggered.connect(self.browse_stats)
        c(QAction, "actionOpenLog").triggered.connect(self.browse_log)
        c(QAction, "actionExportCsv").triggered.connect(self.export_csv)
        self.kernelList.itemChanged.connect(self.on_kernel_changed)
        self.perStageRadio.toggled.connect(self.rebuild)
        self.presetCombo.currentIndexChanged.connect(self.apply_filters)
        self.searchEdit.textChanged.connect(self.apply_filters)
        self.regexCheck.toggled.connect(self.apply_filters)
        self.hideZerosCheck.toggled.connect(self.apply_filters)

        copy = QAction("Copy", self.table)
        copy.setShortcut(QKeySequence.Copy)
        copy.setShortcutContext(Qt.WidgetShortcut)
        copy.triggered.connect(self.copy_rows)
        self.table.addAction(copy)

        self.apply_filters()

    def _child(self, cls, name):
        obj = self.w.findChild(cls, name)
        if obj is None:
            raise SystemExit(f"stats_viewer.ui has no {cls.__name__} named {name!r}")
        return obj

    def unsort(self):
        self.table.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        self.proxy.sort(-1)

    def on_sort_changed(self, section, order):
        if section < 0:
            self.proxy.sort(-1)          # the view does not do this itself

    def status(self, text, ms=6000):
        self.w.statusBar().showMessage(text, ms)

    # ---------------------------------------------------------------- files

    def open_paths(self, stats_path, log_path=None):
        self.statsPathEdit.setText(stats_path)
        self.logPathEdit.setText(log_path or self.find_legend(stats_path) or "")
        self.load()

    @staticmethod
    def find_legend(stats_path):
        """simout / simout.txt / run.log next to stats.txt, if it has the section legend."""
        folder = os.path.dirname(os.path.abspath(stats_path))
        for name in LEGEND_FILES:
            p = os.path.join(folder, name)
            try:
                if os.path.isfile(p) and ss.read_legend(p):
                    return p
            except OSError:
                pass
        return None

    def browse_stats(self):
        start = os.path.dirname(self.statsPathEdit.text()) or os.getcwd()
        path, _ = QFileDialog.getOpenFileName(self.w, "Open gem5 stats", start,
                                              "gem5 stats (stats.txt *.txt);;All files (*)")
        if path:
            found = self.find_legend(path)
            self.open_paths(path, found)
            if found:
                self.status(f"Section legend found in {os.path.basename(found)}")

    def browse_log(self):
        start = os.path.dirname(self.logPathEdit.text() or self.statsPathEdit.text()) or os.getcwd()
        path, _ = QFileDialog.getOpenFileName(self.w, "Open the benchmark output (section legend)",
                                              start, "Logs (*.log *.txt simout);;All files (*)")
        if path:
            self.logPathEdit.setText(path)
            if self.statsPathEdit.text():
                self.load()

    def load(self):
        stats_path, log_path = self.statsPathEdit.text(), self.logPathEdit.text()
        if not stats_path:
            self.status("Choose a stats.txt first")
            return
        try:
            sections = ss.read_sections(stats_path)
            legend = ss.read_legend(log_path) if log_path else {}
        except OSError as e:
            QMessageBox.warning(self.w, "gem5 stats viewer", str(e))
            return
        if not sections:
            QMessageBox.warning(self.w, "gem5 stats viewer",
                                f"{stats_path}\nhas no '{ss.BEGIN}' sections.")
            return
        if log_path and not legend:
            self.status(f"{os.path.basename(log_path)} has no 'section n = frame f stage' lines")

        self.sections, self.legend = sections, legend
        self.names = ss.all_names(sections)
        self.desc = ss.read_descriptions(sections)
        self.stage_data = ss.by_stage(sections, legend, self.names) if legend else None
        self.w.setWindowTitle(f"gem5 stats viewer - {stats_path}")
        self.show_file_info()
        self.fill_kernel_list()

        self.perStageRadio.setEnabled(bool(legend))
        self.perStageRadio.blockSignals(True)
        if not legend:
            self.perSectionRadio.setChecked(True)
        self.perStageRadio.blockSignals(False)
        self.rebuild()

    def show_file_info(self):
        n = len(self.sections)
        if not self.legend:
            self.fileInfoLabel.setText(f"{n} sections, no legend: sections are only numbered, "
                                       "Per stage is off")
            return
        order, _, _, frames = self.stage_data
        text = (f"{n} sections, {len(self.legend)} named, {len(order)} kernels, "
                f"frames {frames[0]}-{frames[-1]}")
        if n != len(self.legend) + 1:
            text += (f"\nWarning: expected {len(self.legend) + 1} sections (legend + gem5's dump "
                     "at exit). An extra one usually means --warmup-insts-no-switch; the names "
                     "may be shifted.")
        self.fileInfoLabel.setText(text)

    # ---------------------------------------------------------------- kernels

    def fill_kernel_list(self):
        if self.legend:
            keys = self.stage_data[0]
            self.kernelsGroup.setTitle("Kernels")
        else:
            keys = [f"section {i}" for i in range(1, len(self.sections) + 1)]
            self.kernelsGroup.setTitle("Sections")
        self.kernelList.blockSignals(True)
        self.kernelList.clear()
        for k in keys:
            it = QListWidgetItem(k)
            it.setData(Qt.UserRole, k)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            on = self.checked is None or k in self.checked or not (set(keys) & self.checked)
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
            self.kernelList.addItem(it)
        self.kernelList.blockSignals(False)
        self.checked = self.ticked()

    def ticked(self):
        return {self.kernelList.item(i).data(Qt.UserRole) for i in range(self.kernelList.count())
                if self.kernelList.item(i).checkState() == Qt.Checked}

    def check_all(self, on):
        self.kernelList.blockSignals(True)
        for i in range(self.kernelList.count()):
            self.kernelList.item(i).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.kernelList.blockSignals(False)
        self.on_kernel_changed()

    def on_kernel_changed(self, *_):
        self.checked = self.ticked()
        self.rebuild()

    # ---------------------------------------------------------------- table

    def rebuild(self, *_):
        if not self.sections:
            return
        checked = self.checked or set()
        headers, tips, columns = [], [], []
        if self.perStageRadio.isChecked() and self.stage_data:
            order, calls, values, frames = self.stage_data
            nf = len(frames)
            for s in order:
                if s in checked:
                    headers.append(s)
                    tips.append(f"{s}: {calls[s]:g} call(s) per frame, frames {frames[0]}-{frames[-1]}.\n"
                                f"Counts: sum of its sections / {nf} frames. "
                                "Ratios (IPC, rates): cycle-weighted mean.")
                    columns.append(values[s])
        else:
            for i, (_, st) in enumerate(self.sections, 1):
                if self.legend:
                    if i not in self.legend or self.legend[i][1] not in checked:
                        continue
                    frame, stage = self.legend[i]
                    headers.append(f"{i}: {stage} f{frame}")
                    tips.append(f"stats.txt section {i}: frame {frame}, {stage}")
                else:
                    if f"section {i}" not in checked:
                        continue
                    headers.append(str(i))
                    tips.append(f"stats.txt section {i}")
                columns.append(st)

        self.model.set_table(self.names, headers, tips, columns, self.desc)
        if self.proxy.sortColumn() >= self.model.columnCount():
            self.unsort()
        self.size_columns()
        self.update_count()

    def size_columns(self):
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setResizeContentsPrecision(200)            # measure 200 rows, not 1000: 26 columns stay fast
        self.table.resizeColumnsToContents()
        h.resizeSection(0, min(max(h.sectionSize(0), 200), 460))

    def apply_filters(self, *_):
        i = self.presetCombo.currentIndex()
        preset = re.compile(PRESETS[i]) if 0 <= i < len(PRESETS) and PRESETS[i] else None
        text = self.searchEdit.text().strip()
        search, err = None, None
        if text:
            try:
                search = re.compile(text if self.regexCheck.isChecked() else re.escape(text), re.I)
            except re.error as e:
                err = f"invalid regex: {e}"
        self.searchEdit.setToolTip(err or "")
        self.searchEdit.setStyleSheet("QLineEdit { border: 1px solid #d03030; }" if err else "")
        self.proxy.set_filters(preset, search, self.hideZerosCheck.isChecked())
        self.update_count(err)

    def update_count(self, err=None):
        shown, total = self.proxy.rowCount(), self.model.rowCount()
        self.countLabel.setText(f"{shown:,} of {total:,} stats shown" + (f"   ({err})" if err else ""))

    # ---------------------------------------------------------------- output

    def visible_rows(self, selected_only=False):
        """Source rows in the table's current order (filtered, sorted)."""
        if selected_only:
            idx = sorted(self.table.selectionModel().selectedRows(), key=lambda i: i.row())
        else:
            idx = [self.proxy.index(r, 0) for r in range(self.proxy.rowCount())]
        return [self.proxy.mapToSource(i).row() for i in idx]

    def rows_as_table(self, rows):
        head = ["stat"] + self.model.headers
        body = []
        for r in rows:
            vals = [self.model.value(r, c) for c in range(1, self.model.columnCount())]
            body.append([self.model.names[r]] + ["" if v is None else f"{v:.10g}" for v in vals])
        return head, body

    def copy_rows(self):
        rows = self.visible_rows(selected_only=True)
        if not rows:
            self.status("Select rows to copy (click, Shift+click, Ctrl+A)")
            return
        head, body = self.rows_as_table(rows)
        QGuiApplication.clipboard().setText("\n".join("\t".join(r) for r in [head] + body) + "\n")
        self.status(f"Copied {len(rows)} row(s)")

    def export_csv(self):
        if not self.sections:
            self.status("Nothing loaded")
            return
        mode = "stage" if self.perStageRadio.isChecked() and self.stage_data else "section"
        start = os.path.join(os.path.dirname(self.statsPathEdit.text()) or os.getcwd(),
                             f"stats_per_{mode}.csv")
        path, _ = QFileDialog.getSaveFileName(self.w, "Export CSV", start, "CSV (*.csv)")
        if path:
            self.write_csv(path)

    def write_csv(self, path):
        head, body = self.rows_as_table(self.visible_rows())
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(head)
            w.writerows(body)
        self.status(f"Wrote {len(body)} rows to {path}")


def main():
    app = QApplication(sys.argv)
    viewer = Viewer()
    args = sys.argv[1:]
    if args:
        viewer.open_paths(args[0], args[1] if len(args) > 1 else None)
    viewer.w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
