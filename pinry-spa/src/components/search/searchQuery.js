export const SEARCH_FIELDS = ['animation', 'aspect', 'min_width', 'min_height', 'date_from', 'date_to'];

export function validateSearchFilters(raw = {}) {
  const filters = {};
  const errors = {};
  SEARCH_FIELDS.forEach((key) => {
    if (raw[key] === undefined || raw[key] === '') return;
    const value = typeof raw[key] === 'number' ? String(raw[key]) : raw[key];
    let valid = typeof value === 'string';
    if (key === 'animation') valid = valid && ['static', 'animated'].includes(value);
    else if (key === 'aspect') valid = valid && ['landscape', 'portrait', 'square'].includes(value);
    else if (key.startsWith('min_')) {
      valid = valid && /^[0-9]{1,10}$/.test(value) && Number(value) >= 1 && Number(value) <= 2147483647;
    } else {
      const parsed = new Date(`${value}T00:00:00Z`);
      valid = valid && /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value)
        && Number(value.slice(0, 4)) >= 2 && Number(value.slice(0, 4)) <= 9998
        && !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
    }
    if (valid) filters[key] = value;
    else errors[key] = 'invalid';
  });
  if (filters.date_from && filters.date_to && filters.date_from > filters.date_to) {
    errors.date_to = 'before_start';
  }
  return { filters, errors };
}

export function readSearchQuery(query = {}) {
  const { filters, errors } = validateSearchFilters(query);
  SEARCH_FIELDS.forEach((key) => {
    if (query[key] === '' || query[key] === null) errors[key] = 'invalid';
  });
  if (query.mode !== undefined && !['pins', 'boards'].includes(query.mode)) errors.mode = 'invalid';
  if (query.q !== undefined && typeof query.q !== 'string') errors.q = 'invalid';
  const rawTags = query.tag === undefined ? [] : [].concat(query.tag);
  if (rawTags.some(tag => typeof tag !== 'string')) errors.tag = 'invalid';
  const selected = [...new Set(rawTags.filter(tag => typeof tag === 'string' && tag !== ''))];
  let filterType = null;
  if (query.mode === 'boards') filterType = 'Board';
  else if (query.mode === 'pins' || selected.length
    || SEARCH_FIELDS.some(key => Object.prototype.hasOwnProperty.call(query, key))) filterType = 'Tag';
  return {
    filterType,
    selected: filterType === 'Board' ? (query.q || '') : selected,
    filters,
    errors,
  };
}

export function writeSearchQuery(state) {
  if (state.filterType === 'Board') return { mode: 'boards', q: state.selected };
  const query = { mode: 'pins' };
  if (state.selected.length) query.tag = state.selected.slice();
  return { ...query, ...validateSearchFilters(state.filters).filters };
}
