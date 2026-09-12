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
      <p v-if="pageError" role="status" class="preview-message">{{ $t('previewPageError') }}</p>
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
            <img :key="currentPin.id" ref="previewImage" data-test="preview-image"
               v-show="imageReady" :src="currentPin.large_image_url"
               :alt="currentPin.description || $t('previewImage')"
               @load="onImageLoaded" @error="onImageError">
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
    return {
      currentPin: this.pinItem,
      busy: false,
      pageError: false,
      imageReady: false,
      imageError: false,
      originalSize: false,
    };
  },
  computed: {
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
    document.addEventListener('keydown', this.onKeydown);
  },
  beforeUnmount() {
    this.deactivate();
  },
  methods: {
    toggleOriginalSize() {
      if (!this.imageReady || this.imageError) return;
      this.originalSize = !this.originalSize;
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
      document.removeEventListener('keydown', this.onKeydown);
    },
    async move(direction) {
      if (this.disposed || !this.isModalActive() || this.busy || !this.navigation
          || (direction < 0 ? !this.hasPrevious : !this.hasNext)) return;
      this.pageError = false;
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
      if (this.disposed || !this.isModalActive() || this.pageError) return;
      const item = this.context.items[index];
      if (item) {
        this.originalSize = false;
        this.resetImageScroll();
        this.imageReady = false;
        this.imageError = false;
        this.currentPin = item;
        // 상세 영역만 처음으로 이동하고 뒤쪽 목록의 스크롤은 유지한다.
        this.$nextTick(() => {
          const content = this.$el.closest('.modal-content');
          if (content) content.scrollTop = 0;
        });
      }
    },
    onKeydown(event) {
      const { target } = event;
      if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey
          || (target && target.closest && target.closest('input, textarea, select, [contenteditable]'))) return;
      if (!this.navigation || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      this.move(event.key === 'ArrowLeft' ? -1 : 1);
    },
    onImageLoaded(event) {
      if (event.target === this.$refs.previewImage) this.imageReady = true;
    },
    onImageError(event) {
      if (event.target === this.$refs.previewImage) this.imageError = true;
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
.is-original-size .preview-image-toggle { width: max-content; max-width: none; cursor: zoom-out; }
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
