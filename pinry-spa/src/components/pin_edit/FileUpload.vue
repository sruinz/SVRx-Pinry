<template>
  <div class="image-upload">
    <div
      v-show="previewImage !== null"
      class="has-text-centered is-center preview">
      <img :src="previewImage">
    </div>
    <div v-show="previewImage === null">
      <b-field>
        <b-upload v-model="dropFile"
                  accept="image/*"
                  drag-drop>
          <section class="section">
            <div class="content has-text-centered">
              <p>
                <b-icon
                  icon="upload"
                  size="is-medium">
                </b-icon>
              </p>
              <p>{{ $t("FileUploadDescription") }}</p>
            </div>
          </section>
        </b-upload>
      </b-field>
    </div>
  </div>
</template>

<script>
export default {
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
  beforeDestroy() {
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
@import '../utils/pin';
@import '../utils/loader';

.preview > img {
  width: $pin-preview-width;
  height: auto;
  @include loader('../../assets/loader.gif');
}

</style>
