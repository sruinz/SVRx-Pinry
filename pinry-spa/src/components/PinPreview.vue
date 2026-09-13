<template>
  <div class="pin-preview-modal">
    <header class="preview-header">
      <button type="button" class="preview-close" data-test="preview-close"
        :aria-label="$t('closeButton')" @click="closePreview">×</button>
      <span v-if="navigation" aria-live="polite" class="preview-position">
        {{ currentIndex + 1 }} / {{ context.items.length }}{{ context.hasNext ? '+' : '' }}
      </span>
    </header>
    <section class="card">
      <p v-if="fullscreenError" role="status" class="preview-message">{{ $t('previewFullscreenError') }}</p>
      <p v-if="pageError" role="status" class="preview-message">{{ $t('previewPageError') }}</p>
      <p v-if="transitionError" role="status" class="preview-message">{{ $t('previewImageError') }}</p>
      <div class="card-image">
        <figure ref="imageViewport" class="image preview-image-viewport"
                :class="{ 'is-original-size': originalSize }"
                :aria-busy="!imageReady && !imageError ? 'true' : 'false'">
          <p v-if="!imageReady" role="status" class="preview-message">
            {{ $t(imageError ? 'previewImageError' : 'previewImageLoading') }}
          </p>
          <button v-show="imageReady" type="button" class="preview-image-toggle"
                  data-test="preview-image-toggle" @click.stop="toggleOriginalSize"
                  :aria-label="$t(originalSize ? 'previewFitScreen' : 'previewOriginalSize')">
            <img v-for="item in previewImages" :key="item.id"
               :data-test="item.id === currentPin.id ? 'preview-image' : 'preview-preload'"
               v-show="item.id === currentPin.id && imageReady" :src="item.large_image_url"
               :aria-hidden="item.id !== currentPin.id ? 'true' : undefined"
               :alt="item.id === currentPin.id ? (item.description || $t('previewImage')) : ''"
               @load="onImageLoaded($event, item)" @error="onImageError($event, item)">
          </button>
        </figure>
        <nav v-if="navigation" class="preview-navigation" data-test="preview-navigation"
             :aria-label="$t('previewNavigation')">
          <button type="button" class="preview-step preview-step--previous"
                  data-test="preview-previous" :disabled="busy || !hasPrevious"
                  @click.stop="move(-1)" :aria-label="$t('previewPrevious')">
            <span class="preview-step__circle"><i class="mdi mdi-chevron-left" aria-hidden="true"></i></span>
            <span class="preview-step__label">{{ $t('previewPrevious') }}</span>
          </button>
          <button type="button" class="preview-step preview-step--next"
                  data-test="preview-next" :disabled="busy || !hasNext"
                  @click.stop="move(1)" :aria-label="$t('previewNext')">
            <span class="preview-step__circle"><i class="mdi mdi-chevron-right" aria-hidden="true"></i></span>
            <span class="preview-step__label">{{ $t('previewNext') }}</span>
          </button>
        </nav>
      </div>
      <div class="card-content">
        <p class="description" v-html="niceLinks(currentPin.description)"></p>
        <div class="preview-details">
          <div class="preview-author">
            <img :src="currentPin.avatar" alt="">
            <span>{{ currentPin.author }}</span>
          </div>
          <div v-if="currentPin.tags.length" class="preview-tags">
            <span v-for="tag in currentPin.tags" :key="tag" class="tag pin-preview-tag">{{ tag }}</span>
          </div>
          <div class="preview-links">
            <button v-if="fullscreenSupported" type="button" class="meta-link"
                    data-test="preview-fullscreen" :disabled="fullscreenBusy" @click.stop="toggleFullscreen">
              <i :class="['mdi', fullscreen ? 'mdi-fullscreen-exit' : 'mdi-fullscreen']" aria-hidden="true"></i>
              {{ $t(fullscreen ? 'previewExitFullscreen' : 'previewFullscreen') }}
            </button>
            <button v-if="navigation" type="button" class="meta-link" data-test="preview-play"
                    :disabled="!playing && (busy || !imageReady || imageError || !hasNext)"
                    :aria-pressed="playing ? 'true' : 'false'" @click.stop="toggleSlideshow">
              <i :class="['mdi', playing ? 'mdi-pause' : 'mdi-play']" aria-hidden="true"></i>
              {{ $t(playing ? 'previewPause' : 'previewPlay') }}
            </button>
            <select v-if="navigation" class="meta-link preview-interval" data-test="preview-interval"
                    :value="slideInterval" :aria-label="$t('previewInterval')" @change="changeInterval">
              <option v-for="seconds in [1, 3, 5, 10]" :key="seconds" :value="seconds">
                {{ $t('previewSeconds', { seconds }) }}
              </option>
            </select>
            <button type="button" class="meta-link" data-test="preview-zoom"
                    :disabled="!imageReady || imageError"
                    :aria-pressed="originalSize ? 'true' : 'false'" @click.stop="toggleOriginalSize">
              {{ $t(originalSize ? 'previewFitScreen' : 'previewOriginalSize') }}
            </button>
            <a :href="currentPin.large_image_url" target="_blank" rel="noopener noreferrer"
               class="meta-link" data-test="preview-stored-original">{{ $t('previewOpenOriginal') }}</a>
            <a v-if="currentPin.referer !== null" :href="currentPin.referer"
               target="_blank" rel="noopener noreferrer" class="meta-link">
              {{ $t('sourceButton') }}
            </a>
            <a v-if="currentPin.original_image_url !== null" :href="currentPin.original_image_url"
               target="_blank" rel="noopener noreferrer" class="meta-link">
              {{ $t('originalImageButton') }}
            </a>
            <button type="button" @click="closeAndGoTo" class="meta-link">
              {{ $t('permalinkButton') }}
            </button>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script>
