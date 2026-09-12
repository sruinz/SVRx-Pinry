<template>
  <div class="boards">
    <section class="section">
      <div
        v-if="isUserBoardList"
        class="container board-tools"
        data-test="board-tools"
      >
        <BoardSortControls
          :mode="sortState.mode"
          :busy="boardToolsBusy"
          @select="applySortMode"
        />
        <BoardOrderToolbar
          ref="boardOrderToolbar"
          :can-enter="canEnterBoardOrdering"
          :editing="ordering.editing"
          :loading="ordering.loading"
          :changed="isBoardOrderChanged"
          :error="ordering.error"
          :announcement="ordering.announcement"
          @enter="enterBoardOrdering"
          @save="saveBoardOrdering"
          @cancel="cancelBoardOrdering"
        />
      </div>
      <div id="boards-container" class="container" v-if="blocks">
        <div
          v-masonry=""          transition-duration="0.3s"
          item-selector=".grid-item"
          column-width=".grid-sizer"
          gutter=".gutter-sizer"
        >
          <template v-for="(item, index) in blocks" :key="item.id">
            <div
                 v-masonry-tile
                 :class="item.class"
                 class="grid"
                 :data-test="`board-order-card-${item.id}`"
                 @dragover.prevent
                 @drop.prevent="onBoardDrop($event, item.id)">
              <div class="grid-sizer"></div>
              <div class="gutter-sizer"></div>
              <div class="board-card grid-item">
                <div @mouseenter="setCurrentEditBoard(item.id)"
                     @mouseleave="setCurrentEditBoard(null)"
                >
                  <div class="card-image">
                    <BoardEditorUI
                      v-show="!ordering.editing && shouldShowEdit(item)"
                      :board="item"
                      v-on:board-delete-succeed="reset"
                      v-on:board-save-succeed="reset"
                    ></BoardEditorUI>
                    <router-link
                      v-if="!ordering.editing"
                      :to="{ name: 'board', params: { boardId: item.id } }"
                    >
                      <img :src="item.preview_image_url"
                         @load="onPinImageLoaded(item.id)"
                         :style="item.style"
                         :alt="item.name"
                         v-show="item.preview_image_url"
                         class="preview-image">
                    </router-link>
                    <div v-else class="board-order-static-image">
                      <img :src="item.preview_image_url"
                         @load="onPinImageLoaded(item.id)"
                         :style="item.style"
                         :alt="item.name"
                         v-show="item.preview_image_url"
                         class="preview-image">
                    </div>
                  </div>
                  <div class="board-footer">
                    <p class="sub-title board-info">{{ item.name }}</p>
                    <p class="description">
                      <small>
                        {{ $t("pinsInBoard") }}<span class="num-pins">{{ item.total_pins }}</span>
                      </small>
                    </p>
                  </div>
                  <div
                    v-if="ordering.editing"
                    class="board-order-card-controls"
                  >
                    <span class="board-order-card-controls__position">
                      {{ index + 1 }} / {{ blocks.length }}
                    </span>
                    <button
                      type="button"
                      class="button is-light board-order-card-controls__handle"
                      draggable="true"
                      data-order-control="handle"
                      :data-board-order-id="item.id"
                      :data-test="`board-order-handle-${item.id}`"
                      :aria-label="$t('boardOrderHandle', {
                        name: item.name,
                        position: index + 1,
                        total: blocks.length,
                      })"
                      :aria-grabbed="String(ordering.pickedBoardId === item.id)"
                      @dragstart="onBoardDragStart($event, item.id)"
                      @dragend="onBoardDragEnd"
                      @keydown="onBoardHandleKeydown($event, item.id)"
                    >
                      <span aria-hidden="true">☰</span>
                    </button>
                    <div class="buttons has-addons board-order-card-controls__moves">
                      <button
                        type="button"
                        class="button is-light"
                        data-order-control="previous"
                        :data-board-order-id="item.id"
                        :data-test="`board-order-previous-${item.id}`"
                        :disabled="ordering.loading"
                        :aria-disabled="index === 0"
                        @click="moveBoard(item.id, -1)"
                      >
                        {{ $t('boardOrderMoveForward') }}
                      </button>
                      <button
                        type="button"
                        class="button is-light"
                        data-order-control="next"
                        :data-board-order-id="item.id"
                        :data-test="`board-order-next-${item.id}`"
                        :disabled="ordering.loading"
                        :aria-disabled="index === blocks.length - 1"
                        @click="moveBoard(item.id, 1)"
                      >
                        {{ $t('boardOrderMoveBackward') }}
                      </button>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>
      <loadingSpinner v-bind:show="status.loading"></loadingSpinner>
      <noMore v-bind:show="!status.hasNext"></noMore>
    </section>
  </div>
