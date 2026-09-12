<template>
  <div class="pin-preview-modal">
    <button type="button" class="preview-close" data-test="preview-close"
      :aria-label="$t('closeButton')" @click="closePreview">×</button>
    <section>
        <nav v-if="navigation" class="preview-navigation" data-test="preview-navigation"
             :aria-label="$t('previewNavigation')">
          <button type="button" data-test="preview-previous" :disabled="busy || !hasPrevious"
                  @click.stop="move(-1)" :aria-label="$t('previewPrevious')">
            <span aria-hidden="true">‹</span> {{ $t('previewPrevious') }}
          </button>
          <span aria-live="polite" class="preview-position">
            {{ currentIndex + 1 }} / {{ context.items.length }}{{ context.hasNext ? '+' : '' }}
          </span>
          <button type="button" data-test="preview-next" :disabled="busy || !hasNext"
                  @click.stop="move(1)" :aria-label="$t('previewNext')">
            {{ $t('previewNext') }} <span aria-hidden="true">›</span>
          </button>
        </nav>
        <p v-if="pageError" role="status" class="preview-message">{{ $t('previewPageError') }}</p>
        <div class="card">
          <div class="card-image">
            <figure class="image" :aria-busy="!imageReady && !imageError ? 'true' : 'false'">
              <p v-if="!imageReady" role="status" class="preview-message">
                {{ $t(imageError ? 'previewImageError' : 'previewImageLoading') }}
              </p>
              <img :key="currentPin.id" ref="previewImage" data-test="preview-image"
                   v-show="imageReady" :src="currentPin.large_image_url"
                   :alt="currentPin.description || $t('previewImage')"
                   @load="onImageLoaded" @error="onImageError">
            </figure>
          </div>
          <div class="card-content">
            <div class="content">
                <p class="description title" v-html="niceLinks(currentPin.description)"></p>
            </div>
            <div class="media">
              <div class="media-left">
                <figure class="image is-48x48">
                  <img :src="currentPin.avatar" alt="Image">
                </figure>
              </div>
              <div class="media-content">
                <div class="is-pulled-left">
                  <p class="title is-4 pin-meta-info"><span class="dim">{{ $t("pinnedByTitle") }}</span><span class="author">{{ currentPin.author }}</span></p>
                  <p class="subtitle is-6" v-show="currentPin.tags.length > 0">
                    <span class="subtitle dim">in&nbsp;</span>
                    <template v-for="tag in currentPin.tags" :key="tag">
                      <span class="tag pin-preview-tag is-info">{{ tag }}</span>
                    </template>
                  </p>
                </div>
                <div class="is-pulled-right">
                  <a :href="currentPin.referer" target="_blank">
                    <button type="button"
                        v-show="currentPin.referer !== null"
                        class="meta-link"
                        >
                      {{ $t("sourceButton") }}
                    </button>
                  </a>
                  <a :href="currentPin.original_image_url" target="_blank">
                    <button type="button"
                        v-show="currentPin.original_image_url !== null"
                        class="meta-link"
                        >
                        {{ $t("originalImageButton") }}
                    </button>
                  </a>
                  <button type="button"
                      @click="closeAndGoTo"
                      class="meta-link"
                      >
                      {{ $t("permalinkButton") }}
                  </button>
                </div>
              </div>
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

<style lang="scss" scoped>
@import './utils/fonts.scss';
.preview-close { position: absolute; top: 8px; right: 8px; z-index: 3; width: 44px; height: 44px; border: 0; border-radius: 50%; background: #172126; color: white; font-size: 28px; cursor: pointer; }
.pin-preview-modal { position: relative; padding-top: 56px; }

.preview-navigation {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  position: sticky;
  top: 0;
  z-index: 2;
  padding: 8px;
  background: #172126;
  color: #fff;
  button {
    min-height: 44px;
    min-width: 80px;
    padding: 4px 12px;
    border: 1px solid #6b828d;
    border-radius: 6px;
    background: #24353d;
    color: #fff;
    cursor: pointer;
    font: inherit;
    &:disabled { opacity: 0.45; cursor: default; }
    &:focus-visible { outline: 3px solid #5ad2c7; outline-offset: 2px; }
    span { font-size: 24px; vertical-align: middle; }
  }
}
.preview-position { white-space: nowrap; }
.preview-message {
  padding: 20px 12px;
  background: #172126;
  color: #fff;
  text-align: center;
}
.card-image .image { min-height: 80px; }
.meta-link {
  margin-left: 0.3rem;
}
.dim {
  @include secondary-font-color-in-dark;
}
.pin-meta-info {
  line-height: 16px;
}
.card {
  background-color: rgba(0, 0, 0, 0.6);
  .content {
    border-bottom: 1px solid #333;
  }
  .card-content {
    .author {
      @include title-font-color-in-dark;
    }
    padding: 0;
    .content {
      padding: 0.3rem;
      margin-bottom: 0;
    }
    .media {
      padding: 0.3rem;
    }
  }
  .description {
    @include title-font;
    @include title-font-color-in-dark;
    font-size: 16px;
    padding: 8px;
  }
}
.pin-preview-tag {
  margin-right: 0.2rem;
  margin-bottom: 2px;
}
/* preview size should always less then screen */
.card-image img {
  padding: 10px;
  margin-left: auto;
  margin-right: auto;
  width: auto;
}
</style>
