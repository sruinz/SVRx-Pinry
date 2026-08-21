<template>
  <div class="pins">
    <section class="section">
      <PinBulkToolbar
        v-if="canManagePins"
        :active="selection.active"
        :selected-count="selection.selectedIds.length"
        :loaded-count="blocks.length"
        :scope="selection.scope"
        :all-count="selection.allCount"
        :can-modify="canModifySelection"
        :operation-in-flight="selection.operationInFlight"
        :announcement="selectionAnnouncement"
        @enter="enterSelection"
        @exit="exitSelection"
        @select-loaded="selectLoadedPins"
        @clear="clearPinSelection"
        @select-all="selectAllPins"
      />
      <div id="pins-container" class="container" v-if="blocks">
        <div
          v-masonry=""
          transition-duration="0.3s"
          item-selector=".grid-item"
          column-width=".grid-sizer"
          gutter=".gutter-sizer"
        >
          <template v-for="item in blocks">
            <div v-bind:key="item.id"
                 v-masonry-tile
                 :class="item.class"
                 class="grid pin-masonry">
              <div class="grid-sizer"></div>
              <div class="gutter-sizer"></div>
              <div
                class="pin-card grid-item"
                :class="{ 'is-selected': isPinSelected(item.id) }"
                :data-test="`pin-card-${item.id}`"
                :role="selection.active ? 'button' : null"
                :tabindex="selection.active ? 0 : null"
                :aria-selected="selection.active ? String(isPinSelected(item.id)) : null"
                @click="onPinCardClick(item, $event)"
                @keydown="onPinCardKeydown(item, $event)"
              >
                <div @mouseenter="showEditButtons(item.id)"
                     @mouseleave="hideEditButtons(item.id)"
                >
                  <EditorUI
                    v-show="!selection.active && shouldShowEdit(item.id)"
                    :pin="item"
                    :currentUsername="editorMeta.user.meta.username"
                    :currentBoard="editorMeta.currentBoard"
                    v-on:pin-delete-succeed="reset"
                    v-on:pin-remove-from-board-succeed="reset"
                  ></EditorUI>
                  <input
                    v-if="selection.active"
                    type="checkbox"
                    class="pin-selection-check"
                    :data-test="`pin-selection-check-${item.id}`"
                    :checked="isPinSelected(item.id)"
                    :aria-label="$t('bulkPinSelectOne')"
                    @click.stop="togglePinSelection(item.id, $event)"
                  >
                  <img :src="item.url"
                     @load="onPinImageLoaded(item.id)"
                     @click.stop="onPinImageClick(item, $event)"
                     :alt="item.description"
                     :style="item.style"
                     :data-test="`pin-image-${item.id}`"
                     class="pin-preview-image">
                </div>
                <div class="pin-footer">
                  <div class="description" v-show="item.description" v-html="niceLinks(item.description)"></div>
                  <div class="details">
                    <div class="is-pulled-left">
                      <img class="avatar" :src="item.avatar" alt="">
                    </div>
                    <div class="pin-info">
                      <span class="dim">{{ $t("pinnedByInfo") }}&nbsp;
                        <span>
                          <router-link
                            :to="{ name: 'user', params: {user: item.author} }">
                            {{ item.author }}
                          </router-link>
                        </span>
                        <template v-if="item.tags.length > 0">
                          &nbsp;in&nbsp;
                          <template v-for="tag in item.tags">
                            <span v-bind:key="tag" class="pin-tag">
                              <router-link :to="{ name: 'tag', params: {tag: tag} }"
                                           params="{tag: tag}">{{ tag }}</router-link>
                            </span>
                          </template>
                        </template>
                        <span v-if="item.referer">• <a :href="item.referer" target="_blank">{{ $t("sourceLink") }}</a></span>
                      </span>
                    </div>
                    <div class="is-clearfix"></div>
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
import PinPreview from './PinPreview.vue';
import loadingSpinner from './loadingSpinner.vue';
import noMore from './noMore.vue';
import scroll from './utils/scroll';
import bus from './utils/bus';
import EditorUI from './editors/PinEditorUI.vue';
import niceLinks from './utils/niceLinks';
import PinBulkToolbar from './bulk/PinBulkToolbar.vue';
import PinSelection from './bulk/PinSelection';

