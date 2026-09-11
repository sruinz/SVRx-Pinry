import axios from 'axios';
import storage from './utils/storage';
import { validateExportJob, validateLatestExports } from './export/exportContract';

const API_PREFIX = '/api/v2/';

const Board = {
  create(name, private_ = false) {
    const url = `${API_PREFIX}boards/`;
    const data = { name, private: private_ };
    return new Promise(
      (resolve, reject) => {
        axios.post(url, data).then(
          (resp) => {
            if (resp.status !== 201) {
              reject(resp);
            }
            resolve(resp.data);
          },
          (error) => {
            reject(error.response);
          },
        );
      },
    );
  },
  get(boardId) {
    const url = `${API_PREFIX}boards/${boardId}/`;
    return axios.get(url);
  },
  fetchFullList(username) {
    const url = `${API_PREFIX}boards-auto-complete/?submitter__username=${username}`;
    return axios.get(url);
  },
  fetchSiteFullList() {
    const url = `${API_PREFIX}boards-auto-complete/`;
    return axios.get(url);
  },
  fetchOrder() {
    return axios.get('/api/v2/boards/order/');
  },
  saveOrder(version, boardIds) {
    return axios.put('/api/v2/boards/order/', { version, board_ids: boardIds });
  },
  fetchListWhichContains(text, offset = 0, limit = 50) {
    const prefix = `${API_PREFIX}boards/?search=${text}`;
    const url = `${prefix}&offset=${offset}&limit=${limit}`;
    return axios.get(url);
  },
  saveChanges(boardId, fieldsForm) {
    const url = `${API_PREFIX}boards/${boardId}/`;
    return axios.patch(
      url,
      fieldsForm,
    );
  },
  addToBoard(boardId, pinIds) {
    const url = `${API_PREFIX}boards/${boardId}/`;
    return axios.patch(
      url,
      { pins_to_add: pinIds },
    );
  },
  removeFromBoard(boardId, pinIds) {
    const url = `${API_PREFIX}boards/${boardId}/`;
    return axios.patch(
      url,
      { pins_to_remove: pinIds },
    );
  },
  setCover(boardId, pinId) {
    return axios.patch(
      `${API_PREFIX}boards/${boardId}/cover/`,
      { pin_id: pinId },
    );
  },
  delete(boardId) {
    const url = `${API_PREFIX}boards/${boardId}/`;
    return axios.delete(url);
  },
  deletePreview(boardId) {
    const url = `${API_PREFIX}boards/${boardId}/delete-preview/`;
    return axios.get(url);
  },
};

const Pin = {
  create(jsonData) {
    const url = `${API_PREFIX}pins/`;
    return axios.post(
      url,
      jsonData,
    );
  },
  createFromURL(jsonData) {
    return this.create(jsonData);
  },
  createFromUpload(formData) {
    const url = `${API_PREFIX}pins/`;
    return axios.post(url, formData);
  },
  deleteById(pinId) {
    const url = `${API_PREFIX}pins/${pinId}/`;
    return axios.delete(
      url,
    );
  },
  updateById(pinId, data) {
    const url = `${API_PREFIX}pins/${pinId}/`;
    return axios.patch(
      url,
      data,
    );
  },
  fetchBoardMemberships(pinId) {
    const url = `${API_PREFIX}pins/${pinId}/board-memberships/`;
    return axios.get(url);
  },
  fetchSelectionIds({ boardId = null, exclusiveOwned = false } = {}) {
    const params = {};
    if (boardId !== null) params.board_id = boardId;
    if (boardId !== null && exclusiveOwned) params.exclusive_owned = 'true';
    return axios.get('/api/v2/pins/selection-ids/', { params });
  },
  bulk(payload) {
    return axios.post('/api/v2/pins/bulk/', payload);
  },
};


function serializeQueryParams(params) {
  const pairs = [];
  Object.keys(params).forEach((key) => {
    const values = Array.isArray(params[key]) ? params[key] : [params[key]];
    values.forEach((value) => {
      pairs.push(`${encodeURIComponent(key)}=${encodeURIComponent(value)}`);
    });
  });
  return pairs.join('&');
}

function fetchPins(offset, tagFilter, userFilter, boardFilter, sortState = null) {
  const url = `${API_PREFIX}pins/`;
  const queryArgs = {
    format: 'json',
    limit: 30,
    offset,
  };
  if (sortState) {
    queryArgs.sort = sortState.mode;
    if (sortState.mode === 'random') queryArgs.random_seed = sortState.randomSeed;
  } else {
    queryArgs.ordering = '-id';
  }
  if (tagFilter) queryArgs.tags__name = tagFilter;
  if (userFilter) queryArgs.submitter__username = userFilter;
  if (boardFilter) queryArgs.pins__id = boardFilter;
  return axios.get(
    url,
    { params: queryArgs, paramsSerializer: serializeQueryParams },
  );
}

