<template>
  <div class="add2board-modal">
    <div>
      <div class="modal-card" style="width: auto">
        <header class="modal-card-head">
          <p class="modal-card-title">{{ $t("Add2BoardModalCardTitle") }}</p>
        </header>
        <section class="modal-card-body">
          <div class="columns">
            <div class="column">
              <FileUpload
                :previewImageURL="pin.url"
              ></FileUpload>
            </div>
            <div class="column">
              <FilterSelect
                :allOptions="boardOptions"
                v-on:boardCreated="onBoardCreated"
                v-on:selected="onSelectBoard"
              ></FilterSelect>
              <p
                v-if="loadError"
                class="help is-danger"
                data-test="add2board-membership-error"
                role="alert"
              >{{ $t("pinBoardMembershipLoadError") }}</p>
            </div>
          </div>
        </section>
        <footer class="modal-card-foot">
          <button class="button" type="button" @click="$parent.close()">{{ $t("closeButton") }}</button>
          <button
            type="button"
            :disabled="!canSubmit"
            @click="doAdd2Board"
            class="button is-primary">{{ $t("Add2BoardModalCardButton") }}
          </button>
        </footer>
      </div>
    </div>
  </div>
</template>

<script>
import API from '../api';
import FileUpload from './FileUpload.vue';
import FilterSelect from './FilterSelect.vue';


export default {
  name: 'Add2Board',
  props: ['pin', 'username'],
  components: {
    FileUpload,
    FilterSelect,
  },
  data() {
    return {
      boardOptions: [],
      boardIds: [],
      createdBoardIds: [],
      createdBoardOptions: [],
      loadingBoards: true,
      loadError: false,
      submitInFlight: false,
      completedBoardIds: [],
      componentAlive: true,
    };
  },
  computed: {
    validBoardIds() {
      return this.filterAddableBoardIds(this.boardIds);
    },
    canSubmit() {
      return this.componentAlive
        && !this.loadingBoards
        && !this.loadError
        && !this.submitInFlight
        && this.validBoardIds.length > 0;
    },
  },
  created() {
    this.fetchBoardList();
  },
  beforeDestroy() {
    this.componentAlive = false;
  },
  methods: {
    doAdd2Board() {
      if (!this.canSubmit) {
        return Promise.resolve();
      }
      const boardIds = this.filterAddableBoardIds(this.boardIds);
      if (boardIds.length === 0) {
        return Promise.resolve();
      }
      this.submitInFlight = true;
      const promises = boardIds.map(
        boardId => API.Board.addToBoard(boardId, [this.pin.id]).then(
          () => ({ boardId, succeeded: true }),
          () => ({ boardId, succeeded: false }),
        ),
      );
      return Promise.all(promises).then((results) => {
        if (!this.componentAlive) {
          return;
        }
        const succeededBoardIds = results
          .filter(result => result.succeeded)
          .map(result => result.boardId);
        const failedBoardIds = results
          .filter(result => !result.succeeded)
          .map(result => result.boardId);
        succeededBoardIds.forEach((boardId) => {
          if (!this.completedBoardIds.includes(boardId)) {
            this.completedBoardIds.push(boardId);
          }
        });
        const succeededValues = new Set(succeededBoardIds);
        this.createdBoardIds = this.createdBoardIds.filter(
          boardId => !succeededValues.has(boardId),
        );
        this.createdBoardOptions = this.markOptionsIncluded(
          this.createdBoardOptions, succeededValues,
        );
        this.boardOptions = this.markOptionsIncluded(
          this.boardOptions, succeededValues,
        );
        this.boardIds = failedBoardIds;
        this.submitInFlight = false;
        if (failedBoardIds.length === 0) {
          this.$buefy.toast.open('Succeed to add pin to boards');
          this.$parent.close();
        } else {
          this.$buefy.toast.open(
            {
              message: 'Failed to add pin to boards',
              type: 'is-danger',
            },
          );
        }
      });
    },
    onSelectBoard(boardIds) {
      this.boardIds = this.filterAddableBoardIds(boardIds);
    },
    onBoardCreated(board) {
      const boardId = Number(board.id);
      if (Number.isSafeInteger(boardId)
          && boardId > 0
          && !this.createdBoardIds.includes(boardId)) {
        this.createdBoardIds.push(boardId);
        this.createdBoardOptions.unshift({
          name: board.name,
          value: boardId,
          disabled: false,
          displayName: board.name,
        });
        this.boardOptions = this.mergeCreatedBoardOptions(
          this.boardOptions,
        );
      }
    },
    markOptionsIncluded(options, includedValues) {
      return options.map((option) => {
        if (!includedValues.has(Number(option.value))) {
          return option;
        }
        return {
          ...option,
          disabled: true,
          displayName: `${option.name} (${this.$t('pinBoardAlreadyIncluded')})`,
        };
      });
    },
    mergeCreatedBoardOptions(options) {
      const knownValues = new Set(
        options.map(option => String(option.value)),
      );
      return this.createdBoardOptions
        .filter(option => !knownValues.has(String(option.value)))
        .concat(options);
    },
    filterAddableBoardIds(boardIds) {
      const completedBoardIds = new Set(this.completedBoardIds);
      const allowedBoardIds = new Set(this.createdBoardIds);
      this.boardOptions.forEach((option) => {
        const boardId = Number(option.value);
        if (option.disabled !== true && !completedBoardIds.has(boardId)) {
          allowedBoardIds.add(boardId);
        }
      });
      const selectedBoardIds = Array.isArray(boardIds) ? boardIds : [];
      const seenBoardIds = new Set();
      return selectedBoardIds.reduce((result, value) => {
        const boardId = Number(value);
        if (Number.isSafeInteger(boardId)
            && boardId > 0
            && !completedBoardIds.has(boardId)
            && allowedBoardIds.has(boardId)
            && !seenBoardIds.has(boardId)) {
          seenBoardIds.add(boardId);
          result.push(boardId);
        }
        return result;
      }, []);
    },
    fetchBoardList() {
      this.loadingBoards = true;
      this.loadError = false;
      return API.Pin.fetchBoardMemberships(this.pin.id).then(
        (resp) => {
          if (!this.componentAlive) {
            return;
          }
          if (!resp || !resp.data || !Array.isArray(resp.data.boards)) {
            throw new Error('Invalid board membership response');
          }
          const boardOptions = resp.data.boards.map((board) => {
            const disabled = board.contains_pin === true;
            return {
              name: board.name,
              value: board.id,
              disabled,
              displayName: disabled
                ? `${board.name} (${this.$t('pinBoardAlreadyIncluded')})`
                : board.name,
            };
          });
          this.boardOptions = this.mergeCreatedBoardOptions(boardOptions);
          this.loadingBoards = false;
        },
      ).catch(
        () => {
          if (!this.componentAlive) {
            return;
          }
          this.boardOptions = [];
          this.boardIds = [];
          this.loadingBoards = false;
          this.loadError = true;
        },
      );
    },
  },
};
</script>
