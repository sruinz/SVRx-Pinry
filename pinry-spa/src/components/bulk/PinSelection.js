export default class PinSelection {
  constructor(orderedIds = []) {
    this.order = [];
    this.loadedIds = new Set();
    this.selected = new Set();
    this.anchorId = null;
    this.ownershipById = {};
    this.scope = 'loaded';
    this.scopeOrder = [];
    this.setLoadedRows(orderedIds.map(id => ({ id, owned: false })));
  }

  setLoadedRows(rows) {
    const parsedRows = PinSelection.parseRows(rows);
    if (parsedRows === null) return this.snapshot();

    this.order = parsedRows.map(row => row.id);
    this.loadedIds = new Set(this.order);
    parsedRows.forEach((row) => {
      this.ownershipById[row.id] = row.owned;
    });
    return this.snapshot();
  }

  toggle(id, modifiers = {}) {
    if (!this.loadedIds.has(id)) return this.snapshot();

    if (modifiers.shiftKey && this.anchorId !== null) {
      this.selectRange(this.anchorId, id);
    } else if (this.selected.has(id)) {
      this.selected.delete(id);
    } else {
      this.selected.add(id);
    }
    this.anchorId = id;
    return this.snapshot();
  }

  selectLoaded(ids) {
    if (!this.areUniqueLoadedIds(ids)) return this.snapshot();

    this.selected = new Set(ids);
    this.anchorId = null;
    this.scope = 'loaded';
    this.scopeOrder = [];
    return this.snapshot();
  }

  applyScope(results) {
    const parsedRows = PinSelection.parseRows(results);
    if (parsedRows === null) return this.snapshot();

    this.selected = new Set(parsedRows.map(row => row.id));
    this.anchorId = null;
    this.scope = 'all';
    this.scopeOrder = parsedRows.map(row => row.id);
    parsedRows.forEach((row) => {
      this.ownershipById[row.id] = row.owned;
    });
    return this.snapshot();
  }

  clear() {
    this.selected.clear();
    this.anchorId = null;
    this.ownershipById = {};
    this.scope = 'loaded';
    this.scopeOrder = [];
    return this.snapshot();
  }

  snapshot() {
    const selectedIds = this.order.filter(id => this.selected.has(id));
    if (this.scope === 'all') {
      this.scopeOrder.forEach((id) => {
        if (!this.loadedIds.has(id) && this.selected.has(id)) {
          selectedIds.push(id);
        }
      });
    }

    return {
      selectedIds,
      anchorId: this.anchorId,
      ownershipById: { ...this.ownershipById },
      scope: this.scope,
    };
  }

  selectRange(startId, endId) {
    const startIndex = this.order.indexOf(startId);
    const endIndex = this.order.indexOf(endId);
    if (startIndex < 0 || endIndex < 0) {
      this.selected.add(endId);
      return;
    }

    const firstIndex = Math.min(startIndex, endIndex);
    const lastIndex = Math.max(startIndex, endIndex);
    this.order.slice(firstIndex, lastIndex + 1).forEach(id => this.selected.add(id));
  }

  areUniqueLoadedIds(ids) {
    if (!Array.isArray(ids)) return false;
    const uniqueIds = new Set(ids);
    return uniqueIds.size === ids.length && ids.every(id => this.loadedIds.has(id));
  }

  static parseRows(rows) {
    if (!Array.isArray(rows)) return null;
    const ids = new Set();
    const parsedRows = [];

    for (let index = 0; index < rows.length; index += 1) {
      const row = rows[index];
      if (
        !row
        || !Number.isInteger(row.id)
        || row.id <= 0
        || typeof row.owned !== 'boolean'
        || ids.has(row.id)
      ) {
        return null;
      }
      ids.add(row.id);
      parsedRows.push({ id: row.id, owned: row.owned });
    }

    return parsedRows;
  }
}
