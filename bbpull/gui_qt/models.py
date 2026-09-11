"""Qt models over the existing catalog.

This module is the whole reason for the PySide6 move. Measured:

    rows     Tk (widget per row)   Qt (model/view)
     120          429 ms                0.4 ms
   1,000        2,235 ms                1.7 ms
   5,000      ~14,105 ms                9.1 ms

`QAbstractListModel` holds *data*, not widgets. The view asks only for the rows
currently on screen and paints them through a delegate, so cost is flat in the
number of rows instead of linear. No engine code changes: these models read the
same `Catalog` truth the Tk build used.
"""

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

from ..catalog import ANNOUNCEMENTS_ID, walk
from ..palette import KIND_LABELS

#: Custom roles. The delegate reads the raw node through `NodeRole` and does all
#: its own layout, which is what keeps the model free of presentation concerns.
NodeRole = Qt.UserRole + 1
SelectedRole = Qt.UserRole + 2
ImpliedRole = Qt.UserRole + 3
HoverRole = Qt.UserRole + 4


class SelectionState:
    """Flat selection set with folder-implies-children semantics.

    Kept as a plain object (not a Qt selection model) because the rules are
    custom: ticking a folder means its whole subtree, and unticking one child
    releases the folder and re-selects the rest. The delegate reads this when
    painting, so a change repaints only the affected rows.
    """

    def __init__(self):
        self._selected = set()
        self._parents = {}

    def set_tree(self, nodes, parents=None):
        self._parents = dict(parents or {})

    def load(self, ids):
        self._selected = set(ids or ())

    def ids(self):
        return set(self._selected)

    def is_selected(self, node_id):
        return node_id in self._selected

    def _ancestor_selected(self, node_id):
        parent = self._parents.get(node_id)
        while parent:
            if parent in self._selected:
                return parent
            parent = self._parents.get(parent)
        return None

    def is_implied(self, node_id):
        return self._ancestor_selected(node_id) is not None

    def topmost(self):
        return [n for n in self._selected if self._ancestor_selected(n) is None]

    def toggle(self, node_id, children_of=None):
        """Toggle one row. Returns True when the selection changed."""
        if node_id in self._selected:
            self._selected.discard(node_id)
            return True

        ancestor = self._ancestor_selected(node_id)
        if ancestor is not None and children_of is not None:
            # Unticking something covered by a folder: drop the folder and
            # re-select the rest of its subtree, so "exclude one item" works.
            self._selected.discard(ancestor)
            for other in walk(children_of(ancestor)):
                other_id = other["id"]
                if other_id != node_id and not self._is_descendant(other_id, node_id):
                    self._selected.add(other_id)
            return True

        self._selected.add(node_id)
        return True

    def _is_descendant(self, candidate, ancestor):
        current = self._parents.get(candidate)
        while current:
            if current == ancestor:
                return True
            current = self._parents.get(current)
        return False

    def clear(self):
        self._selected.clear()

    def add_many(self, ids):
        self._selected.update(ids)


