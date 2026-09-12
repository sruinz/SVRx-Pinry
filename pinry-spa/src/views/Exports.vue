<template>
  <section class="section" data-test="exports-view">
    <div class="container">
      <h1 class="title">{{ $t('exportsTitle') }}</h1>
      <p v-if="loading" data-test="exports-loading">
        {{ $t('exportsLoading') }}
      </p>

      <div
        v-if="errorType === 'login'"
        class="notification is-warning"
        data-test="export-login-required"
      >
        <p>{{ $t('exportLoginRequired') }}</p>
        <button
          type="button"
          class="button"
          data-test="export-login"
          @click="logIn"
        >
          {{ $t('exportLoginAction') }}
        </button>
      </div>
      <p
        v-else-if="errorType === 'contract'"
        class="notification is-danger"
        data-test="export-contract-error"
      >
        {{ $t('exportContractError') }}
      </p>
      <p
        v-else-if="errorType === 'temporary'"
        class="notification is-warning"
        data-test="export-status-error"
      >
        {{ $t('exportStatusTemporaryError') }}
      </p>

      <ExportStatusPanel
        v-if="hasJobs"
        :latest-attempt="latestAttempt"
        :downloadable-job="downloadableJob"
      />

      <div v-if="showEmpty" data-test="exports-empty">
        <p>{{ $t('exportEmpty') }}</p>
      </div>
      <p v-if="showNewJobHint" data-test="exports-new-job-hint">
        {{ $t('exportsNewJobHint') }}
      </p>

      <div
        class="exports-actions"
        data-test="exports-actions"
      >
        <button
          type="button"
          class="button"
          data-test="export-refresh"
          :disabled="loading"
          @click="refreshNow"
        >
          {{ $t('exportRefresh') }}
        </button>
      </div>

      <div
        data-test="export-announcement"
        aria-live="polite"
        aria-atomic="true"
        class="exports-announcement"
      >
        {{ announcementText }}
      </div>
    </div>
  </section>
</template>

<script>
import API from '@/components/api';
import modals from '@/components/modals';
import ExportStatusPanel from '@/components/export/ExportStatusPanel.vue';
import { createExportPoller } from '@/components/export/exportPoller';

const ACTIVE_STATES = ['queued', 'snapshotting', 'archiving', 'verifying'];

export default {
  name: 'Exports',
  components: { ExportStatusPanel },
  data() {
    return {
      latestEnvelope: null,
      loading: true,
      errorType: null,
      isDestroyed: false,
      poller: null,
      expiryTimer: null,
      expiryKey: null,
      firedExpiryKeys: [],
    };
  },
  computed: {
    latestAttempt() {
      return this.latestEnvelope ? this.latestEnvelope.latest_attempt : null;
    },
    downloadableJob() {
      return this.latestEnvelope ? this.latestEnvelope.downloadable_job : null;
    },
    hasJobs() {
      return this.latestAttempt !== null || this.downloadableJob !== null;
    },
    hasActiveJob() {
      return this.latestAttempt !== null && ACTIVE_STATES.includes(this.latestAttempt.state);
    },
    showEmpty() {
      return !this.loading && this.errorType === null && !this.hasJobs;
    },
    showNewJobHint() {
      return !this.loading && this.errorType === null && !this.hasActiveJob;
    },
    announcementSignature() {
      const current = this.latestAttempt;
      if (!current) return 'empty';
      const rounded = current.overall_percent === null
        ? 'unknown' : Math.floor(current.overall_percent);
      return [current.id, current.state, current.phase_label, rounded].join(':');
    },
    announcementText() {
      if (this.announcementSignature === 'empty') return '';
      const current = this.latestAttempt;
      const state = this.$t(`exportState${current.state.charAt(0).toUpperCase()}${current.state.slice(1)}`);
      const progress = current.overall_percent === null
        ? this.$t('exportProgressUnknown')
        : this.$t('exportProgress', { percent: Math.floor(current.overall_percent) });
      return this.$t('exportAnnouncement', {
        state,
        phase: current.phase_label,
        progress,
      });
    },
  },
  mounted() {
    this.poller = createExportPoller({
      fetchLatest: API.Export.fetchLatest,
      onData: this.receiveLatest,
      onError: this.receiveError,
      documentRef: document,
      stopOnTerminal: true,
    });
    this.poller.start();
  },
  beforeUnmount() {
    this.isDestroyed = true;
    this.clearExpiryTimer();
    if (this.poller) this.poller.stop();
  },
  methods: {
    clearExpiryTimer() {
      if (this.expiryTimer !== null) {
        clearTimeout(this.expiryTimer);
        this.expiryTimer = null;
      }
      this.expiryKey = null;
    },
    syncExpiryTimer(downloadableJob) {
      if (!downloadableJob || !downloadableJob.expires_at) {
        this.clearExpiryTimer();
        return;
      }
      const key = `${downloadableJob.id}:${downloadableJob.expires_at}`;
      if (key === this.expiryKey) return;
      this.clearExpiryTimer();
      this.expiryKey = key;
      if (this.firedExpiryKeys.includes(key)) return;
      const delay = Math.max(0, new Date(downloadableJob.expires_at).getTime() - Date.now());
      this.expiryTimer = setTimeout(() => {
        this.expiryTimer = null;
        if (this.isDestroyed || this.expiryKey !== key) return;
        this.firedExpiryKeys.push(key);
        this.poller.refresh();
      }, delay);
    },
    receiveLatest(envelope) {
      if (this.isDestroyed) return;
      this.latestEnvelope = envelope;
      this.loading = false;
      this.errorType = null;
      this.syncExpiryTimer(envelope.downloadable_job);
    },
    receiveError(error) {
      if (this.isDestroyed) return;
      this.loading = false;
      if (error && error.response && error.response.status === 401) {
        this.latestEnvelope = null;
        this.errorType = 'login';
        this.clearExpiryTimer();
        this.poller.stop();
        return;
      }
      if (error && error.message === 'invalid_export_contract') {
        this.latestEnvelope = null;
        this.errorType = 'contract';
        this.clearExpiryTimer();
        this.poller.stop();
        return;
      }
      this.errorType = 'temporary';
    },
    restartPolling() {
      if (this.isDestroyed || !this.poller) return;
      this.errorType = null;
      this.loading = this.latestEnvelope === null;
      this.poller.start();
    },
    refreshNow() {
      if (!this.poller || this.isDestroyed) return;
      if (this.errorType === 'login' || this.errorType === 'contract') {
        this.restartPolling();
      } else {
        this.poller.refresh();
      }
    },
    logIn() {
      modals.openLogin(this, () => this.restartPolling());
    },
  },
};
</script>

<style lang="scss" scoped>
.container {
  max-width: 100%;
  min-width: 0;
}

.exports-actions {
  display: flex;
  flex-wrap: wrap;
  gap: .5rem;
  margin-top: 1rem;
}

.exports-announcement {
  height: 1px;
  margin: -1px;
  overflow: hidden;
  padding: 0;
  position: absolute;
  width: 1px;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
}
</style>