function fetchPin(pinId) {
  const url = `${API_PREFIX}pins/${pinId}`;
  return new Promise(
    (resolve, reject) => {
      const p = axios.get(
        url,
      );
      p.then(
        (resp) => {
          const response = {
            data: { results: [resp.data], next: null },
          };
          resolve(response);
        },
        (error) => {
          reject(error);
        },
      );
    },
  );
}

function fetchBoardForUser(username, offset = 0, limit = 50, sortState = null) {
  const params = {
    submitter__username: username,
    offset,
    limit,
  };
  if (sortState) {
    params.sort = sortState.mode;
    if (sortState.mode === 'random') params.random_seed = sortState.randomSeed;
  } else {
    params.ordering = '-id';
  }
  return axios.get(`${API_PREFIX}boards/`, { params });
}

const User = {
  storageKey: 'pinry.user',
  signUp(username, email, password, passwordRepeat) {
    const url = `${API_PREFIX}profile/users/`;
    return new Promise(
      (resolve, reject) => {
        const p = axios.post(
          url,
          {
            username,
            email,
            password,
            password_repeat: passwordRepeat,
          },
        );
        p.then(
          (resp) => {
            if (resp.status !== 201) {
              reject(resp);
            }
            resolve(resp.data);
          },
          (error) => {
            console.log('Failed to sign up due to unexpected error:', error);
            reject(error.response);
          },
        );
      },
    );
  },
  logIn(username, password) {
    const url = `${API_PREFIX}profile/login/`;
    return new Promise(
      (resolve, reject) => {
        const p = axios.post(
          url,
          {
            username,
            password,
          },
        );
        p.then(
          (resp) => {
            if (resp.status !== 200) {
              reject(resp);
            }
            resolve(resp.data);
          },
          (error) => {
            console.log('Failed to log in due to unexpected error:', error);
            reject(error.response);
          },
        );
      },
    );
  },
  logOut() {
    const self = this;
    return axios.post('/api-auth/logout/').then(() => {
      storage.set(self.storageKey, null, 1);
    });
  },
  fetchUserInfoByName(username) {
    /* returns null if user not logged in */
    const url = `${API_PREFIX}profile/public-users/?username=${username}`;
    return new Promise(
      (resolve) => {
        axios.get(url).then(
          (resp) => {
            const users = resp.data;
            if (users.length === 0) {
              return resolve(null);
            }
            return resolve(users[0]);
          },
        );
      },
    );
  },
  fetchUserInfo() {
    /* returns null if user not logged in */
    storage.set(this.storageKey, null, 1);
    const url = `${API_PREFIX}profile/users/`;
    return new Promise(
      (resolve) => {
        axios.get(url).then(
          (resp) => {
            const users = resp.data;
            if (users.length === 0) {
              return resolve(null);
            }
            return resolve(users[0]);
          },
          () => resolve(null),
        );
      },
    );
  },
};

const SSO = {
  policy() {
    return axios.get(`${API_PREFIX}sso/providers/`).then(({ data }) => {
      if (!data || !Array.isArray(data.providers)
        || typeof data.password_login_enabled !== 'boolean'
        || typeof data.api_tokens_enabled !== 'boolean') throw new Error('invalid SSO policy');
      return { ...data, providers: data.providers.filter(provider => provider.enabled !== false) };
    });
  },
  identities() {
    return axios.get(`${API_PREFIX}sso/identities/`).then(({ data }) => {
      if (!Array.isArray(data)) throw new Error('invalid identities');
      return data;
    });
  },
  csrfToken() {
    const cookie = document.cookie.split(';').map(value => value.trim())
      .find(value => value.startsWith('csrftoken='));
    return cookie ? decodeURIComponent(cookie.slice('csrftoken='.length)) : '';
  },
  passwordReauth(password) {
    return axios.post(`${API_PREFIX}sso/password/reauth/`, { password });
  },
  unlink(id) {
    return axios.post(`${API_PREFIX}sso/identities/${id}/unlink/`);
  },
};

const Tag = {
  fetchList() {
    const url = `${API_PREFIX}tags-auto-complete/`;
    return axios.get(url);
  },
};

const Version = {
  fetch() {
    return axios.get(`${API_PREFIX}version/`);
  },
};

const Export = {
  preview(payload) {
    return axios.post(`${API_PREFIX}exports/preview/`, payload);
  },
  create(payload) {
    return axios.post(`${API_PREFIX}exports/`, payload);
  },
  fetchLatest() {
    return axios.get(`${API_PREFIX}exports/latest/`)
      .then(response => validateLatestExports(response.data));
  },
  fetchJob(jobId) {
    return axios.get(`${API_PREFIX}exports/${jobId}/`)
      .then(response => validateExportJob(response.data));
  },
};

export default {
  SSO,
  Tag,
  Pin,
  Board,
  fetchPin,
  fetchPins,
  fetchBoardForUser,
  User,
  Version,
  Export,
};
