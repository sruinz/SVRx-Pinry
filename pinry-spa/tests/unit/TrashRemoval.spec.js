/* eslint-env jest */
import VueI18n from 'vue-i18n';
import VueRouter from 'vue-router';
import { createLocalVue, shallowMount } from '@vue/test-utils';

import API from '@/components/api';
import PHeader from '@/components/PHeader.vue';
import router from '@/router';
import en from '@/components/utils/i18n/locales/en.json';
import fr from '@/components/utils/i18n/locales/fr.json';
import zh from '@/components/utils/i18n/locales/zh.json';

const removedKeys = [
  'trashLink', 'trashTitle', 'trashLoading', 'trashEmpty', 'trashLoadError',
  'trashRestoreButton', 'trashPermanentDeleteButton', 'trashRestoreSuccess',
  'trashRestoreError', 'trashPermanentDeleteConfirm',
  'trashPermanentDeleteSuccess', 'trashPermanentDeleteError',
  'pinMoveToTrashConfirm', 'pinMovedToTrash', 'pinMoveToTrashError',
];
const directDeleteKeys = ['pinDeleteConfirm', 'pinDeleted', 'pinDeleteError'];
const directDeleteTranslations = {
  en: ['Delete this Pin?', 'Pin deleted', 'Failed to delete Pin'],
  fr: [
    'Supprimer cette fiche ?', 'Fiche supprimée',
    'Impossible de supprimer la fiche',
  ],
  zh: ['删除此 Pin？', 'Pin 已删除', '无法删除 Pin'],
};

describe('Trash removal', () => {
  it('leaves /trash to the PageNotFound route', () => {
    expect(router.match('/trash').name).toBe('PageNotFound');
  });

  it('does not show a trash link to logged-in users', async () => {
    const localVue = createLocalVue();
    localVue.use(VueI18n);
    localVue.use(VueRouter);
    const initializeUser = jest.spyOn(PHeader.methods, 'initializeUser')
      .mockImplementation(() => {});
    const wrapper = shallowMount(PHeader, {
      localVue,
      router,
      i18n: new VueI18n({ locale: 'en', messages: { en } }),
      stubs: ['b-icon'],
    });

    await wrapper.setData({
      user: { loggedIn: true, meta: { username: 'owner' } },
    });

    expect(wrapper.find('[data-test="trash-link"]').exists()).toBe(false);
    initializeUser.mockRestore();
  });

  it('does not expose trash-only API helpers', () => {
    expect(API.Pin.fetchTrash).toBeUndefined();
    expect(API.Pin.restore).toBeUndefined();
    expect(API.Pin.deletePermanently).toBeUndefined();
  });

  it.each([['en', en], ['fr', fr], ['zh', zh]])(
    'uses direct deletion translations without trash-only keys for %s',
    (localeName, locale) => {
      removedKeys.forEach((key) => {
        expect(locale[key]).toBeUndefined();
      });
      directDeleteKeys.forEach((key) => {
        expect(locale[key]).toEqual(expect.any(String));
        expect(locale[key]).not.toBe('');
      });
      expect(directDeleteKeys.map(key => locale[key]))
        .toEqual(directDeleteTranslations[localeName]);
      expect(Object.values(locale).join('\n')).not.toContain('Immich');
    },
  );
});
