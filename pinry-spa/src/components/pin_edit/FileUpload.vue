<template>
  <div class="image-upload">
    <div
      v-show="previewImage !== null"
      class="has-text-centered is-center preview">
      <img :src="previewImage">
    </div>
    <div v-show="previewImage === null">
      <FormField>
        <label class="file-drop" data-test="file-drop" @dragover.prevent @drop.prevent="selectFiles($event.dataTransfer.files)">
          <input type="file" accept="image/*" @change="selectFiles($event.target.files)">
          <section class="section">
            <div class="content has-text-centered">
              <p>
                <i aria-hidden="true" class="mdi mdi-upload"></i>
              </p>
              <p>{{ $t("FileUploadDescription") }}</p>
            </div>
          </section>
        </label>
      </FormField>
    </div>
  </div>
</template>

<script>
import FormField from '../ui/FormField.vue';

export default {
  components: { FormField },
  name: 'FileUpload',
  data() {
    return {
      dropFile: null,
      objectUrl: null,
    };
  },
  props: {
    previewImageURL: {
      type: String,
      default: null,
    },
  },
  watch: {
    dropFile(newFile) {
      this.releaseObjectUrl();
      if (newFile !== null) {
        this.objectUrl = URL.createObjectURL(newFile);
      }
      this.$emit('imageSelected', newFile);
    },
  },
  beforeUnmount() {
    this.releaseObjectUrl();
  },
  computed: {
    previewImage() {
      if (this.previewExists()) {
        return this.previewImageURL;
      }
      if (this.objectUrl !== null) {
        return this.objectUrl;
      }
      return null;
    },
  },
  methods: {
    selectFiles(files) {
      const file = files && files[0];
      if (file && !file.type.startsWith('image/')) return;
      this.dropFile = file || null;
    },
    releaseObjectUrl() {
      if (this.objectUrl !== null) {
        URL.revokeObjectURL(this.objectUrl);
        this.objectUrl = null;
      }
    },
    previewExists() {
      return this.previewImageURL !== null && this.previewImageURL !== '';
    },
  },
};
</script>

<style lang="scss" scoped>
@use '../utils/pin' as *;
@use '../utils/loader' as *;

.preview > img {
  width: $pin-preview-width;
  height: auto;
  @include loader('../../assets/loader.gif');
}
.file-drop { display: block; border: 2px dashed #b5b5b5; border-radius: 6px; cursor: pointer; }
.file-drop input { max-width: 100%; }

</style>