</template>

<script>
import API from './api';
import pinHandler from './utils/PinHandler';
import loadingSpinner from './loadingSpinner.vue';
import noMore from './noMore.vue';
import scroll from './utils/scroll';
import placeholder from '../assets/pinry-placeholder.jpg';
import BoardEditorUI from './editors/BoardEditUI.vue';
import bus from './utils/bus';
import BoardSortControls from './board_ordering/BoardSortControls.vue';
import BoardOrderToolbar from './board_ordering/BoardOrderToolbar.vue';
import {
  boardSortStorageKey,
  generateRandomSeed,
  readBoardSortState,
  transitionBoardSortState,
  writeBoardSortState,
} from './board_ordering/boardSortState';

function getBoardSortStorage() {
  try {
    return window.localStorage;
  } catch (_error) {
    return null;
  }
}

function createBoardItem(board) {
  const defaultPreviewImage = placeholder;
  const boardItem = {};
  let previewImage = {
    image: { thumbnail: { image: null, width: 240, height: 240 } },
  };
  if (board.cover !== null) {
    previewImage = board.cover;
  }
  boardItem.id = board.id;
  boardItem.name = board.name;
  boardItem.private = board.private;
  boardItem.cover = board.cover;
  boardItem.cover_pin_id = board.cover_pin_id;
  boardItem.total_pins = board.total_pins;
  if (previewImage.image.thumbnail.image !== null) {
    boardItem.preview_image_url = pinHandler.escapeUrl(
      previewImage.image.thumbnail.image,
    );
  } else {
    boardItem.preview_image_url = defaultPreviewImage;
  }
  boardItem.style = {
    width: '100%',
    height: 'auto',
    aspectRatio: `${previewImage.image.thumbnail.width} / ${previewImage.image.thumbnail.height}`,
  };
  boardItem.class = {};
  boardItem.author = board.submitter.username;
  return boardItem;
}

function initialData() {
  return {
    currentEditBoard: null,
    blocks: [],
    blocksMap: {},
    status: {
      loading: false,
      hasNext: true,
      offset: 0,
    },
    editorMeta: {
      user: { loggedIn: false, meta: { username: null } },
      resolved: false,
    },
    sortState: { version: 1, mode: 'custom', randomSeed: 0 },
    sortStorageKey: null,
    ordering: {
      editing: false,
      loading: false,
      version: null,
      originalBoardIds: [],
      error: '',
      announcement: '',
      pickedBoardId: null,
      draggedBoardId: null,
    },
  };
}