import niceLinks from './utils/niceLinks';

const SIZE_STORAGE_KEY = 'pinry-preview-size';
const INTERVAL_STORAGE_KEY = 'pinry-preview-interval';

export default {
  name: 'PinPreview',
  emits: ['close'],
  inject: { isModalActive: { default: () => () => true } },
  props: {
    pinItem: { type: Object, required: true },
    navigation: { type: Function, default: null },
    loadNext: { type: Function, default: null },
  },
  data() {
    let originalSize = false;
    let slideInterval = 5;
    try {
      originalSize = window.localStorage.getItem(SIZE_STORAGE_KEY) === 'original';
      const storedInterval = Number(window.localStorage.getItem(INTERVAL_STORAGE_KEY));
      if ([1, 3, 5, 10].includes(storedInterval)) slideInterval = storedInterval;
    } catch (_error) {
      // 저장소를 읽을 수 없으면 화면 맞춤으로 시작한다.
    }
    return {
      currentPin: this.pinItem,
      busy: false,
      pageError: false,
      imageReady: false,
      imageError: false,
      preparedPin: null,
      preparedReady: false,
      preparedFailed: false,
      pendingMove: null,
      transitionError: false,
      originalSize,
      slideInterval,
      playing: false,
      fullscreen: false,
      fullscreenSupported: false,
      fullscreenBusy: false,
      fullscreenError: false,
    };
  },
  computed: {
    previewImages() {
      return this.preparedPin ? [this.currentPin, this.preparedPin] : [this.currentPin];
    },
    context() {
      return this.navigation ? this.navigation() : { items: [this.currentPin], hasNext: false };
    },
    currentIndex() {
      return this.context.items.findIndex(item => item.id === this.currentPin.id);
    },
    hasPrevious() {
      return this.currentIndex > 0;
    },
    hasNext() {
      return this.currentIndex >= 0 && (
        this.currentIndex < this.context.items.length - 1 || this.context.hasNext
      );
    },
  },
  mounted() {
    this.disposed = false;
    this.scheduleRevision = 0;
    this.fullscreenSupported = typeof this.$el.requestFullscreen === 'function'
      && document.fullscreenEnabled !== false;
    document.addEventListener('keydown', this.onKeydown);
    document.addEventListener('visibilitychange', this.onVisibilityChange);
    document.addEventListener('fullscreenchange', this.onFullscreenChange);
    document.addEventListener('scroll', this.onPreviewScroll, { capture: true, passive: true });
  },
  beforeUnmount() {
    this.deactivate();
  },
  methods: {
    changeInterval(event) {
      const seconds = Number(event.target.value);
      if (![1, 3, 5, 10].includes(seconds)) return;
      this.slideInterval = seconds;
      try {
        window.localStorage.setItem(INTERVAL_STORAGE_KEY, String(seconds));
      } catch (_error) {
        // 저장소가 차단되어도 현재 선택과 재생은 유지한다.
      }
      if (this.pendingMove && this.pendingMove.automatic) this.pendingMove = null;
      this.scheduleSlide();
    },
    onPreviewScroll(event) {
      const content = this.$el.closest('.modal-content');
      if (this.playing && (this.$el.contains(event.target) || event.target === content)) {
        // 마지막 스크롤부터 감상 시간을 다시 주고 자동 재생 상태는 유지한다.
        if (this.pendingMove && this.pendingMove.automatic) this.pendingMove = null;
        this.scheduleSlide();
      }
    },
    stopSlideshow() {
      this.playing = false;
      if (this.pendingMove && this.pendingMove.automatic) this.pendingMove = null;
      clearTimeout(this.slideTimer);
      this.slideTimer = null;
    },
    toggleSlideshow() {
      if (this.playing) {
        this.stopSlideshow();
      } else if (!this.disposed && this.isModalActive() && !document.hidden
          && this.imageReady && !this.imageError && !this.busy && this.hasNext) {
        this.playing = true;
        this.scheduleSlide();
      }
    },
    scheduleSlide() {
      this.scheduleRevision += 1;
      clearTimeout(this.slideTimer);
      if (!this.playing || !this.imageReady || this.imageError || this.busy) return;
      if (!this.hasNext || this.disposed || !this.isModalActive() || document.hidden) {
        this.stopSlideshow();
        return;
      }
      this.slideTimer = setTimeout(() => {
        this.slideTimer = null;
        if (this.disposed || !this.isModalActive() || document.hidden) this.stopSlideshow();
        else this.move(1, true);
      }, this.slideInterval * 1000);
    },
    onVisibilityChange() {
      if (document.hidden) this.stopSlideshow();
    },
    onFullscreenChange() {
      const wasFullscreen = this.fullscreen;
      this.fullscreen = document.fullscreenElement === this.$el;
      if (wasFullscreen && !this.fullscreen) this.stopSlideshow();
    },
    async toggleFullscreen() {
      if (this.disposed || !this.isModalActive() || this.fullscreenBusy) return;
      this.fullscreenBusy = true;
      this.fullscreenError = false;
      const element = this.$el;
      try {
        if (document.fullscreenElement === element) await document.exitFullscreen();
        else await element.requestFullscreen();
        // 요청 도중 닫힌 상세보기가 전체 화면에 남지 않도록 한다.
        if (this.disposed && document.fullscreenElement === element) await document.exitFullscreen();
      } catch (_error) {
        if (!this.disposed) this.fullscreenError = true;
      } finally {
        this.fullscreenBusy = false;
      }
    },
    toggleOriginalSize() {
      if (!this.imageReady || this.imageError) return;
      this.originalSize = !this.originalSize;
      try {
        window.localStorage.setItem(SIZE_STORAGE_KEY, this.originalSize ? 'original' : 'fit');
      } catch (_error) {
        // 저장할 수 없어도 현재 상세보기에서는 선택을 유지한다.
      }
      this.resetImageScroll();
    },
    resetImageScroll() {
      this.$nextTick(() => {
        const viewport = this.$refs.imageViewport;
        if (viewport) {
          viewport.scrollTop = 0;
          viewport.scrollLeft = 0;
        }
      });
    },
    deactivate() {
      this.disposed = true;
      this.stopSlideshow();
      this.pendingMove = null;
      this.preparedPin = null;
      document.removeEventListener('keydown', this.onKeydown);
      document.removeEventListener('visibilitychange', this.onVisibilityChange);
      document.removeEventListener('fullscreenchange', this.onFullscreenChange);
      document.removeEventListener('scroll', this.onPreviewScroll, true);
      if (document.fullscreenElement === this.$el) {
        Promise.resolve(document.exitFullscreen()).catch(() => {});
      }
    },
    async move(direction, automatic = false) {
      if (!automatic) this.stopSlideshow();
      if (this.disposed || !this.isModalActive() || this.busy || !this.navigation
          || (direction < 0 ? !this.hasPrevious : !this.hasNext)) return;
      this.pageError = false;
      const { scheduleRevision } = this;
      const index = this.currentIndex + direction;
      if (index >= this.context.items.length && this.loadNext) {
        this.busy = true;
        try {
          const loaded = await this.loadNext();
          if (loaded === false) this.pageError = true;
        } catch (_error) {
          this.pageError = true;
        } finally {
          this.busy = false;
        }
      }
      if (this.disposed || !this.isModalActive() || this.pageError) {
        this.stopSlideshow();
        return;
      }
      if (automatic && !this.playing) return;
      if (automatic && scheduleRevision !== this.scheduleRevision) {
        this.scheduleSlide();
        return;
      }
      const item = this.context.items[index];
      if (item) {
        this.transitionError = false;
        if (this.imageReady && !this.imageError) {
          // 실패한 준비 이미지는 사용자가 다시 이동을 요청할 때만 새로 요청한다.
          if (this.preparedPin && this.preparedPin.id === item.id && this.preparedFailed) {
            if (automatic) {
              this.transitionError = true;
              this.stopSlideshow();
              return;
            }
            this.preparedPin = null;
            await this.$nextTick();
            if (this.disposed || (automatic && !this.playing)) return;
          }
          this.pendingMove = { id: item.id, automatic };
          this.prepareImage(item);
          this.commitPrepared();
        } else this.activatePin(item, false);
      }
      if (!item) this.stopSlideshow();
    },
    prepareImage(item) {
      if (this.preparedPin && this.preparedPin.id === item.id) return;
      this.preparedPin = item;
      this.preparedReady = false;
      this.preparedFailed = false;
    },
    prepareNext() {
      if (this.disposed || this.pendingMove || !this.imageReady || document.hidden) return;
      const item = this.context.items[this.currentIndex + 1];
      if (item) this.prepareImage(item);
    },
    commitPrepared() {
      const pending = this.pendingMove;
      if (this.disposed || !this.isModalActive() || !pending || !this.preparedReady
          || !this.preparedPin || pending.id !== this.preparedPin.id
          || (pending.automatic && (!this.playing || document.hidden))) return;
      this.activatePin(this.preparedPin, true);
    },
    activatePin(item, ready) {
      this.currentPin = item;
      this.imageReady = ready;
      this.imageError = false;
      this.pendingMove = null;
      this.preparedPin = null;
      this.preparedReady = false;
      this.preparedFailed = false;
      this.resetImageScroll();
      if (!this.hasNext) this.stopSlideshow();
      this.$nextTick(() => {
        if (this.disposed) return;
        if (document.fullscreenElement === this.$el) this.$el.scrollTop = 0;
        const content = this.$el.closest('.modal-content');
        if (content) content.scrollTop = 0;
        if (ready) {
          this.prepareNext();
          this.scheduleSlide();
        }
      });
    },
    onKeydown(event) {
      const { target } = event;
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey
          || (target && target.closest && target.closest('input, textarea, select, [contenteditable]'))) return;
      if (!this.navigation || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      this.move(event.key === 'ArrowLeft' ? -1 : 1);
    },
    async onImageLoaded(event, item) {
      const image = event.target;
      if (this.disposed || !this.$el.contains(image)) return;
      if (item.id === this.currentPin.id) {
        this.imageReady = true;
        this.prepareNext();
        this.scheduleSlide();
      } else if (this.preparedPin && item.id === this.preparedPin.id) {
        const prepared = this.preparedPin;
        try {
          if (image.decode) await image.decode();
        } catch (_error) {
          if (this.preparedPin === prepared) this.onImageError({ target: image }, item);
          return;
        }
        if (this.disposed || this.preparedPin !== prepared
            || !this.$el.contains(image)) return;
        this.preparedReady = true;
        this.commitPrepared();
      }
    },
    onImageError(event, item) {
      if (this.disposed || !this.$el.contains(event.target)) return;
      if (item.id === this.currentPin.id) {
        this.imageError = true;
        this.stopSlideshow();
      } else if (this.preparedPin && item.id === this.preparedPin.id) {
        this.preparedFailed = true;
        if (this.pendingMove && this.pendingMove.id === item.id) {
          this.transitionError = true;
          this.pendingMove = null;
          this.stopSlideshow();
        }
      }
    },
    closeAndGoTo() {
      this.closePreview();
      this.$router.push(
        { name: 'pin', params: { pinId: this.currentPin.id } },
      );
    },
    closePreview() {
      this.deactivate();
      this.$emit('close');
    },
    niceLinks,
  },
};
</script>