function createImageItem(pin) {
  const image = {};
  image.url = pinHandler.escapeUrl(pin.image.thumbnail.image);
  image.id = pin.id;
  image.owner_id = pin.submitter.id;
  image.private = pin.private;
  image.description = pin.description;
  image.tags = pin.tags;
  image.author = pin.submitter.username;
  image.avatar = `//gravatar.com/avatar/${pin.submitter.gravatar}`;
  image.large_image_url = pinHandler.escapeUrl(pin.image.image);
  image.original_image_url = pin.url;
  image.referer = pin.referer;
  image.orgianl_width = pin.image.width;
  image.style = {
    width: `${pin.image.thumbnail.width}px`,
    height: `${pin.image.thumbnail.height}px`,
  };
  image.class = {};
  return image;
}

function initialData() {
  return {
    blocks: [],
    blocksMap: {},
    status: {
      loading: false,
      hasNext: true,
      offset: 0,
    },
    editorMeta: {
      currentEditId: null,
      currentBoard: {},
      user: {
        loggedIn: false,
        meta: {},
      },
    },
    metaReady: {
      user: false,
      board: false,
    },
    selection: {
      active: false,
      selectedIds: [],
      anchorId: null,
      scope: 'loaded',
      allCount: 0,
      ownedCount: 0,
      ownershipById: {},
      operationInFlight: false,
      progress: null,
      result: null,
    },
  };
}