class CatalogModel(QAbstractListModel):
    """Rows of one folder of the course outline.

    Navigation re-points the model at a different folder and resets it, which Qt
    handles in microseconds; there are no widgets to create or destroy.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nodes = []
        self._catalog = None
        self._folder = "root"
        self._filter = ""
        self.scope_all = False
        self.selection = SelectionState()
        self.hover_id = None

    # -- data plumbing ---------------------------------------------------
    def set_catalog(self, catalog):
        """Adopt a catalog and point at its root.

        `_rebuild()` must run here, not only in `set_folder()`: the window calls
        `set_catalog()` and then `set_folder("root")`, but a caller that only set
        the catalog would see an empty model.
        """
        self.beginResetModel()
        self._catalog = catalog
        self._folder = "root"
        self._filter = ""
        self._rebuild()
        self.endResetModel()

    def set_folder(self, folder_id):
        self.beginResetModel()
        self._folder = folder_id or "root"
        self._rebuild()
        self.endResetModel()

    def set_filter(self, text):
        """Apply a text filter. `scope_all` widens it to the whole course."""
        self.beginResetModel()
        self._filter = (text or "").strip().lower()
        self._rebuild()
        self.endResetModel()

    def _rebuild(self):
        self._nodes = self._base_nodes()
        if not self._filter:
            return
        self._nodes = [n for n in self._nodes
                       if self._filter in (n.get("title") or "").lower()]

    def _base_nodes(self):
        if not self._catalog:
            return []
        if self.scope_all and self._filter:
            return list(walk(self._catalog.tree))
        if self._folder == "root":
            return list(self._catalog.tree)
        node = self._catalog.find(self._folder)
        return list((node or {}).get("children") or [])

    def refresh(self):
        """Re-read the current folder without changing it."""
        if self.rowCount() == 0:
            self.beginResetModel()
            self._rebuild()
            self.endResetModel()
            return
        top = self.index(0, 0)
        bottom = self.index(self.rowCount() - 1, 0)
        self.dataChanged.emit(top, bottom)

    @property
    def folder(self):
        return self._folder

    def node_at(self, row):
        if 0 <= row < len(self._nodes):
            return self._nodes[row]
        return None

    def row_of(self, node_id):
        for row, node in enumerate(self._nodes):
            if node["id"] == node_id:
                return row
        return -1

    # -- QAbstractListModel ----------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._nodes)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        node = self._nodes[index.row()]
        node_id = node["id"]
        if role == Qt.DisplayRole:
            return node["title"]
        if role == NodeRole:
            return node
        if role == SelectedRole:
            return self.selection.is_selected(node_id)
        if role == ImpliedRole:
            return self.selection.is_implied(node_id)
        if role == HoverRole:
            return node_id == self.hover_id
        if role == Qt.ToolTipRole:
            bits = [node["title"], KIND_LABELS.get(node["kind"], "其他")]
            if node.get("itemCount"):
                bits.append(f"{node['itemCount']} 個項目")
            return "\n".join(bits)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    # -- convenience -----------------------------------------------------
    def meta_text(self, node):
        bits = [KIND_LABELS.get(node["kind"], "其他")]
        if node["kind"] == "folder" and node.get("itemCount"):
            bits.append(f"{node['itemCount']} 個項目")
        if node.get("hasBody"):
            bits.append("含內文")
        if node.get("synthetic"):
            bits.append("系統")
        return "  ·  ".join(bits)


class CourseListModel(QAbstractListModel):
    """The sidebar's enrolled courses, rendered as cards.

    91 courses is where the Tk build spent 594 widgets; here it is 91 rows of
    plain dicts painted on demand.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []          # list of (header_text | None, meta | None)
        self._active_id = ""

    def set_courses(self, metas, active_id=""):
        self.beginResetModel()
        self._active_id = active_id or ""
        self._rows = self._group(metas)
        self.endResetModel()

    def set_active(self, course_id):
        if course_id == self._active_id:
            return
        old, new = self._active_id, course_id or ""
        self._active_id = new
        for row, entry in enumerate(self._rows):
            meta = entry[1]
            if meta and meta.course_id in (old, new):
                index = self.index(row, 0)
                self.dataChanged.emit(index, index)

    @staticmethod
    def _group(metas):
        """Group by academic year with a header row, terms descending."""
        from ..gui.courses import group_by_year, year_label

        rows = []
        grouped = group_by_year(metas)
        for year, items in grouped.items():
            rows.append((year_label(year), None))
            for meta in items:
                rows.append((None, meta))
        return rows

    def row_height(self, scale):
        """Must match `CourseDelegate.sizeHint`, or Qt crops the delegate paint."""
        from .delegates import course_row_height

        return course_row_height(scale)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        header, meta = self._rows[index.row()]
        if role == NodeRole:
            return header if meta is None else meta
        if role == Qt.DisplayRole:
            return header if meta is None else meta.display_name
        if role == Qt.ToolTipRole:
            if meta is None:
                return header
            return f"{meta.name}\n{meta.course_id}"
        if role == SelectedRole:
            return bool(meta) and meta.course_id == self._active_id
        if role == ImpliedRole:
            return meta is None      # headers are not selectable items
        return None

    def meta_at(self, index):
        if not index.isValid():
            return None
        return self._rows[index.row()][1]

    def is_header(self, index):
        if not index.isValid():
            return False
        return self._rows[index.row()][1] is None