<style scoped>
.pin-preview-modal {
  position: relative;
  padding: 0 12px 12px;
  border: 1px solid #526368;
  border-radius: 9px;
  background: var(--pinry-surface);
  color: var(--pinry-text);
  box-shadow: 0 18px 70px rgba(0, 0, 0, .35);
}
.preview-header { position: relative; display: flex; align-items: center; justify-content: center; min-height: 46px; }
.preview-close {
  position: absolute;
  top: 0;
  right: -6px;
  z-index: 3;
  width: 44px;
  height: 44px;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--pinry-text);
  font-size: 30px;
  font-weight: 300;
  cursor: pointer;
}
.preview-close:hover { background: var(--pinry-hover); }
.preview-position { padding-inline: 44px; white-space: nowrap; font-size: 13px; font-variant-numeric: tabular-nums; }
.card { background: transparent; color: inherit; box-shadow: none; }
.card-image { position: relative; }
.card-image .image { min-height: 96px; display: grid; align-items: center; }
.card-image img {
  display: block;
  width: 100%;
  height: auto;
  max-height: calc(100dvh - 220px);
  object-fit: contain;
  border-radius: 5px;
}
.meta-link:disabled { opacity: .45; cursor: default; }
.meta-link[aria-pressed="true"] { color: var(--pinry-accent); }
.preview-image-toggle { display: block; width: 100%; padding: 0; border: 0; background: transparent; cursor: zoom-in; }
.preview-image-viewport.is-original-size { display: block; overflow: auto; max-height: calc(100dvh - 220px); }
.is-original-size .preview-image-toggle { width: max-content; max-width: none; margin-inline: auto; cursor: zoom-out; }
.is-original-size .preview-image-toggle img { width: auto; max-width: none; max-height: none; }
.meta-link:focus-visible,
.preview-image-toggle:focus-visible { outline: 2px solid var(--pinry-accent); outline-offset: -2px; }
.preview-step {
  position: absolute;
  top: 50%;
  transform: translateY(-24px);
  display: flex;
  align-items: center;
  flex-direction: column;
  gap: 5px;
  width: 60px;
  padding: 0;
  border: 0;
  background: transparent;
  color: #edf1f1;
  font: inherit;
  cursor: pointer;
}
.preview-step--previous { left: -84px; }
.preview-step--next { right: -84px; }
.preview-step__circle {
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  border: 1px solid #b3bec1;
  border-radius: 50%;
  background: rgba(17, 23, 24, .88);
  font-size: 36px;
  line-height: 1;
}
.preview-step__label { font-size: 13px; text-shadow: 0 1px 4px #000; }
.preview-step:hover .preview-step__circle { background: #2e4144; }
.preview-step:disabled { opacity: .35; cursor: default; }
.preview-message { padding: 20px 12px; color: var(--pinry-muted); text-align: center; }
.card-content { padding: 12px 5px 0; }
.description { color: var(--pinry-text); font-size: 18px; font-weight: 600; line-height: 1.5; overflow-wrap: anywhere; }
.preview-details { display: flex; align-items: center; flex-wrap: wrap; gap: 10px 14px; margin-top: 9px; }
.preview-author { display: flex; align-items: center; gap: 8px; color: var(--pinry-muted); font-size: 12px; }
.preview-author img { width: 26px; height: 26px; border-radius: 50%; }
.preview-tags { display: flex; flex-wrap: wrap; gap: 6px; }
.preview-links { display: flex; align-items: center; flex-wrap: wrap; gap: 0; margin-left: auto; }
.meta-link { background: transparent; border: 0; color: var(--pinry-text); font: inherit; font-size: 12px; padding: 0 10px; cursor: pointer; }
.meta-link + .meta-link { border-left: 1px solid var(--pinry-border); }
.meta-link:hover { color: var(--pinry-accent); }
.preview-interval { width: auto; min-width: 64px; }
.preview-interval option { background: var(--pinry-surface); color: var(--pinry-text); }
.pin-preview-modal:fullscreen {
  width: 100%; height: 100%; max-height: none; overflow: auto;
  padding: 0 24px 20px; border: 0; border-radius: 0;
  background: var(--pinry-surface);
}
.pin-preview-modal:fullscreen .preview-step--previous { left: 12px; }
.pin-preview-modal:fullscreen .preview-step--next { right: 12px; }

@media screen and (max-width: 768px) {
  .pin-preview-modal { padding: 0 8px 12px; }
  .preview-step--previous { left: 6px; }
  .preview-step--next { right: 6px; }
  .preview-step { width: 48px; }
  .preview-step__label { display: none; }
  .card-image img { max-height: calc(100dvh - 200px); }
  .description { font-size: 16px; }
  .preview-links { margin-left: 0; flex-basis: 100%; min-height: 44px; }
  .meta-link { min-height: 44px; display: inline-flex; align-items: center; padding-inline: 8px; }
}
</style>
