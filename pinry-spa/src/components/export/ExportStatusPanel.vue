<template>
  <div class="export-status-panel">
    <article
      v-for="card in cards"
      :key="card.job.id"
      class="box export-status-card"
      :data-test="card.kind === 'latest' ? 'export-latest-card' : 'export-previous-card'"
    >
      <h2 class="subtitle">
        {{ $t(card.kind === 'latest' ? 'exportCurrentTitle' : 'exportPreviousTitle') }}
      </h2>
      <p class="export-break-anywhere" data-test="export-state">
        {{ $t(stateKey(card.job.state)) }}
      </p>
      <p class="export-break-anywhere" data-test="export-phase">
        {{ $t('exportPhase', { value: card.job.phase_label }) }}
      </p>
      <p
        v-if="card.job.state === 'queued'"
        data-test="export-worker-waiting"
      >
        {{ $t('exportWorkerWaiting') }}
      </p>

      <div
        class="export-progress"
        data-test="export-progress"
        role="progressbar"
        aria-valuemin="0"
        aria-valuemax="100"
        :aria-valuenow="card.job.overall_percent"
        :aria-valuetext="progressText(card.job)"
      >
        <span
          v-if="card.job.overall_percent !== null"
          class="export-progress__fill"
          :style="{ width: `${card.job.overall_percent}%` }"
        ></span>
        <span class="export-progress__text">{{ progressText(card.job) }}</span>
      </div>

      <ul class="export-counters" data-test="export-counters">
        <li data-test="export-included-total">
          {{ $t('exportIncludedTotal', { count: card.job.counters.included_total }) }}
        </li>
        <li data-test="export-excluded-total">
          {{ $t('exportExcludedTotal', { count: card.job.counters.excluded_total }) }}
        </li>
        <li data-test="export-bytes">
          {{ $t('exportBytesProgress', {
            done: card.job.counters.bytes_done,
            total: card.job.counters.bytes_total,
          }) }}
        </li>
      </ul>

      <p v-if="card.job.heartbeat_at" data-test="export-heartbeat">
        {{ $t('exportHeartbeat', { seconds: heartbeatAge(card.job.heartbeat_at) }) }}
      </p>
      <p data-test="export-resume-count">
        {{ $t('exportResumeCount', { count: card.job.resume_count }) }}
      </p>

      <div v-if="card.job.error" class="export-error">
        <p class="export-break-anywhere" data-test="export-error-code">
          {{ $t('exportErrorCode', { code: card.job.error.code }) }}
        </p>
        <p class="export-break-anywhere" data-test="export-error-message">
          {{ card.job.error.message }}
        </p>
        <p v-if="card.job.error.retryable" data-test="export-retryable">
          {{ $t('exportRetryableHint') }}
        </p>
      </div>

      <p
        v-if="card.kind === 'previous' && card.job.expires_at"
        class="export-break-anywhere"
        data-test="export-previous-expires"
      >
        {{ $t('exportExpiresAt', { value: card.job.expires_at }) }}
      </p>
      <div
        v-if="card.job.download_url"
        class="export-actions"
        data-test="export-actions"
      >
        <a
          class="button is-primary export-break-anywhere"
          :data-test="card.kind === 'previous' && latestAttempt
            ? 'export-previous-download' : 'export-download'"
          :href="card.job.download_url"
        >
          {{ $t('exportDownload') }}
        </a>
      </div>
    </article>
  </div>
</template>

<script>
const STATE_KEYS = {
  queued: 'exportStateQueued',
  snapshotting: 'exportStateSnapshotting',
  archiving: 'exportStateArchiving',
  verifying: 'exportStateVerifying',
  complete: 'exportStateComplete',
  failed: 'exportStateFailed',
  expired: 'exportStateExpired',
};

export default {
  name: 'ExportStatusPanel',
  props: {
    latestAttempt: { type: Object, default: null },
    downloadableJob: { type: Object, default: null },
  },
  computed: {
    cards() {
      const cards = [];
      if (this.latestAttempt) cards.push({ kind: 'latest', job: this.latestAttempt });
      if (
        this.downloadableJob
        && (!this.latestAttempt || this.downloadableJob.id !== this.latestAttempt.id)
      ) {
        cards.push({ kind: 'previous', job: this.downloadableJob });
      }
      return cards;
    },
  },
  methods: {
    heartbeatAge(value) {
      return Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
    },
    progressText(job) {
      if (job.overall_percent === null) return this.$t('exportProgressUnknown');
      return this.$t('exportProgress', { percent: job.overall_percent });
    },
    stateKey(state) {
      return STATE_KEYS[state];
    },
  },
};
</script>

<style lang="scss" scoped>
.export-status-panel,
.export-status-card {
  max-width: 100%;
  min-width: 0;
}

.export-status-card,
.export-break-anywhere {
  overflow-wrap: anywhere;
}

.export-progress {
  background: #e9ecef;
  border-radius: .25rem;
  margin: 1rem 0;
  min-height: 2rem;
  overflow: hidden;
  position: relative;
}

.export-progress__fill {
  background: #3273dc;
  bottom: 0;
  left: 0;
  position: absolute;
  top: 0;
}

.export-progress__text {
  display: block;
  padding: .25rem .5rem;
  position: relative;
}

.export-counters {
  display: grid;
  gap: .75rem;
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.export-actions {
  display: flex;
  flex-wrap: wrap;
  gap: .5rem;
  margin-top: 1rem;
}

@media screen and (max-width: 480px) {
  .export-counters {
    grid-template-columns: minmax(0, 1fr);
  }

  .export-actions > * {
    max-width: 100%;
  }
}
</style>
