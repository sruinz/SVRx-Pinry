/* eslint-env jest */
import fs from 'fs';
import path from 'path';
import zlib from 'zlib';
import { createI18n } from 'vue-i18n';
import { shallowMount } from '@vue/test-utils';

import PHeader from '@/components/PHeader.vue';
import ko from '@/components/utils/i18n/locales/ko.json';


function paeth(left, up, upLeft) {
  const prediction = left + up - upLeft;
  const leftDistance = Math.abs(prediction - left);
  const upDistance = Math.abs(prediction - up);
  const upLeftDistance = Math.abs(prediction - upLeft);
  if (leftDistance <= upDistance && leftDistance <= upLeftDistance) {
    return left;
  }
  return upDistance <= upLeftDistance ? up : upLeft;
}


function decodeRgbaPng(assetPath) {
  const input = fs.readFileSync(assetPath);
  const signature = '89504e470d0a1a0a';
  expect(input.subarray(0, 8).toString('hex')).toBe(signature);

  let offset = 8;
  let header;
  const compressed = [];
  while (offset < input.length) {
    const length = input.readUInt32BE(offset);
    const type = input.subarray(offset + 4, offset + 8).toString('ascii');
    const value = input.subarray(offset + 8, offset + 8 + length);
    if (type === 'IHDR') {
      header = {
        width: value.readUInt32BE(0),
        height: value.readUInt32BE(4),
        bitDepth: value[8],
        colorType: value[9],
      };
    }
    if (type === 'IDAT') {
      compressed.push(value);
    }
    offset += length + 12;
  }

  expect(header).toMatchObject({ bitDepth: 8, colorType: 6 });
  const rowBytes = header.width * 4;
  const filtered = zlib.inflateSync(Buffer.concat(compressed));
  const pixels = Buffer.alloc(rowBytes * header.height);
  let readOffset = 0;
  for (let y = 0; y < header.height; y += 1) {
    const filter = filtered[readOffset];
    readOffset += 1;
    for (let x = 0; x < rowBytes; x += 1) {
      const value = filtered[readOffset];
      readOffset += 1;
      const left = x >= 4 ? pixels[y * rowBytes + x - 4] : 0;
      const up = y > 0 ? pixels[(y - 1) * rowBytes + x] : 0;
      const upLeft = y > 0 && x >= 4 ? pixels[(y - 1) * rowBytes + x - 4] : 0;
      const index = y * rowBytes + x;
      if (filter === 0) pixels[index] = value;
      if (filter === 1) pixels[index] = (value + left) % 256;
      if (filter === 2) pixels[index] = (value + up) % 256;
      if (filter === 3) pixels[index] = (value + Math.floor((left + up) / 2)) % 256;
      if (filter === 4) pixels[index] = (value + paeth(left, up, upLeft)) % 256;
    }
  }
  return { ...header, pixels };
}


function alphaBounds({ width, height, pixels }) {
  const bounds = {
    left: width, top: height, right: -1, bottom: -1,
  };
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      if (pixels[(y * width + x) * 4 + 3] > 0) {
        bounds.left = Math.min(bounds.left, x);
        bounds.top = Math.min(bounds.top, y);
        bounds.right = Math.max(bounds.right, x);
        bounds.bottom = Math.max(bounds.bottom, y);
      }
    }
  }
  return bounds;
}


function opaqueMeanLuminance({ width, height, pixels }) {
  let count = 0;
  let total = 0;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const offset = (y * width + x) * 4;
      if (pixels[offset + 3] === 255) {
        total += (0.2126 * pixels[offset])
          + (0.7152 * pixels[offset + 1])
          + (0.0722 * pixels[offset + 2]);
        count += 1;
      }
    }
  }
  return total / count;
}


describe('SVRx Pinry branding', () => {
  let initializeUser;

  beforeEach(() => {
    initializeUser = jest.spyOn(PHeader.methods, 'initializeUser')
      .mockImplementation(() => {});
  });

  afterEach(() => {
    initializeUser.mockRestore();
  });

  it('다크 헤더의 홈 링크에 밝은 은색 로고를 표시한다', () => {
    const wrapper = shallowMount(PHeader, {
      global: {
        directives: { masonry: {}, 'masonry-tile': {} },
        stubs: ['router-link'],
        plugins: [createI18n({ legacy: false, locale: 'ko', messages: { ko } })],
      },
    });

    const lockup = wrapper.find('[data-test="brand-lockup"]');
    expect(lockup.attributes('href')).toBe('/');
    expect(lockup.find('img').attributes()).toMatchObject({
      alt: '',
      height: '32',
    });
    expect(lockup.find('picture').exists()).toBe(false);
    expect(lockup.find('img').attributes('src')).toContain('svrx-pinry-dark-ui.png');
    expect(wrapper.find('[data-test="brand-name"]').text()).toBe('SVRx Pinry');
  });

  it.each([
    ['src/assets/svrx-pinry-dark-ui.png', 512, 472, 20],
    ['src/assets/svrx-pinry-light-ui.png', 512, 472, 20],
    ['public/favicon-dark.png', 128, 118, 5],
    ['public/favicon-light.png', 128, 118, 5],
    ['public/favicon.png', 48, 44, 2],
    ['public/img/icons/android-chrome-192x192.png', 192, 176, 8],
    ['public/img/icons/android-chrome-512x512.png', 512, 472, 20],
  ])('keeps a centered four-percent alpha margin for %s', (
    assetRelativePath,
    size,
    contentSize,
    margin,
  ) => {
    const image = decodeRgbaPng(path.resolve(__dirname, '../..', assetRelativePath));
    const bounds = alphaBounds(image);

    expect(image).toMatchObject({
      width: size, height: size, bitDepth: 8, colorType: 6,
    });
    expect(bounds.right - bounds.left + 1).toBeGreaterThanOrEqual(contentSize - 1);
    expect(bounds.right - bounds.left + 1).toBeLessThanOrEqual(contentSize + 1);
    expect(bounds.bottom - bounds.top + 1).toBeGreaterThanOrEqual(contentSize - 1);
    expect(bounds.bottom - bounds.top + 1).toBeLessThanOrEqual(contentSize + 1);
    expect(Math.abs(bounds.left - margin)).toBeLessThanOrEqual(1);
    expect(Math.abs(bounds.top - margin)).toBeLessThanOrEqual(1);
    expect(Math.abs(size - 1 - bounds.right - margin)).toBeLessThanOrEqual(1);
    expect(Math.abs(size - 1 - bounds.bottom - margin)).toBeLessThanOrEqual(1);
  });

  it('keeps the dark UI artwork materially brighter than the light UI artwork', () => {
    const darkUi = decodeRgbaPng(path.resolve(
      __dirname,
      '../../src/assets/svrx-pinry-dark-ui.png',
    ));
    const lightUi = decodeRgbaPng(path.resolve(
      __dirname,
      '../../src/assets/svrx-pinry-light-ui.png',
    ));

    expect(opaqueMeanLuminance(darkUi) - opaqueMeanLuminance(lightUi))
      .toBeGreaterThan(100);
  });
});
