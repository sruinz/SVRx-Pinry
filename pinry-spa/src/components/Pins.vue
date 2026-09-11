<template>
  <div class="pins">
    <section class="section">
      <div
        v-if="!pinFilters.idFilter"
        class="container pin-tools"
        data-test="pin-tools"
      >
        <div class="pin-tools__primary" data-test="pin-tools-primary">
          <PinSortControls
            :mode="sortState.mode"
            :disabled="interactionMode !== 'browse'
              || selection.active
              || selection.operationInFlight"
            :busy="status.loading"
            :announcement="sortAnnouncement"
            @select="applySortMode"
          />
          <div v-if="canSelectPins" class="pin-tools__management">
            <PinBulkToolbar
              :active="false"
              :selected-count="selection.selectedIds.length"
              :loaded-count="blocks.length"
              :scope="selection.scope"
              :all-count="selection.allCount"
              :show-add-to-board="isMyPinsRoute"
              :can-add-to-board="canUseOwnedPinActions"
              :show-move="isOwnedBoardRoute"
              :can-move="isOwnedBoardRoute"
              :show-edit="canManagePins"
              :can-edit="canUseOwnedPinActions"
              :show-export="true"
              :can-export="selection.selectedIds.length > 0"
              :show-delete="canManagePins"
              :can-delete="canUseOwnedPinActions"
              :can-select-all="false"
              :operation-in-flight="selection.operationInFlight"
              :enter-disabled="interactionMode !== 'browse' || coverSelection.inFlight"
              :announcement="selectionAnnouncement"
              @enter="enterSelection"
            />
            <button
              v-if="isOwnedBoardRoute"
              type="button"
              class="button"
              data-test="board-export"
              :disabled="interactionMode !== 'browse'
                || coverSelection.inFlight
                || selection.operationInFlight"
              @click="openBoardExport"
            >
              {{ $t('exportBoard') }}
            </button>
            <BoardCoverToolbar
              v-if="isOwnedBoardRoute"
              ref="boardCoverToolbar"
              :active="false"
              :selected-id="coverSelection.candidateId"
              :current-cover-id="currentBoardCoverId"
              :busy="coverSelection.inFlight"
              :can-reset="currentBoardCoverId !== null"
              :disabled="interactionMode !== 'browse'"
              @enter="enterCoverSelection"
            />
          </div>
        </div>
        <div
          v-if="(selection.active && canSelectPins)
            || (interactionMode === 'cover-selection' && isOwnedBoardRoute)"
          class="pin-tools__active"
          data-test="pin-tools-active"
        >
          <PinBulkToolbar
            v-if="selection.active && canSelectPins"
            :active="true"
            :selected-count="selection.selectedIds.length"
            :loaded-count="blocks.length"
            :scope="selection.scope"
            :all-count="selection.allCount"
            :show-add-to-board="isMyPinsRoute"
            :can-add-to-board="canUseOwnedPinActions"
            :show-move="isOwnedBoardRoute"
            :can-move="isOwnedBoardRoute"
            :show-edit="canManagePins"
            :can-edit="canUseOwnedPinActions"
            :show-export="true"
            :can-export="selection.selectedIds.length > 0"
            :show-delete="canManagePins"
            :can-delete="canUseOwnedPinActions"
            :can-select-all="false"
            :operation-in-flight="selection.operationInFlight"
            :announcement="selectionAnnouncement"
            @exit="exitSelection"
            @select-loaded="selectLoadedPins"
            @clear="clearPinSelection"
            @select-all="selectAllPins"
            @add-to-board="openBulkBoard('add')"
            @move="openBulkBoard('move')"
            @edit="openBulkEdit"
            @export="openSelectedExport"
            @delete="confirmBulkDelete"
          />
          <template v-else-if="interactionMode === 'cover-selection' && isOwnedBoardRoute">
            <p class="pin-tools__status" data-test="board-cover-status">
              {{ $t('boardCoverEnter') }} ·
              {{ $t('bulkPinSelectedCount', {
                count: coverSelection.candidateId === null ? 0 : 1,
              }) }}
            </p>
            <BoardCoverToolbar
              :active="true"
              :selected-id="coverSelection.candidateId"
              :current-cover-id="currentBoardCoverId"
              :busy="coverSelection.inFlight"
              :can-reset="currentBoardCoverId !== null"
              :disabled="interactionMode !== 'browse'"
              :error="coverSelection.error"
              @apply="applyCoverPin(coverSelection.candidateId)"
              @cancel="cancelCoverSelection"
              @reset="confirmCoverReset"
            />
          </template>
        </div>
      </div>
      <div
        v-if="selection.result && selection.result.code === 'selection_too_large'"
        class="notification is-warning"
        data-test="pin-selection-too-large"
        role="alert"
      >
        {{ $t('bulkPinSelectionTooLarge') }}
      </div>
      <div
        v-if="selection.progress"
        class="notification is-info"
        data-test="pin-bulk-progress"
        aria-live="polite"
      >
        {{ $t('bulkPinProgress', selection.progress) }}
      </div>
      <div
        v-if="selection.result && Number.isInteger(selection.result.total)"
        class="notification"
        data-test="pin-bulk-result"
      >
        <span>{{ $t('bulkPinResultSucceeded', { count: selection.result.succeeded }) }}</span>
        <span>{{ $t('bulkPinResultPreserved', { count: selection.result.preserved }) }}</span>
        <span>{{ $t('bulkPinResultFailed', { count: selection.result.failed }) }}</span>
        <button
          v-if="selection.result.retryIds && selection.result.retryIds.length > 0"
          type="button"
          class="button"
          data-test="pin-bulk-retry"
          :disabled="selection.operationInFlight"
          @click="retryBulkDelete"
        >
          {{ $t('bulkPinRetry') }}
        </button>
      </div>
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
                :class="{
                  'is-selected': isPinSelected(item.id) || isCoverCandidate(item.id),
                  'is-cover-disabled': isCoverCandidateDisabled(item),
                }"
                :data-test="`pin-card-${item.id}`"
                :role="interactionMode !== 'browse' ? 'button' : null"
                :tabindex="interactionMode !== 'browse' ? 0 : null"
                :aria-selected="selection.active ? String(isPinSelected(item.id)) : null"
                :aria-pressed="interactionMode === 'cover-selection'
                  ? String(isCoverCandidate(item.id)) : null"
                :aria-disabled="isCoverCandidateDisabled(item) ? 'true' : null"
                :aria-label="isCoverCandidateDisabled(item)
                  ? $t('boardCoverPrivateUnavailable') : null"
                @click.capture="onPinCardClickCapture($event)"
                @click="onPinCardClick(item, $event)"
                @keydown="onPinCardKeydown(item, $event)"
              >
                <div class="pin-image-container" @mouseenter="showEditButtons(item.id)"
                     @mouseleave="hideEditButtons(item.id)"
                >
                  <EditorUI
                    v-show="interactionMode === 'browse' && shouldShowEdit(item.id)"
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
                  <span
                    v-if="interactionMode === 'cover-selection' && isCoverCandidate(item.id)"
                    class="pin-selection-check"
                    aria-hidden="true"
                  >✓</span>
                  <span v-if="item.is_gif" class="pin-gif-badge">GIF</span>
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
import BoardCoverToolbar from './board_cover/BoardCoverToolbar.vue';
import PinSelection from './bulk/PinSelection';
import { executeBulk, intersectRemaining } from './bulk/bulkExecutor';
import { openExport, openPinBulkBoard, openPinBulkEdit } from './modals';
import PinSortControls from './sorting/PinSortControls.vue';
import {
  generateRandomSeed,
  pinSortStorageKey,
  readPinSortState,
  transitionPinSortState,
  writePinSortState,
} from './sorting/pinSortState';

