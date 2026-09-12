<template>
  <div class="board-modal">
    <div>
      <div class="modal-card" style="width: auto">
        <header class="modal-card-head">
          <p class="modal-card-title">{{ $t(UIMeta.title) }}</p>
        </header>
        <section class="modal-card-body">
          <div v-if="!isEdit">
            <FormField v-bind:label="$t('nameLabel')"
                       :type="createModel.form.name.type"
                       :message="createModel.form.name.error">
                <input class="input" :aria-label="$t('nameLabel')"
                  type="text"
                  v-model="createModel.form.name.value"
                  v-bind:placeholder="$t('boardNamePlaceholder')"
                  maxlength="128"
                  >
            </FormField>
            <FormField v-bind:label="$t('privacyOptionLabel')"
                       :type="createModel.form.private.type"
                       :message="createModel.form.private.error">
                <label class="checkbox"><input type="checkbox" v-model="createModel.form.private.value">
                    {{ $t("isPrivateCheckbox") }}
                </label>
              </FormField>
          </div>
          <div v-if="isEdit">
            <FormField v-bind:label="$t('nameLabel')"
                       :type="editModel.form.name.type"
                       :message="editModel.form.name.error">
                <input class="input" :aria-label="$t('nameLabel')"
                  type="text"
                  v-model="editModel.form.name.value"
                  v-bind:placeholder="$t('boardNamePlaceholder')"
                  maxlength="128"
                  >
            </FormField>
            <FormField v-bind:label="$t('privacyOptionLabel')"
                       :type="editModel.form.private.type"
                       :message="editModel.form.private.error">
                <label class="checkbox"><input type="checkbox"
                  v-model="editModel.form.private.value"
                  data-test="board-private-checkbox"
                >
                    {{ $t("isPrivateCheckbox") }}
                </label>
              </FormField>
            <p
              v-if="willResetPrivateCover"
              class="notification is-info"
              data-test="board-cover-publish-warning"
              role="status"
            >
              {{ $t('boardCoverPublishWarning') }}
            </p>
          </div>
        </section>
        <footer class="modal-card-foot">
          <button class="button" type="button" @click="$emit('close')">{{ $t("closeButton") }}</button>
          <button
            v-if="!isEdit"
            @click="createBoard"
            class="button is-primary">{{ $t("createBoardButton") }}
          </button>
          <button
            v-if="isEdit"
            @click="saveBoardChanges"
            class="button is-primary">{{ $t("saveChangesButton") }}
          </button>
        </footer>
      </div>
    </div>
  </div>
</template>

<script>
import FormField from './ui/FormField.vue';
import API from './api';
import ModelForm from './utils/ModelForm';
import bus from './utils/bus';

const fields = ['name', 'private'];

export default {
  components: { FormField },
  name: 'BoardEditModal',
  data() {
    const createModel = ModelForm.fromFields(fields);
    const editModel = ModelForm.fromFields(fields);
    return {
      UIMeta: {
        title: 'BoardCreateTitle',
      },
      createModel,
      editModel,
    };
  },
  props: {
    isEdit: {
      type: Boolean,
      default: false,
    },
    board: {
      type: Object,
      default() {
        return {};
      },
    },
  },
  created() {
    if (this.isEdit) {
      this.UIMeta.title = 'BoardEditTitle';
      this.editModel.assignToForm(this.board);
    } else {
      this.createModel.form.private.value = false;
    }
  },
  computed: {
    willResetPrivateCover() {
      return Boolean(
        this.isEdit
        && this.board.private
        && this.editModel.form.private.value === false
        && this.board.cover_pin_id
        && this.board.cover
        && this.board.cover.private,
      );
    },
  },
  methods: {
    saveBoardChanges() {
      const self = this;
      const promise = API.Board.saveChanges(
        this.board.id,
        this.editModel.asData(),
      );
      promise.then(
        (resp) => {
          self.$emit('boardSaved', resp);
          self.$emit('close');
        },
        (error) => {
          self.editModel.markFieldsAsDanger(error.response.data);
        },
      );
    },
    createBoard() {
      const self = this;
      const promise = API.Board.create(
        this.createModel.form.name.value,
        this.createModel.form.private.value,
      );
      promise.then(
        (resp) => {
          bus.bus.emit(bus.events.refreshBoards);
          self.$emit('boardCreated', resp);
          self.$emit('close');
        },
        (resp) => {
          self.createModel.markFieldsAsDanger(resp.data);
        },
      );
    },
  },
};
</script>
