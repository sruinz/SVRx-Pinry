<template>
  <section class="trash-pins" aria-live="polite">
    <p
      v-if="status.loading"
      class="has-text-centered"
      data-test="trash-loading">
      {{ $t("trashLoading") }}
    </p>
    <p
      v-if="status.error"
      class="notification is-danger"
      role="alert"
      data-test="trash-error">
      {{ $t("trashLoadError") }}
    </p>
    <p
      v-if="!status.loading && !status.error && pins.length === 0"
      class="has-text-centered"
      data-test="trash-empty">
      {{ $t("trashEmpty") }}
    </p>
    <div class="columns is-multiline">
      <article
        v-for="pin in pins"
        :key="pin.id"
        :data-pin-id="pin.id"
        class="column is-one-quarter">
        <div class="card">
          <div class="card-image">
            <figure class="image">
              <img :src="pin.image.thumbnail.image" :alt="pin.description">
            </figure>
          </div>
          <div class="card-content">
            <div v-if="pin.description" class="content">
              {{ pin.description }}
            </div>
            <div class="buttons">
              <button
                type="button"
                class="button is-primary"
                data-test="restore"
                :disabled="isBusy(pin.id)"
                @click="restore(pin)">
                {{ $t("trashRestoreButton") }}
              </button>
              <button
                type="button"
                class="button is-danger"
                data-test="delete-permanently"
                :disabled="isBusy(pin.id)"
                @click="confirmPermanentDelete(pin)">
                {{ $t("trashPermanentDeleteButton") }}
              </button>
            </div>
          </div>
        </div>
      </article>
    </div>
  </section>
</template>

<script>
import API from './api';
import scroll from './utils/scroll';

export default {
  name: 'TrashPins',
  data() {
    return {
      pins: [],
      busyPins: {},
      mutationInFlight: false,
      fetchQueued: false,
      unbindScroll: null,
      disposed: false,
      status: {
        loading: false,
        error: false,
        hasNext: true,
        offset: 0,
      },
    };
  },
  methods: {
    registerScrollEvent() {
      this.unbindScroll = scroll.bindScroll2Bottom(() => {
        this.fetchMore();
      });
    },
    fetchMore() {
      if (this.disposed || !this.status.hasNext) {
        return;
      }
      if (this.mutationInFlight) {
        this.fetchQueued = true;
        return;
      }
      if (this.status.loading) {
        return;
      }
      this.status.loading = true;
      this.status.error = false;
      API.Pin.fetchTrash(this.status.offset).then(
        (resp) => {
          if (this.disposed) {
            return;
          }
          const { results, next } = resp.data;
          this.pins = this.pins.concat(results);
          this.status.offset += results.length;
          this.status.hasNext = next !== null;
          this.status.loading = false;
        },
        () => {
          if (this.disposed) {
            return;
          }
          this.status.loading = false;
          this.status.error = true;
        },
      );
    },
    isBusy(pinId) {
      return (
        this.disposed
        || this.status.loading
        || this.mutationInFlight
        || this.busyPins[pinId] === true
      );
    },
    setBusy(pinId, busy) {
      if (busy) {
        this.$set(this.busyPins, pinId, true);
      } else {
        this.$delete(this.busyPins, pinId);
      }
    },
    beginMutation(pinId) {
      if (this.disposed || this.status.loading || this.mutationInFlight) {
        return false;
      }
      this.mutationInFlight = true;
      this.setBusy(pinId, true);
      return true;
    },
    finishMutation(pinId) {
      if (this.disposed) {
        return;
      }
      this.setBusy(pinId, false);
      this.mutationInFlight = false;
      if (this.fetchQueued) {
        this.fetchQueued = false;
        this.fetchMore();
      }
    },
    removePin(pinId) {
      this.pins = this.pins.filter(pin => pin.id !== pinId);
      this.status.offset = Math.max(0, this.status.offset - 1);
    },
    restore(pin) {
      if (!this.beginMutation(pin.id)) {
        return;
      }
      API.Pin.restore(pin.id).then(
        () => {
          if (this.disposed) {
            return;
          }
          this.removePin(pin.id);
          this.$buefy.toast.open(this.$t('trashRestoreSuccess'));
          this.finishMutation(pin.id);
        },
        () => {
          if (this.disposed) {
            return;
          }
          this.$buefy.toast.open({
            type: 'is-danger',
            message: this.$t('trashRestoreError'),
          });
          this.finishMutation(pin.id);
        },
      );
    },
    confirmPermanentDelete(pin) {
      if (this.isBusy(pin.id)) {
        return;
      }
      this.$buefy.dialog.confirm({
        message: this.$t('trashPermanentDeleteConfirm'),
        onConfirm: () => {
          this.deletePermanently(pin);
        },
      });
    },
    deletePermanently(pin) {
      if (!this.beginMutation(pin.id)) {
        return;
      }
      API.Pin.deletePermanently(pin.id).then(
        () => {
          if (this.disposed) {
            return;
          }
          this.removePin(pin.id);
          this.$buefy.toast.open(this.$t('trashPermanentDeleteSuccess'));
          this.finishMutation(pin.id);
        },
        () => {
          if (this.disposed) {
            return;
          }
          this.$buefy.toast.open({
            type: 'is-danger',
            message: this.$t('trashPermanentDeleteError'),
          });
          this.finishMutation(pin.id);
        },
      );
    },
  },
  created() {
    this.registerScrollEvent();
    this.fetchMore();
  },
  beforeDestroy() {
    this.disposed = true;
    this.fetchQueued = false;
    if (this.unbindScroll !== null) {
      this.unbindScroll();
      this.unbindScroll = null;
    }
  },
};
</script>

<style scoped>
.card {
  height: 100%;
}

.card-image img {
  width: 100%;
}
</style>