const MAX_SELECTION_IDS = 50000;
const SELECTION_FIELDS = ['count', 'results'];
const SELECTION_ROW_FIELDS = ['id', 'owned'];

function getPinSortStorage() {
  try {
    return window.localStorage;
  } catch (_error) {
    return null;
  }
}

function hasExactFields(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  return keys.length === fields.length && keys.every((key, index) => key === fields[index]);
}

function validateSelectionResponse(response) {
  if (!response || !hasExactFields(response.data, SELECTION_FIELDS)) {
    return { code: 'selection_failed' };
  }
  const { count, results } = response.data;
  if (
    !Number.isInteger(count)
    || count < 0
    || !Array.isArray(results)
    || count !== results.length
  ) return { code: 'selection_failed' };

  const ids = new Set();
  const valid = results.every((row) => {
    if (
      !hasExactFields(row, SELECTION_ROW_FIELDS)
      || !Number.isInteger(row.id)
      || row.id <= 0
      || typeof row.owned !== 'boolean'
      || ids.has(row.id)
    ) return false;
    ids.add(row.id);
    return true;
  });
  if (!valid) return { code: 'selection_failed' };
  if (count > MAX_SELECTION_IDS) return { code: 'selection_too_large' };
  return { count, results };
}

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
  image.is_gif = /\.gif$/i.test(image.large_image_url);
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
      selectionRequestInFlight: false,
      bulkOperationInFlight: false,
      progress: null,
      result: null,
    },
    interactionMode: 'browse',
    coverSelection: {
      candidateId: null,
      inFlight: false,
      error: null,
      requestToken: 0,
    },
    bulkDeleteDialogOpen: false,
    bulkModalOpen: false,
    bulkModalStarted: false,
    sortState: { version: 1, mode: 'latest', randomSeed: 0 },
    sortStorageKey: null,
    sortAnnouncement: '',
    sortLegacyFallback: false,
    sortFallbackAttempted: false,
  };
}