export default {
  name: 'boards',
  beforeCreate() {
    this.requestGeneration = 0;
    this.orderRequestToken = 0;
    this.scrollDisposer = null;
    this.seedFactory = generateRandomSeed;
  },
  components: {
    loadingSpinner,
    noMore,
    BoardEditorUI,
    BoardSortControls,
    BoardOrderToolbar,
  },
  data: initialData,
  props: ['filters'],
  watch: {
    filters: {
      deep: true,
      handler() {
        this.activateSortContext();
        this.reset();
        this.$nextTick(() => window.scrollTo(0, 0));
      },
    },
  },
  computed: {
    isUserBoardList() {
      return Boolean(
        this.filters
        && this.filters.boardUsername
        && !this.filters.boardNameContains,
      );
    },
    canEnterBoardOrdering() {
      return this.isUserBoardList
        && this.editorMeta.resolved
        && this.editorMeta.user.loggedIn
        && this.editorMeta.user.meta.username === this.filters.boardUsername
        && this.sortState.mode === 'custom'
        && this.blocks.length >= 2
        && !this.status.loading
        && !this.ordering.editing;
    },
    isBoardOrderChanged() {
      if (!this.ordering.editing) return false;
      const currentIds = this.blocks.map(board => board.id);
      return currentIds.length === this.ordering.originalBoardIds.length
        && currentIds.some((id, index) => id !== this.ordering.originalBoardIds[index]);
    },
    boardToolsBusy() {
      return this.status.loading || this.ordering.loading || this.ordering.editing;
    },
  },
  methods: {
    captureFilters() {
      return {
        boardUsername: this.filters.boardUsername,
        boardNameContains: this.filters.boardNameContains,
      };
    },
    isRequestCurrent(generation, filters) {
      if (this.requestGeneration !== generation) return false;
      return this.filters.boardUsername === filters.boardUsername
        && this.filters.boardNameContains === filters.boardNameContains;
    },
    activateSortContext() {
      const sortStorageKey = boardSortStorageKey(this.filters.boardUsername);
      if (sortStorageKey === this.sortStorageKey) return;
      this.sortStorageKey = sortStorageKey;
      this.sortState = readBoardSortState(
        getBoardSortStorage(), this.sortStorageKey, this.seedFactory,
      );
    },
    applySortMode(mode) {
      if (!this.isUserBoardList || this.boardToolsBusy) return;
      const transition = transitionBoardSortState(this.sortState, mode, this.seedFactory);
      if (!transition.changed) return;
      this.sortState = transition.state;
      writeBoardSortState(getBoardSortStorage(), this.sortStorageKey, this.sortState);
      this.reset();
      this.$nextTick(() => window.scrollTo(0, 0));
    },
    initialize() {
      const generation = this.requestGeneration;
      const filters = this.captureFilters();
      this.initializeMeta(generation, filters);
      this.fetchMore(true, generation, filters);
    },
    initializeMeta(generation, filters) {
      const self = this;
      API.User.fetchUserInfo().then(
        (user) => {
          if (!self.isRequestCurrent(generation, filters)) return;
          if (user === null) {
            self.editorMeta.user.loggedIn = false;
            self.editorMeta.user.meta = {};
          } else {
            self.editorMeta.user.meta = user;
            self.editorMeta.user.loggedIn = true;
          }
          self.editorMeta.resolved = true;
        },
        () => {
          if (!self.isRequestCurrent(generation, filters)) return;
          self.editorMeta.user.loggedIn = false;
          self.editorMeta.user.meta = {};
          self.editorMeta.resolved = true;
        },
      );
    },
    reset() {
      this.requestGeneration += 1;
      this.orderRequestToken += 1;
      const sorting = {
        sortState: this.sortState,
        sortStorageKey: this.sortStorageKey,
      };
      const data = initialData();
      Object.entries(data).forEach(
        (kv) => {
          const [key, value] = kv;
          this[key] = value;
        },
      );
      Object.entries(sorting).forEach(
        (kv) => {
          const [key, value] = kv;
          this[key] = value;
        },
      );
      this.initialize();
    },
    shouldShowEdit(board) {
      if (this.ordering.editing) {
        return false;
      }
      if (!this.editorMeta.user.loggedIn) {
        return false;
      }
      if (this.editorMeta.user.meta.username !== board.author) {
        return false;
      }
      return this.currentEditBoard === board.id;
    },
    setCurrentEditBoard(boardId) {
      if (this.ordering.editing) return;
      this.currentEditBoard = boardId;
    },
    onPinImageLoaded(itemId) {
      this.blocksMap[itemId].class = {
        'image-loaded': true,
      };
      this.blocksMap[itemId].style.height = 'auto';
      if (this.ordering.editing) {
        this.redrawBoardMasonry();
      }
    },
    registerScrollEvent() {
      const self = this;
      this.scrollDisposer = scroll.bindScroll2Bottom(
        () => {
          if (self.status.loading || !self.status.hasNext
              || self.ordering.loading || self.ordering.editing) {
            return;
          }
          self.fetchMore();
        },
      );
    },
    buildBlocks(results) {
      const blocks = [];
      results.forEach(
        (pin) => {
          const item = createBoardItem(pin);
          blocks.push(
            item,
          );
        },
      );
      return blocks;
    },
    shouldFetchMore(created) {
      if (this.ordering.loading || this.ordering.editing) {
        return false;
      }
      if (!created) {
        if (this.status.loading) {
          return false;
        }
        if (!this.status.hasNext) {
          return false;
        }
      }
      return true;
    },
    fetchMore(
      created,
      generation = this.requestGeneration,
      filters = this.captureFilters(),
    ) {
      if (!this.isRequestCurrent(generation, filters)) return;
      if (!this.shouldFetchMore(created)) {
        return;
      }
      let promise;
      if (filters.boardUsername) {
        promise = API.fetchBoardForUser(
          filters.boardUsername,
          this.status.offset,
          50,
          this.sortState,
        );
      } else if (filters.boardNameContains) {
        promise = API.Board.fetchListWhichContains(
          filters.boardNameContains,
          this.status.offset,
        );
      } else {
        return;
      }
      this.status.loading = true;
      promise.then(
        (resp) => {
          if (!this.isRequestCurrent(generation, filters)) return;
          const { results, next } = resp.data;
          const consumed = results.length;
          const pageIds = new Set();
          const newBlocks = this.buildBlocks(results).filter((item) => {
            if (this.blocksMap[item.id] || pageIds.has(item.id)) return false;
            pageIds.add(item.id);
            return true;
          });
          newBlocks.forEach(
            (item) => { this.blocksMap[item.id] = item; },
          );
          this.blocks = this.blocks.concat(newBlocks);
          this.status.offset += consumed;
          this.status.hasNext = !(next === null);
          this.status.loading = false;
        },
        () => {
          if (!this.isRequestCurrent(generation, filters)) return;
          this.status.loading = false;
        },
      );
    },
    isOrderRequestCurrent(token, generation, filters) {
      return this.orderRequestToken === token
        && this.isRequestCurrent(generation, filters);
    },
    async loadRemainingBoardsForOrdering(token, generation, filters) {
      const blocks = this.blocks.slice();
      const blocksMap = { ...this.blocksMap };
      let { offset, hasNext } = this.status;

      while (hasNext) {
        // Board ID 집합의 일관성을 위해 페이지를 순차로 요청한다.
        // eslint-disable-next-line no-await-in-loop
        const resp = await API.fetchBoardForUser(
          filters.boardUsername,
          offset,
          50,
          this.sortState,
        );
        if (!this.isOrderRequestCurrent(token, generation, filters)) return null;
        const { results, next } = resp.data;
        if (results.length === 0 && next !== null) {
          throw new Error('Board pagination did not advance');
        }
        const pageIds = new Set();
        this.buildBlocks(results).forEach((item) => {
          if (blocksMap[item.id] || pageIds.has(item.id)) return;
          pageIds.add(item.id);
          blocksMap[item.id] = item;
          blocks.push(item);
        });
        offset += results.length;
        hasNext = next !== null;
      }

      return {
        blocks, blocksMap, offset, hasNext,
      };
    },
    hasExactBoardIds(blocks, boardIds) {
      if (!Array.isArray(boardIds) || blocks.length !== boardIds.length) return false;
      const loadedIds = new Set(blocks.map(board => board.id));
      return loadedIds.size === blocks.length
        && new Set(boardIds).size === boardIds.length
        && boardIds.every(boardId => loadedIds.has(boardId));
    },
    async enterBoardOrdering() {
      if (!this.canEnterBoardOrdering || this.ordering.loading) return;
      const token = this.orderRequestToken + 1;
      this.orderRequestToken = token;
      const generation = this.requestGeneration;
      const filters = this.captureFilters();
      this.ordering.loading = true;
      this.ordering.error = '';
      this.ordering.announcement = this.$t('boardOrderLoading');
      this.currentEditBoard = null;

      try {
        const snapshotResponse = await API.Board.fetchOrder();
        if (!this.isOrderRequestCurrent(token, generation, filters)) return;
        const loaded = await this.loadRemainingBoardsForOrdering(
          token, generation, filters,
        );
        if (!loaded || !this.isOrderRequestCurrent(token, generation, filters)) return;
        const snapshot = snapshotResponse.data;
        if (!this.hasExactBoardIds(loaded.blocks, snapshot.board_ids)) {
          this.ordering.loading = false;
          this.ordering.error = this.$t('boardOrderMismatch');
          this.ordering.announcement = '';
          return;
        }
        const blockById = new Map(loaded.blocks.map(board => [board.id, board]));
        this.blocks = snapshot.board_ids.map(boardId => blockById.get(boardId));
        this.blocksMap = loaded.blocksMap;
        this.status.offset = loaded.offset;
        this.status.hasNext = loaded.hasNext;
        this.ordering.version = snapshot.version;
        this.ordering.originalBoardIds = snapshot.board_ids.slice();
        this.ordering.pickedBoardId = null;
        this.ordering.draggedBoardId = null;
        this.ordering.editing = true;
        this.ordering.loading = false;
        this.ordering.error = '';
        this.ordering.announcement = '';
        this.redrawBoardMasonry();
      } catch (_error) {
        if (!this.isOrderRequestCurrent(token, generation, filters)) return;
        this.ordering.loading = false;
        this.ordering.error = this.$t('boardOrderLoadFailed');
        this.ordering.announcement = '';
      }
    },
    focusMovedBoard(boardId, control) {
      this.$nextTick(() => {
        this.redrawBoardMasonry();
        const selector = `[data-board-order-id="${boardId}"]`
          + `[data-order-control="${control}"]`;
        const target = this.$el.querySelector(selector)
          || this.$el.querySelector(`[data-test="board-order-handle-${boardId}"]`);
        if (target) target.focus();
      });
    },
    moveBoard(boardId, delta) {
      if (!this.ordering.editing || this.ordering.loading) return;
      const currentIndex = this.blocks.findIndex(board => board.id === boardId);
      if (currentIndex < 0) return;
      const targetIndex = Math.max(
        0,
        Math.min(this.blocks.length - 1, currentIndex + delta),
      );
      const { activeElement } = document;
      const activeControl = activeElement
        && activeElement.getAttribute('data-board-order-id') === String(boardId)
        ? activeElement.getAttribute('data-order-control') : 'handle';
      if (targetIndex !== currentIndex) {
        const reordered = this.blocks.slice();
        const [movedBoard] = reordered.splice(currentIndex, 1);
        reordered.splice(targetIndex, 0, movedBoard);
        this.blocks = reordered;
        this.ordering.error = '';
        this.ordering.announcement = this.$t('boardOrderMoved', {
          name: movedBoard.name,
          position: targetIndex + 1,
        });
      }
      this.focusMovedBoard(boardId, activeControl || 'handle');
    },
    onBoardDragStart(event, boardId) {
      if (!this.ordering.editing || this.ordering.loading) {
        event.preventDefault();
        return;
      }
      this.ordering.draggedBoardId = boardId;
      this.ordering.pickedBoardId = boardId;
      const { dataTransfer } = event;
      dataTransfer.effectAllowed = 'move';
      dataTransfer.setData('text/plain', String(boardId));
    },
    onBoardDrop(event, targetBoardId) {
      if (!this.ordering.editing || this.ordering.loading) return;
      const sourceBoardId = this.ordering.draggedBoardId;
      const sourceIndex = this.blocks.findIndex(board => board.id === sourceBoardId);
      const targetIndex = this.blocks.findIndex(board => board.id === targetBoardId);
      if (sourceIndex >= 0 && targetIndex >= 0) {
        this.moveBoard(sourceBoardId, targetIndex - sourceIndex);
      }
      this.onBoardDragEnd();
    },
    onBoardDragEnd() {
      this.ordering.draggedBoardId = null;
      this.ordering.pickedBoardId = null;
    },
    onBoardHandleKeydown(event, boardId) {
      const isActivation = event.key === ' '
        || event.key === 'Spacebar'
        || event.key === 'Enter';
      if (isActivation) {
        event.preventDefault();
        const board = this.blocks.find(item => item.id === boardId);
        if (this.ordering.pickedBoardId === boardId) {
          this.ordering.pickedBoardId = null;
          this.ordering.announcement = this.$t('boardOrderReleased', { name: board.name });
        } else {
          this.ordering.pickedBoardId = boardId;
          this.ordering.announcement = this.$t('boardOrderPicked', { name: board.name });
        }
        return;
      }
      if (event.key === 'Escape' && this.ordering.pickedBoardId === boardId) {
        event.preventDefault();
        this.ordering.pickedBoardId = null;
        const board = this.blocks.find(item => item.id === boardId);
        this.ordering.announcement = this.$t('boardOrderReleased', { name: board.name });
        return;
      }
      if (this.ordering.pickedBoardId !== boardId) return;
      const moves = {
        ArrowLeft: -1,
        ArrowUp: -1,
        ArrowRight: 1,
        ArrowDown: 1,
      };
      if (moves[event.key]) {
        event.preventDefault();
        this.moveBoard(boardId, moves[event.key]);
      }
    },
    redrawBoardMasonry() {
      this.$nextTick(() => {
        if (typeof this.$redrawVueMasonry === 'function') {
          this.$redrawVueMasonry();
        }
      });
    },
    restoreBoardOrder(boardIds) {
      const blockById = new Map(this.blocks.map(board => [board.id, board]));
      this.blocks = boardIds.map(boardId => blockById.get(boardId)).filter(Boolean);
    },
    focusBoardOrderEnter() {
      this.$nextTick(() => {
        const toolbar = this.$refs.boardOrderToolbar;
        if (toolbar) toolbar.focusEnter();
      });
    },
    cancelBoardOrdering() {
      if (!this.ordering.editing || this.ordering.loading) return;
      this.restoreBoardOrder(this.ordering.originalBoardIds);
      this.ordering.editing = false;
      this.ordering.pickedBoardId = null;
      this.ordering.draggedBoardId = null;
      this.ordering.error = '';
      this.ordering.announcement = this.$t('boardOrderCancelled');
      this.focusBoardOrderEnter();
      this.redrawBoardMasonry();
    },
    async saveBoardOrdering() {
      if (!this.ordering.editing || this.ordering.loading || !this.isBoardOrderChanged) return;
      const token = this.orderRequestToken + 1;
      this.orderRequestToken = token;
      const generation = this.requestGeneration;
      const filters = this.captureFilters();
      const boardIds = this.blocks.map(board => board.id);
      const { version } = this.ordering;
      this.ordering.loading = true;
      this.ordering.error = '';

      try {
        const response = await API.Board.saveOrder(version, boardIds);
        if (!this.isOrderRequestCurrent(token, generation, filters)) return;
        const snapshot = response.data;
        this.ordering.version = snapshot.version;
        this.ordering.originalBoardIds = snapshot.board_ids.slice();
        this.restoreBoardOrder(snapshot.board_ids);
        this.ordering.editing = false;
        this.ordering.loading = false;
        this.ordering.pickedBoardId = null;
        this.ordering.draggedBoardId = null;
        this.ordering.announcement = this.$t('boardOrderSaved');
        this.focusBoardOrderEnter();
        this.redrawBoardMasonry();
      } catch (error) {
        if (!this.isOrderRequestCurrent(token, generation, filters)) return;
        this.ordering.loading = false;
        if (error && error.response && error.response.status === 409) {
          this.ordering.error = this.$t('boardOrderRefreshRequired');
        } else {
          this.ordering.error = this.$t('boardOrderSaveFailed');
        }
      }
    },
  },
  created() {
    bus.bus.on(bus.events.refreshBoards, this.reset);
    this.registerScrollEvent();
    this.activateSortContext();
    this.initialize();
  },
  beforeUnmount() {
    this.requestGeneration += 1;
    this.orderRequestToken += 1;
    bus.bus.off(bus.events.refreshBoards, this.reset);
    if (typeof this.scrollDisposer === 'function') this.scrollDisposer();
    this.scrollDisposer = null;
  },
};
</script>

