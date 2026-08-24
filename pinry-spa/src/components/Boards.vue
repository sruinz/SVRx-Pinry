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
          :busy="status.loading"
          @select="applySortMode"
        />
      </div>
      <div id="boards-container" class="container" v-if="blocks">
        <div
          v-masonry=""          transition-duration="0.3s"
          item-selector=".grid-item"
          column-width=".grid-sizer"
          gutter=".gutter-sizer"
        >
          <template v-for="item in blocks">
            <div v-bind:key="item.id"
                 v-masonry-tile
                 :class="item.class"
                 class="grid">
              <div class="grid-sizer"></div>
              <div class="gutter-sizer"></div>
              <div class="board-card grid-item">
                <div @mouseenter="currentEditBoard = item.id"
                     @mouseleave="currentEditBoard = null"
                >
                  <div class="card-image">
                    <BoardEditorUI
                      v-show="shouldShowEdit(item)"
                      :board="item"
                      v-on:board-delete-succeed="reset"
                      v-on:board-save-succeed="reset"
                    ></BoardEditorUI>
                    <router-link :to="{ name: 'board', params: { boardId: item.id } }">
                      <img :src="item.preview_image_url"
                         @load="onPinImageLoaded(item.id)"
                         :style="item.style"
                         v-show="item.preview_image_url"
                         class="preview-image">
                    </router-link>
                  </div>
                  <div class="board-footer">
                    <p class="sub-title board-info">{{ item.name }}</p>
                    <p class="description">
                      <small>
                        {{ $t("pinsInBoard") }}<span class="num-pins">{{ item.total_pins }}</span>
                      </small>
                    </p>
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
    width: `${previewImage.image.thumbnail.width}px`,
    height: `${previewImage.image.thumbnail.height}px`,
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
    },
    sortState: { version: 1, mode: 'custom', randomSeed: 0 },
    sortStorageKey: null,
  };
}

export default {
  name: 'boards',
  beforeCreate() {
    this.requestGeneration = 0;
    this.seedFactory = generateRandomSeed;
  },
  components: {
    loadingSpinner,
    noMore,
    BoardEditorUI,
    BoardSortControls,
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
      return Boolean(this.filters && this.filters.boardUsername);
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
      if (!this.isUserBoardList || this.status.loading) return;
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
        },
      );
    },
    reset() {
      this.requestGeneration += 1;
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
      if (!this.editorMeta.user.loggedIn) {
        return false;
      }
      if (this.editorMeta.user.meta.username !== board.author) {
        return false;
      }
      return this.currentEditBoard === board.id;
    },
    onPinImageLoaded(itemId) {
      this.blocksMap[itemId].class = {
        'image-loaded': true,
      };
      this.blocksMap[itemId].style.height = 'auto';
    },
    registerScrollEvent() {
      const self = this;
      scroll.bindScroll2Bottom(
        () => {
          if (self.status.loading || !self.status.hasNext) {
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
  },
  created() {
    bus.bus.$on(bus.events.refreshBoards, this.reset);
    this.registerScrollEvent();
    this.activateSortContext();
    this.initialize();
  },
};
</script>

<style lang="scss" scoped>
/* grid */
@import 'utils/pin';

.grid-sizer,
.grid-item { width: $pin-preview-width; }
.grid-item {
  margin-bottom: 15px;
}
.gutter-sizer {
  width: 15px;
}

/* card */
$pin-footer-position-fix: -6px;
$avatar-width: 30px;
$avatar-height: 30px;
@import './utils/fonts';
@import './utils/loader.scss';

.board-tools {
  margin-bottom: 1rem;
}

.board-card{
  .card-image > img {
    min-width: $pin-preview-width;
    background-color: white;
    border-radius: 3px 3px 0 0;
    @include loader('../assets/loader.gif');
  }
}
.board-footer {
  position: relative;
  top: $pin-footer-position-fix;
  background-color: white;
  border-radius: 0 0 3px 3px ;
  box-shadow: 0 1px 0 #bbb;
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
    color: $main-title-font-color;
  }
  .num-pins {
    font-size: 0.8rem;
    color: $main-title-font-color;
  }
}

@import 'utils/grid-layout';
@include screen-grid-layout("#boards-container")

</style>