export default {
  name: 'pins',
  beforeCreate() {
    this.selectionModel = new PinSelection();
    this.requestGeneration = 0;
    this.selectionRequestToken = 0;
    this.bulkOperationToken = 0;
    this.bulkModalHandle = null;
    this.previewModalHandle = null;
    this.pinPageRequest = null;
    this.isDestroyed = false;
    this.seedFactory = generateRandomSeed;
  },
  components: {
    loadingSpinner,
    noMore,
    EditorUI,
    PinBulkToolbar,
    BoardCoverToolbar,
    PinSortControls,
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
    pinFilters: {
      deep: true,
      handler() {
        this.activateSortContext();
        this.reset();
        this.$nextTick(() => window.scrollTo(0, 0));
      },
    },
    canSelectPins(canSelect) {
      if (canSelect || !this.selection.active) return;
      this.invalidateSelectionRequest();
      this.invalidateBulkOperation();
      this.updateSelection(this.selectionModel.selectLoaded([]), {
        active: false,
        allCount: 0,
        operationInFlight: false,
        result: null,
      });
      this.interactionMode = 'browse';
    },
    isOwnedBoardRoute(isOwned) {
      if (isOwned || this.interactionMode !== 'cover-selection') return;
      this.invalidateCoverSelection();
    },
  },
  computed: {
    isMyPinsRoute() {
      if (!this.metaReady.user || !this.editorMeta.user.loggedIn) return false;
      const { username } = this.editorMeta.user.meta;
      return Boolean(
        this.pinFilters.userFilter
        && this.pinFilters.userFilter === username,
      );
    },
    isOwnedBoardRoute() {
      if (!this.metaReady.user || !this.editorMeta.user.loggedIn) return false;
      const { username } = this.editorMeta.user.meta;
      const { submitter } = this.editorMeta.currentBoard;
      return Boolean(
        this.pinFilters.boardFilter
        && this.metaReady.board
        && submitter
        && typeof username === 'string'
        && username.length > 0
        && typeof submitter.username === 'string'
        && submitter.username.length > 0
        && submitter.username === username,
      );
    },
    currentBoardCoverId() {
      const id = this.editorMeta.currentBoard.cover_pin_id;
      return Number.isInteger(id) ? id : null;
    },
    canManagePins() {
      return this.isMyPinsRoute || this.isOwnedBoardRoute;
    },
    canSelectPins() {
      return Boolean(
        this.metaReady.user
        && this.editorMeta.user.loggedIn
        && (!this.pinFilters.boardFilter || this.metaReady.board),
      );
    },
    hasNonOwnedSelection() {
      return this.selection.selectedIds.some(
        id => this.selection.ownershipById[id] !== true,
      );
    },
    canUseOwnedPinActions() {
      return !this.hasNonOwnedSelection;
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
    activateSortContext() {
      this.sortStorageKey = pinSortStorageKey(this.pinFilters);
      this.sortState = readPinSortState(
        getPinSortStorage(), this.sortStorageKey, this.seedFactory,
      );
      this.sortLegacyFallback = this.sortStorageKey === null;
      this.sortFallbackAttempted = false;
      this.sortAnnouncement = '';
    },
    applySortMode(mode) {
      if (
        this.interactionMode !== 'browse'
        || this.selection.active
        || this.selection.operationInFlight
        || this.status.loading
      ) return;
      const leavingLegacyFallback = this.sortLegacyFallback;
      const transition = transitionPinSortState(this.sortState, mode, this.seedFactory);
      if (!transition.changed && !leavingLegacyFallback) return;
      this.sortState = transition.state;
      this.sortLegacyFallback = false;
      this.sortFallbackAttempted = false;
      writePinSortState(getPinSortStorage(), this.sortStorageKey, this.sortState);
      const announcement = transition.reshuffled ? this.$t('pinSortReshuffled') : '';
      this.sortAnnouncement = '';
      this.reset();
      this.$nextTick(() => {
        this.sortAnnouncement = announcement;
        window.scrollTo(0, 0);
      });
    },
    sortRequestState() {
      return this.sortLegacyFallback || this.pinFilters.idFilter ? null : this.sortState;
    },
    captureFilterSnapshot() {
      return {
        tagFilter: this.pinFilters.tagFilter,
        userFilter: this.pinFilters.userFilter,
        boardFilter: this.pinFilters.boardFilter,
        idFilter: this.pinFilters.idFilter,
      };
    },
    isRequestCurrent(generation, filters) {
      if (this.isDestroyed || this.requestGeneration !== generation) return false;
      const current = this.pinFilters;
      return current.tagFilter === filters.tagFilter
        && current.userFilter === filters.userFilter
        && current.boardFilter === filters.boardFilter
        && current.idFilter === filters.idFilter;
    },
    invalidateSelectionRequest() {
      this.selectionRequestToken += 1;
      if (this.selection) {
        this.selection.selectionRequestInFlight = false;
        this.syncOperationInFlight();
      }
    },
    invalidateBulkOperation() {
      this.bulkOperationToken += 1;
      const modalHandle = this.bulkModalHandle;
      this.bulkModalHandle = null;
      this.bulkDeleteDialogOpen = false;
      this.bulkModalOpen = false;
      this.bulkModalStarted = false;
      if (this.selection) {
        this.selection.bulkOperationInFlight = false;
        this.syncOperationInFlight();
        this.selection.progress = null;
      }
      if (modalHandle && typeof modalHandle.close === 'function') modalHandle.close();
    },
    syncOperationInFlight() {
      this.selection.operationInFlight = this.selection.selectionRequestInFlight
        || this.selection.bulkOperationInFlight;
    },
    isSelectionRequestCurrent(token, model, generation, filters) {
      return this.selection.active
        && this.selectionRequestToken === token
        && this.selectionModel === model
        && this.isRequestCurrent(generation, filters);
    },
    isBulkOperationCurrent(token, model, generation, filters) {
      return this.bulkOperationToken === token
        && this.selectionModel === model
        && this.isRequestCurrent(generation, filters);
    },
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
      if (
        !this.canSelectPins
        || this.interactionMode !== 'browse'
        || this.selection.bulkOperationInFlight
      ) return;
      this.interactionMode = 'bulk-selection';
      this.updateSelection(this.selectionModel.selectLoaded([]), {
        active: true,
        allCount: 0,
        result: null,
      });
    },
    exitSelection() {
      if (this.selection.bulkOperationInFlight) return;
      this.invalidateSelectionRequest();
      this.updateSelection(this.selectionModel.selectLoaded([]), {
        active: false,
        allCount: 0,
        operationInFlight: false,
        result: null,
      });
      this.interactionMode = 'browse';
    },
    openSelectedExport() {
      if (
        !this.canSelectPins
        || !this.selection.active
        || this.selection.operationInFlight
        || this.selection.selectedIds.length === 0
      ) return;
      openExport(this, { pinIds: this.selection.selectedIds.slice() });
    },
    openBoardExport() {
      const boardId = Number(this.pinFilters.boardFilter);
      if (
        !this.isOwnedBoardRoute
        || this.interactionMode !== 'browse'
        || this.coverSelection.inFlight
        || this.selection.operationInFlight
        || !Number.isSafeInteger(boardId)
        || boardId <= 0
      ) return;
      openExport(this, { boardId });
    },
    enterCoverSelection() {
      if (
        !this.isOwnedBoardRoute
        || this.interactionMode !== 'browse'
        || this.selection.active
        || this.coverSelection.inFlight
      ) return;
      this.interactionMode = 'cover-selection';
      this.coverSelection.candidateId = this.currentBoardCoverId;
      this.coverSelection.error = null;
    },
    invalidateCoverSelection() {
      this.interactionMode = 'browse';
      this.coverSelection.candidateId = null;
      this.coverSelection.inFlight = false;
      this.coverSelection.error = null;
      this.coverSelection.requestToken += 1;
    },
    cancelCoverSelection() {
      if (this.interactionMode !== 'cover-selection' || this.coverSelection.inFlight) return;
      this.invalidateCoverSelection();
      this.$nextTick(() => {
        if (this.$refs.boardCoverToolbar) this.$refs.boardCoverToolbar.focusEnter();
      });
    },
    isCoverCandidate(id) {
      return this.interactionMode === 'cover-selection'
        && this.coverSelection.candidateId === id;
    },
    isCoverCandidateDisabled(item) {
      return this.interactionMode === 'cover-selection'
        && !this.editorMeta.currentBoard.private
        && item.private === true;
    },
    selectCoverCandidate(item) {
      if (
        this.interactionMode !== 'cover-selection'
        || this.coverSelection.inFlight
        || this.isCoverCandidateDisabled(item)
      ) return;
      this.coverSelection.candidateId = item.id;
      this.coverSelection.error = null;
    },
    async applyCoverPin(pinId) {
      if (
        this.coverSelection.inFlight
        || !this.isOwnedBoardRoute
        || this.interactionMode !== 'cover-selection'
      ) return;
      const token = this.coverSelection.requestToken + 1;
      this.coverSelection.requestToken = token;
      this.coverSelection.inFlight = true;
      this.coverSelection.error = null;
      try {
        const response = await API.Board.setCover(
          this.editorMeta.currentBoard.id,
          pinId,
        );
        if (token !== this.coverSelection.requestToken) return;
        this.editorMeta.currentBoard = response.data;
        this.interactionMode = 'browse';
        this.coverSelection.candidateId = null;
        this.$buefy.toast.open({
          message: this.$t('boardCoverSaved'),
          type: 'is-success',
        });
        this.$nextTick(() => {
          const toolbar = this.$refs.boardCoverToolbar;
          if (toolbar && typeof toolbar.focusEnter === 'function') {
            toolbar.focusEnter();
          }
        });
      } catch (error) {
        if (token !== this.coverSelection.requestToken) return;
        this.handleCoverError(error);
      } finally {
        if (token === this.coverSelection.requestToken) {
          this.coverSelection.inFlight = false;
        }
      }
    },
    handleCoverError(error) {
      const response = error && error.response;
      const status = response && response.status;
      const code = response && response.data && response.data.code;
      const invalidCodes = new Set([
        'board_cover_invalid',
        'board_cover_private_pin',
        'board_cover_changed',
      ]);

      if ((status === 400 || status === 409) && invalidCodes.has(code)) {
        this.coverSelection.candidateId = null;
        this.$buefy.toast.open({
          message: this.$t('boardCoverRefreshRequired'),
          type: 'is-warning',
        });
        this.reset();
        return;
      }

      if (status === 403 || status === 404) {
        this.coverSelection.candidateId = null;
        this.$buefy.toast.open({
          message: this.$t('boardCoverSaveFailed'),
          type: 'is-danger',
        });
        this.reset();
        return;
      }

      this.coverSelection.error = this.$t('boardCoverSaveFailed');
    },
    confirmCoverReset() {
      if (
        this.coverSelection.inFlight
        || this.interactionMode !== 'cover-selection'
        || this.currentBoardCoverId === null
      ) return;
      this.$buefy.dialog.confirm({
        message: this.$t('boardCoverResetConfirm'),
        onConfirm: () => this.applyCoverPin(null),
      });
    },
    clearPinSelection() {
      this.updateSelection(this.selectionModel.selectLoaded([]), {
        allCount: 0,
        result: null,
      });
    },
    selectLoadedPins() {
      if (this.selection.operationInFlight) return;
      this.updateSelection(
        this.selectionModel.selectLoaded(this.blocks.map(item => item.id)),
        { allCount: 0, result: null },
      );
    },
    selectAllPins() {
      if (this.selection.operationInFlight) return;
      const token = this.selectionRequestToken + 1;
      const model = this.selectionModel;
      const generation = this.requestGeneration;
      const filters = this.captureFilterSnapshot();
      this.selectionRequestToken = token;
      this.selection.selectionRequestInFlight = true;
      this.syncOperationInFlight();
      this.selection.result = null;
      API.Pin.fetchSelectionIds({
        boardId: filters.boardFilter || null,
      }).then(
        (response) => {
          if (!this.isSelectionRequestCurrent(token, model, generation, filters)) return;
          const validation = validateSelectionResponse(response);
          if (validation.code) {
            this.selection.result = { code: validation.code };
            return;
          }
          const applied = this.selectionModel.tryApplyScope(validation.results);
          if (!applied.applied) {
            this.selection.result = { code: 'selection_failed' };
            return;
          }
          this.updateSelection(applied.snapshot, {
            allCount: validation.count,
            result: null,
          });
        },
        (error) => {
          if (!this.isSelectionRequestCurrent(token, model, generation, filters)) return;
          const code = error && error.response && error.response.data
            ? error.response.data.code
            : 'selection_failed';
          this.selection.result = { code };
        },
      ).catch(() => {
        if (!this.isSelectionRequestCurrent(token, model, generation, filters)) return;
        this.selection.result = { code: 'selection_failed' };
      }).then(() => {
        if (this.isSelectionRequestCurrent(token, model, generation, filters)) {
          this.selection.selectionRequestInFlight = false;
          this.syncOperationInFlight();
        }
      });
    },
    bulkContext() {
      return {
        model: this.selectionModel,
        generation: this.requestGeneration,
        filters: this.captureFilterSnapshot(),
      };
    },
    startBulkModal() {
      const token = this.bulkOperationToken + 1;
      const context = this.bulkContext();
      this.bulkOperationToken = token;
      this.bulkModalOpen = true;
      this.bulkModalStarted = false;
      this.selection.bulkOperationInFlight = true;
      this.syncOperationInFlight();
      return { token, context };
    },
    isBulkModalCurrent(token, context) {
      return this.bulkModalOpen && this.isBulkOperationCurrent(
        token,
        context.model,
        context.generation,
        context.filters,
      );
    },
    markBulkModalStarted(token, context) {
      if (!this.isBulkModalCurrent(token, context)) return false;
      this.bulkModalStarted = true;
      return true;
    },
    settleBulkModal(token, context) {
      if (!this.isBulkModalCurrent(token, context) || !this.bulkModalStarted) return false;
      this.bulkModalStarted = false;
      return true;
    },
    consumeBulkModal(token, context) {
      if (!this.isBulkModalCurrent(token, context)) return false;
      this.bulkOperationToken += 1;
      this.bulkModalHandle = null;
      this.bulkModalOpen = false;
      this.bulkModalStarted = false;
      this.selection.bulkOperationInFlight = false;
      this.syncOperationInFlight();
      return true;
    },
    closeBulkModal(token, context) {
      if (!this.isBulkModalCurrent(token, context) || this.bulkModalStarted) return false;
      return this.consumeBulkModal(token, context);
    },
    completeBulkModal(result, operation, token, context) {
      if (!this.consumeBulkModal(token, context)) return;
      this.finishBulkOperation({ ...result, operation });
    },
    openBulkBoard(mode) {
      if (
        this.selection.operationInFlight
        || !this.selection.active
        || this.selection.selectedIds.length === 0
      ) return;
      if (mode === 'add' && (!this.isMyPinsRoute || !this.canUseOwnedPinActions)) return;
      if (mode === 'move' && !this.isOwnedBoardRoute) return;

      this.selection.result = null;
      const selectedIds = [...this.selection.selectedIds];
      const { token, context } = this.startBulkModal();
      try {
        const modalHandle = openPinBulkBoard(this, {
          mode,
          sourceBoardId: mode === 'move' ? Number(this.pinFilters.boardFilter) : null,
          selectedIds,
          username: this.editorMeta.user.meta.username,
          canStartOperation: () => this.isBulkModalCurrent(token, context),
        }, result => this.completeBulkModal(result, mode, token, context), {
          started: () => this.markBulkModalStarted(token, context),
          settled: () => this.settleBulkModal(token, context),
          closed: () => this.closeBulkModal(token, context),
        });
        if (this.isBulkModalCurrent(token, context)) this.bulkModalHandle = modalHandle;
      } catch (_error) {
        this.consumeBulkModal(token, context);
      }
    },
    openBulkEdit() {
      if (
        this.selection.operationInFlight
        || !this.selection.active
        || this.selection.selectedIds.length === 0
        || !this.canUseOwnedPinActions
      ) return;

      this.selection.result = null;
      const selectedIds = [...this.selection.selectedIds];
      const { token, context } = this.startBulkModal();
      try {
        const modalHandle = openPinBulkEdit(
          this,
          {
            selectedIds,
            canStartOperation: () => this.isBulkModalCurrent(token, context),
          },
          result => this.completeBulkModal(result, 'update', token, context),
          {
            started: () => this.markBulkModalStarted(token, context),
            settled: () => this.settleBulkModal(token, context),
            closed: () => this.closeBulkModal(token, context),
          },
        );
        if (this.isBulkModalCurrent(token, context)) this.bulkModalHandle = modalHandle;
      } catch (_error) {
        this.consumeBulkModal(token, context);
      }
    },
    confirmBulkDelete() {
      if (
        this.bulkDeleteDialogOpen
        || this.selection.operationInFlight
        || !this.selection.active
        || this.selection.selectedIds.length === 0
        || !this.canUseOwnedPinActions
      ) return;

      this.selection.result = null;
      const selectedIds = [...this.selection.selectedIds];
      const token = this.bulkOperationToken + 1;
      const context = this.bulkContext();
      let active = true;
      this.bulkOperationToken = token;
      this.bulkDeleteDialogOpen = true;
      this.$buefy.dialog.confirm({
        message: this.$t('bulkPinDeleteConfirm', { count: selectedIds.length }),
        onConfirm: () => {
          if (
            !active
            || !this.isBulkOperationCurrent(
              token, context.model, context.generation, context.filters,
            )
          ) return;
          active = false;
          this.bulkDeleteDialogOpen = false;
          this.runBulkDelete(selectedIds);
        },
        onCancel: () => {
          if (active && this.isBulkOperationCurrent(
            token, context.model, context.generation, context.filters,
          )) {
            active = false;
            this.bulkDeleteDialogOpen = false;
          }
        },
      });
    },
    failedDeleteIds(ids, result) {
      const candidates = new Set([
        ...(result.failedIds || []),
        ...(result.remainingIds || []),
      ]);
      return ids.filter(id => candidates.has(id));
    },
    validatedOwnedSelectionRows(response) {
      if (!response || !response.data) return null;
      const { count, results } = response.data;
      if (
        !Number.isInteger(count)
        || count < 0
        || !Array.isArray(results)
        || count !== results.length
      ) return null;

      const ids = new Set();
      const valid = results.every((row) => {
        if (
          !row
          || !Number.isInteger(row.id)
          || row.id <= 0
          || ids.has(row.id)
          || typeof row.owned !== 'boolean'
        ) return false;
        ids.add(row.id);
        return true;
      });
      return valid ? results.filter(row => row.owned === true) : null;
    },
    refreshDeleteRetryIds(ids, result, token, context) {
      const candidates = this.failedDeleteIds(ids, result);
      if (candidates.length === 0) return Promise.resolve([]);
      return API.Pin.fetchSelectionIds({
        boardId: context.filters.boardFilter
          ? Number(context.filters.boardFilter)
          : null,
      }).then(
        (response) => {
          if (!this.isBulkOperationCurrent(
            token, context.model, context.generation, context.filters,
          )) return [];
          const rows = this.validatedOwnedSelectionRows(response);
          return rows === null ? [] : intersectRemaining(candidates, rows);
        },
        () => [],
      );
    },
    runBulkDelete(ids) {
      if (this.selection.operationInFlight || ids.length === 0) return null;
      const token = this.bulkOperationToken + 1;
      const context = this.bulkContext();
      this.bulkOperationToken = token;
      this.selection.bulkOperationInFlight = true;
      this.syncOperationInFlight();
      this.selection.progress = { completed: 0, total: ids.length };
      this.selection.result = null;
      return executeBulk({
        ids: [...ids],
        operation: 'delete',
        request: payload => API.Pin.bulk(payload),
        onProgress: (progress) => {
          if (this.isBulkOperationCurrent(
            token, context.model, context.generation, context.filters,
          )) this.selection.progress = progress;
        },
      }).then((result) => {
        if (!this.isBulkOperationCurrent(
          token, context.model, context.generation, context.filters,
        )) return result;
        return this.refreshDeleteRetryIds(ids, result, token, context).then((retryIds) => {
          if (!this.isBulkOperationCurrent(
            token, context.model, context.generation, context.filters,
          )) return result;
          this.finishBulkOperation({ ...result, operation: 'delete', retryIds });
          return result;
        });
      });
    },
    retryBulkDelete() {
      if (
        this.selection.operationInFlight
        || !this.selection.result
        || !Array.isArray(this.selection.result.retryIds)
        || this.selection.result.retryIds.length === 0
      ) return;
      this.runBulkDelete([...this.selection.result.retryIds]);
    },
    finishBulkOperation(result) {
      this.reset();
      if (this.isDestroyed) return;
      this.selection.result = result;
      this.selection.progress = null;
    },
    isPinSelected(id) {
      return this.selection.selectedIds.includes(id);
    },
    togglePinSelection(id, event) {
      if (!this.selection.active) return;
      if (event) event.preventDefault();
      if (this.selection.operationInFlight) return;
      this.updateSelection(this.selectionModel.toggle(id, {
        shiftKey: Boolean(event && event.shiftKey),
        ctrlKey: Boolean(event && event.ctrlKey),
        metaKey: Boolean(event && event.metaKey),
      }), { result: null });
    },
    onPinCardClickCapture(event) {
      if (this.interactionMode === 'cover-selection' || this.selection.active) {
        event.preventDefault();
      }
    },
    onPinCardClick(item, event) {
      if (this.interactionMode === 'cover-selection') {
        if (event) event.preventDefault();
        this.selectCoverCandidate(item);
        return;
      }
      if (!this.selection.active) return;
      this.togglePinSelection(item.id, event);
    },
    onPinImageClick(item, event) {
      if (this.interactionMode === 'cover-selection') {
        if (event) event.preventDefault();
        this.selectCoverCandidate(item);
        return;
      }
      if (this.selection.active) {
        this.togglePinSelection(item.id, event);
        return;
      }
      this.openPreview(item);
    },
    onPinCardKeydown(item, event) {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      if (this.interactionMode === 'cover-selection') {
        event.preventDefault();
        this.selectCoverCandidate(item);
        return;
      }
      if (!this.selection.active) return;
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
      if (this.interactionMode === 'cover-selection') {
        if (event.key === 'Escape') {
          event.preventDefault();
          this.cancelCoverSelection();
        }
        return;
      }
      if (!this.selection.active) return;
      if (event.key === 'Escape') {
        event.preventDefault();
        if (this.selection.bulkOperationInFlight) return;
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
      this.closePreview();
      this.previewModalHandle = this.$buefy.modal.open(
        {
          parent: this,
          component: PinPreview,
          props: {
            pinItem,
            navigation: this.pinFilters.idFilter ? null : () => ({
              items: this.blocks,
              hasNext: this.status.hasNext,
            }),
            loadNext: () => this.fetchMore(),
          },
          scroll: 'keep',
          customClass: 'pin-preview-at-home',
        },
      );
      const modal = this.previewModalHandle;
      if (modal) {
        modal.$once('close', () => {
          if (this.previewModalHandle === modal) this.previewModalHandle = null;
        });
      }
    },
    closePreview() {
      const modal = this.previewModalHandle;
      this.previewModalHandle = null;
      if (modal && typeof modal.close === 'function') modal.close();
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
      const generation = this.requestGeneration;
      const filters = this.captureFilterSnapshot();
      this.initializeMeta(generation, filters);
      this.fetchMore(true, generation, filters);
    },
    initializeMeta(
      generation = this.requestGeneration,
      filters = this.captureFilterSnapshot(),
    ) {
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
          self.metaReady.user = true;
          self.syncLoadedSelection();
        },
        () => {
          if (!self.isRequestCurrent(generation, filters)) return;
          self.editorMeta.user.loggedIn = false;
          self.editorMeta.user.meta = {};
          self.metaReady.user = true;
          self.syncLoadedSelection();
        },
      );
    },
    reset() {
      this.closePreview();
      this.pinPageRequest = null;
      this.invalidateSelectionRequest();
      this.invalidateBulkOperation();
      this.invalidateCoverSelection();
      const coverRequestToken = this.coverSelection.requestToken;
      this.requestGeneration += 1;
      const sorting = {
        sortState: this.sortState,
        sortStorageKey: this.sortStorageKey,
        sortAnnouncement: this.sortAnnouncement,
        sortLegacyFallback: this.sortLegacyFallback,
        sortFallbackAttempted: this.sortFallbackAttempted,
      };
      const data = initialData();
      this.selectionModel = new PinSelection();
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
      this.coverSelection.requestToken = coverRequestToken;
      this.initialize();
    },
    fallbackToLegacySort() {
      if (this.sortFallbackAttempted) return false;
      this.sortFallbackAttempted = true;
      this.sortLegacyFallback = true;
      this.sortState = { ...this.sortState, mode: 'latest' };
      this.sortAnnouncement = '';
      try {
        const storage = getPinSortStorage();
        if (storage) storage.removeItem(this.sortStorageKey);
      } catch (_ignored) {
        // 저장소 삭제가 막혀도 메모리 대체 상태를 사용한다.
      }
      this.reset();
      return true;
    },
    fetchMore(
      created,
      generation = this.requestGeneration,
      filters = this.captureFilterSnapshot(),
    ) {
      if (!this.isRequestCurrent(generation, filters)) return null;
      if (this.status.loading) return this.pinPageRequest;
      if (!this.shouldFetchMore(created)) {
        return null;
      }
      this.status.loading = true;
      let promise;
      const { offset } = this.status;
      if (filters.tagFilter) {
        promise = API.fetchPins(
          offset, filters.tagFilter, null, null, this.sortRequestState(),
        );
      } else if (filters.userFilter) {
        promise = API.fetchPins(
          offset, null, filters.userFilter, null, this.sortRequestState(),
        );
      } else if (filters.boardFilter) {
        if (this.metaReady.board) {
          promise = API.fetchPins(
            offset, null, null, filters.boardFilter, this.sortRequestState(),
          );
        } else {
          const prevPromise = API.Board.get(filters.boardFilter);
          promise = prevPromise.then(
            (resp) => {
              if (!this.isRequestCurrent(generation, filters)) return null;
              this.editorMeta.currentBoard = resp.data;
              this.metaReady.board = true;
              return API.fetchPins(
                offset, null, null, filters.boardFilter, this.sortRequestState(),
              );
            },
          );
        }
      } else if (filters.idFilter) {
        promise = API.fetchPin(filters.idFilter);
      } else {
        promise = API.fetchPins(offset, null, null, null, this.sortRequestState());
      }
      this.pinPageRequest = promise.then(
        (resp) => {
          if (!resp || !this.isRequestCurrent(generation, filters)) return false;
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
          this.syncLoadedSelection();
          this.status.offset += consumed;
          this.status.hasNext = next !== null;
          this.status.loading = false;
          return true;
        },
        (error) => {
          if (!this.isRequestCurrent(generation, filters)) return false;
          const status = error && error.response ? error.response.status : null;
          const code = error && error.response && error.response.data
            ? error.response.data.code : null;
          if (
            status === 400
            && code === 'pin_sort_invalid'
            && this.fallbackToLegacySort()
          ) return false;
          this.status.loading = false;
          return false;
        },
      );
      return this.pinPageRequest;
    },
    niceLinks,
  },
  created() {
    bus.bus.$on(bus.events.refreshPin, this.reset);
    this.registerScrollEvent();
    document.addEventListener('keydown', this.onDocumentKeydown);
    this.activateSortContext();
    this.initialize();
  },
  beforeDestroy() {
    this.closePreview();
    this.invalidateSelectionRequest();
    this.invalidateBulkOperation();
    this.invalidateCoverSelection();
    this.updateSelection(this.selectionModel.selectLoaded([]), {
      active: false,
      allCount: 0,
      operationInFlight: false,
      result: null,
    });
    this.isDestroyed = true;
    this.requestGeneration += 1;
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

.pin-image-container {
  position: relative;
}
.pin-gif-badge {
  position: absolute;
  bottom: 8px;
  left: 8px;
  z-index: 1;
  padding: 2px 6px;
  border-radius: 3px;
  background: rgba(0, 0, 0, 0.8);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  line-height: 20px;
  pointer-events: none;
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

.pin-tools {
  margin-bottom: 1rem;
}

.pin-tools__primary,
.pin-tools__management,
.pin-tools__active {
  display: flex;
  align-items: center;
  gap: .5rem;
}

.pin-tools__primary,
.pin-tools__active {
  justify-content: space-between;
}

.pin-tools__management {
  flex-wrap: wrap;
  justify-content: flex-end;
}

.pin-tools__active {
  margin-top: 1rem;
  padding-top: 1rem;
  border-top: 1px solid #dbdbdb;
}

.pin-tools__status {
  margin: 0;
}

@media screen and (max-width: 768px) {
  .pin-tools__primary,
  .pin-tools__active {
    align-items: stretch;
    flex-direction: column;
  }

  .pin-tools__management {
    justify-content: flex-start;
  }
}

.pin-card{
  position: relative;

  &.is-selected {
    outline: 3px solid #3273dc;
    outline-offset: 2px;
  }

  &[role="button"] .pin-preview-image {
    cursor: pointer;
  }

  &.is-cover-disabled {
    opacity: .6;

    .pin-preview-image {
      cursor: not-allowed;
    }
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