<style lang="scss" scoped>
/* grid */
@import 'utils/pin';

.grid-sizer,
.grid-item { width: $pin-preview-width; }
.grid-item {
  margin-bottom: 20px;
}
.gutter-sizer {
  width: 20px;
}

/* card */
$avatar-width: 30px;
$avatar-height: 30px;
@import './utils/fonts';
@import './utils/loader.scss';

.board-tools {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: .75rem 1rem;
  margin-bottom: 1rem;
}

.board-card{
  overflow: hidden;
  border: 1px solid var(--pinry-border);
  border-radius: 8px;
  .preview-image { display: block; }
  .card-image > img {
    min-width: $pin-preview-width;
    background-color: var(--pinry-surface);
    border-radius: 3px 3px 0 0;
    @include loader('../assets/loader.gif');
  }
}
.board-footer {
  position: relative;
  top: 0;
  background-color: var(--pinry-surface);
  border-radius: 0 0 3px 3px ;
  box-shadow: none;
  font-weight: bold;
  .description {
    @include secondary-font;
    padding-left: 10px;
    padding-bottom: 5px;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .board-info {
    padding: 10px;
    color: var(--pinry-text);
  }
  .num-pins {
    font-size: 0.8rem;
    color: var(--pinry-text);
  }
}

.board-order-static-image { cursor: default; }

.board-order-card-controls {
  display: grid;
  grid-template-columns: auto auto 1fr;
  align-items: center;
  gap: .45rem;
  padding: .55rem;
  border-top: 1px solid var(--pinry-border);
  border-radius: 0 0 4px 4px;
  background: var(--pinry-surface);
  box-shadow: 0 1px 0 #bbb;
}

.board-order-card-controls__position {
  min-width: 2.8rem;
  color: var(--pinry-muted);
  font-size: .75rem;
  font-variant-numeric: tabular-nums;
  text-align: center;
}

.board-order-card-controls__handle {
  min-width: 2.25rem;
  cursor: grab;
  touch-action: none;
}

.board-order-card-controls__handle:active { cursor: grabbing; }
.board-order-card-controls__moves { justify-self: end; margin-bottom: 0; }
.board-order-card-controls__moves .button { margin-bottom: 0; }

@media screen and (max-width: 768px) {
  .board-order-card-controls { grid-template-columns: auto 1fr; }
  .board-order-card-controls__handle { display: none; }
  .board-order-card-controls__moves {
    display: flex;
    justify-self: stretch;
  }
  .board-order-card-controls__moves .button { flex: 1; }
}

@import 'utils/grid-layout';
@include screen-grid-layout("#boards-container, .board-tools");

@media screen and (max-width: 543px) {
  .grid-item, .grid-sizer { width: 100%; }
  #boards-container, .board-tools { max-width: 360px; }
}

</style>