export default {
  name: 'pins',
  beforeCreate() {
    this.selectionModel = new PinSelection();
  },
  components: {
    loadingSpinner,
    noMore,
    EditorUI,
    PinBulkToolbar,
  },
  data() {
    return initialData();
  },
  props: {
    pinFilters: {
      type: Object,
      default() {
        return {
          tagFilter: null,
          userFilter: null,
          boardFilter: null,
        };
      },
    },
  },
  watch: {
    pinFilters() {
      this.reset();
    },
  },
  computed: {
    canManagePins() {
      if (!this.metaReady.user || !this.editorMeta.user.loggedIn) return false;
      const { username } = this.editorMeta.user.meta;
      if (this.pinFilters.userFilter) {
        return this.pinFilters.userFilter === username;
      }
      const { submitter } = this.editorMeta.currentBoard;
      return Boolean(
        this.pinFilters.boardFilter
        && this.metaReady.board
        && submitter
        && submitter.username === username,
      );
    },
    hasNonOwnedSelection() {
      return this.selection.selectedIds.some(
        id => this.selection.ownershipById[id] !== true,
      );
    },
    canModifySelection() {
      return this.selection.selectedIds.length > 0 && !this.hasNonOwnedSelection;
    },
    selectionAnnouncement() {
      if (this.selection.result && this.selection.result.code === 'selection_too_large') {
        return this.$t('bulkPinSelectionTooLarge');
      }
      if (this.selection.scope === 'all') {
        return this.$t('bulkPinAllSelected', { count: this.selection.allCount });
      }
      return this.$t('bulkPinSelectedCount', {
        count: this.selection.selectedIds.length,
      });
    },
  },
  methods: {
    updateSelection(snapshot, extra = {}) {
      const ownedCount = snapshot.selectedIds.filter(
        id => snapshot.ownershipById[id] === true,
      ).length;
      this.selection = {
        ...this.selection,
        ...snapshot,
        ownedCount,
        ...extra,
      };
    },
    syncLoadedSelection() {
      const username = this.metaReady.user && this.editorMeta.user.loggedIn
        ? this.editorMeta.user.meta.username
        : null;
      const rows = this.blocks.map(item => ({
        id: item.id,
        owned: username !== null && item.author === username,
      }));
      this.updateSelection(this.selectionModel.setLoadedRows(rows));
    },
    enterSelection() {
      if (!this.canManagePins) return;
      this.updateSelection(this.selectionModel.selectLoaded([]), {
        active: true,
        allCount: 0,
        result: null,
      });
    },
    exitSelection() {
      this.updateSelection(this.selectionModel.selectLoaded([]), {
        active: false,
        allCount: 0,
        result: null,
      });
    },
    clearPinSelection() {
      this.updateSelection(this.selectionModel.selectLoaded([]), {
        allCount: 0,
        result: null,
      });
    },
    selectLoadedPins() {
      this.updateSelection(
        this.selectionModel.selectLoaded(this.blocks.map(item => item.id)),
        { allCount: 0, result: null },
      );
    },
    selectAllPins() {
      if (this.selection.operationInFlight) return;
      this.selection.operationInFlight = true;
      this.selection.result = null;
      API.Pin.fetchSelectionIds({
        boardId: this.pinFilters.boardFilter || null,
      }).then(
        (response) => {
          const { count, results } = response.data;
          if (!Number.isInteger(count) || !Array.isArray(results) || count !== results.length) {
            throw new Error('invalid_selection_response');
          }
          const snapshot = this.selectionModel.applyScope(results);
          if (snapshot.scope !== 'all') throw new Error('invalid_selection_response');
          this.updateSelection(snapshot, { allCount: count, result: null });
        },
        (error) => {
          const code = error && error.response && error.response.data
            ? error.response.data.code
            : 'selection_failed';
          this.selection.result = { code };
        },
      ).catch(() => {
        this.selection.result = { code: 'selection_failed' };
      }).then(() => {
        this.selection.operationInFlight = false;
      });
    },
    isPinSelected(id) {
      return this.selection.selectedIds.includes(id);
    },
    togglePinSelection(id, event) {
      if (!this.selection.active) return;
      if (event) event.preventDefault();
      this.updateSelection(this.selectionModel.toggle(id, {
        shiftKey: Boolean(event && event.shiftKey),
        ctrlKey: Boolean(event && event.ctrlKey),
        metaKey: Boolean(event && event.metaKey),
      }), { result: null });
    },
    onPinCardClick(item, event) {
      if (!this.selection.active) return;
      this.togglePinSelection(item.id, event);
    },
    onPinImageClick(item, event) {
      if (this.selection.active) {
        this.togglePinSelection(item.id, event);
        return;
      }
      this.openPreview(item);
    },
    onPinCardKeydown(item, event) {
      if (!this.selection.active || (event.key !== 'Enter' && event.key !== ' ')) return;
      event.preventDefault();
      this.togglePinSelection(item.id, event);
    },
    isEditableTarget(target) {
      if (!target || target === document) return false;
      const tagName = target.tagName ? target.tagName.toLowerCase() : '';
      if (['input', 'textarea', 'select'].includes(tagName)) return true;
      if (target.isContentEditable) return true;
      return Boolean(
        target.closest
        && target.closest('[contenteditable]:not([contenteditable="false"])'),
      );
    },
    onDocumentKeydown(event) {
      if (!this.selection.active) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        this.exitSelection();
        return;
      }
      const selectAll = (event.ctrlKey || event.metaKey)
        && typeof event.key === 'string'
        && event.key.toLowerCase() === 'a';
      if (
        !selectAll
        || event.isComposing
        || event.keyCode === 229
        || this.isEditableTarget(event.target)
      ) return;
      event.preventDefault();
      this.selectLoadedPins();
    },
    shouldShowEdit(id) {
      if (!this.editorMeta.user.loggedIn) {
        return false;
      }
      return this.editorMeta.currentEditId === id;
    },
    showEditButtons(id) {
      this.editorMeta.currentEditId = id;
    },
    hideEditButtons() {
      this.editorMeta.currentEditId = null;
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
          const item = createImageItem(pin);
          blocks.push(
            item,
          );
        },
      );
      return blocks;
    },
    openPreview(pinItem) {
      this.$buefy.modal.open(
        {
          parent: this,
          component: PinPreview,
          props: {
            pinItem,
          },
          scroll: 'keep',
          customClass: 'pin-preview-at-home',
        },
      );
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
    initialize() {
      this.initializeMeta();
      this.fetchMore(true);
    },
    initializeMeta() {
      const self = this;
      API.User.fetchUserInfo().then(
        (user) => {
          if (user === null) {
            self.editorMeta.user.loggedIn = false;
            self.editorMeta.user.meta = {};
          } else {
            self.editorMeta.user.meta = user;
            self.editorMeta.user.loggedIn = true;
          }
          self.metaReady.user = true;
          self.syncLoadedSelection();
        },
        () => {
          self.editorMeta.user.loggedIn = false;
          self.editorMeta.user.meta = {};
          self.metaReady.user = true;
          self.syncLoadedSelection();
        },
      );
    },
    reset() {
      const data = initialData();
      this.selectionModel = new PinSelection();
      Object.entries(data).forEach(
        (kv) => {
          const [key, value] = kv;
          this[key] = value;
        },
      );
      this.initialize();
    },
    fetchMore(created) {
      if (!this.shouldFetchMore(created)) {
        return;
      }
      this.status.loading = true;
      let promise;
      if (this.pinFilters.tagFilter) {
        promise = API.fetchPins(this.status.offset, this.pinFilters.tagFilter, null, null);
      } else if (this.pinFilters.userFilter) {
        promise = API.fetchPins(this.status.offset, null, this.pinFilters.userFilter, null);
      } else if (this.pinFilters.boardFilter) {
        const prevPromise = API.Board.get(this.pinFilters.boardFilter);
        promise = prevPromise.then(
          (resp) => {
            this.editorMeta.currentBoard = resp.data;
            this.metaReady.board = true;
            return API.fetchPins(this.status.offset, null, null, this.pinFilters.boardFilter);
          },
        );
      } else if (this.pinFilters.idFilter) {
        promise = API.fetchPin(this.pinFilters.idFilter);
      } else {
        promise = API.fetchPins(this.status.offset);
      }
      promise.then(
        (resp) => {
          const { results, next } = resp.data;
          let newBlocks = this.buildBlocks(results);
          newBlocks.forEach(
            (item) => { this.blocksMap[item.id] = item; },
          );
          newBlocks = this.blocks.concat(newBlocks);
          this.blocks = newBlocks;
          this.syncLoadedSelection();
          this.status.offset = newBlocks.length;
          this.status.hasNext = !(next === null);
          this.status.loading = false;
        },
        () => { this.status.loading = false; },
      );
    },
    niceLinks,
  },
  created() {
    bus.bus.$on(bus.events.refreshPin, this.reset);
    this.registerScrollEvent();
    document.addEventListener('keydown', this.onDocumentKeydown);
    this.initialize();
  },
  beforeDestroy() {
    document.removeEventListener('keydown', this.onDocumentKeydown);
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

/* pin-image transition */
.pin-masonry.image-loaded{
  opacity: 1;
  transition: opacity .3s;
}
.pin-masonry {
  opacity: 0;
}

/* card */
$pin-footer-position-fix: -6px;
$avatar-width: 30px;
$avatar-height: 30px;
@import './utils/fonts';
@import './utils/loader.scss';

.pin-card{
  position: relative;

  &.is-selected {
    outline: 3px solid #3273dc;
    outline-offset: 2px;
  }

  .pin-selection-check {
    position: absolute;
    z-index: 2;
    top: .5rem;
    left: .5rem;
  }

  .pin-preview-image {
    cursor: zoom-in;
  }
  > img {
    min-width: $pin-preview-width;
    background-color: white;
    border-radius: 3px 3px 0 0;
    @include loader('../assets/loader.gif');
  }
  .avatar {
    height: $avatar-height;
    width: $avatar-width;
    border-radius: 3px;
  }
  .pin-tag {
    margin-right: 0.2rem;
  }
}
.pin-footer {
  position: relative;
  overflow-wrap: break-word;
  top: $pin-footer-position-fix;
  background-color: white;
  border-radius: 0 0 3px 3px ;
  box-shadow: 0 1px 0 #bbb;
  .description {
    @include description-font;
    padding: 8px;
    border-bottom: 1px solid #DDDDDD;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .details {
    @include secondary-font;
    padding: 10px;
    > .pin-info {
      line-height: 16px;
      width: 220px;
      padding-left: $avatar-width + 5px;
    }
    .pin-info a {
      font-weight: bold;
    }
  }
}

@import 'utils/grid-layout';
@include screen-grid-layout("#pins-container")

</style>
