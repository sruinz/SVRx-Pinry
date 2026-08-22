/* eslint-env jest */
import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawnSync } from 'child_process';


describe('production branding build validator', () => {
  let buildDirectory;

  beforeEach(() => {
    buildDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'svrx-pinry-build-'));
  });

  afterEach(() => {
    fs.rmSync(buildDirectory, { recursive: true, force: true });
  });

  function write(relativePath, content = '') {
    const target = path.join(buildDirectory, relativePath);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, content);
  }

  function validate() {
    return spawnSync(
      process.execPath,
      [path.resolve(__dirname, '../../scripts/verify-branding-build.js'), buildDirectory],
      { encoding: 'utf8' },
    );
  }

  it('accepts Korean HTML with visible theme icons and existing manifest assets', () => {
    write('favicon.png');
    write('favicon-light.png');
    write('favicon-dark.png');
    write('img/icons/android-chrome-192x192.png');
    write('img/icons/android-chrome-512x512.png');
    write('index.html', [
      '<html lang="ko"><head>',
      '<link rel="icon" href="/favicon.png">',
      '<link rel="icon" href="/favicon-light.png" media="(prefers-color-scheme: light)">',
      '<link rel="icon" href="/favicon-dark.png" media="(prefers-color-scheme: dark)">',
      '<link rel="apple-touch-icon" href="/img/icons/android-chrome-192x192.png">',
      '<meta name="msapplication-TileImage" content="/img/icons/android-chrome-192x192.png">',
      '</head></html>',
    ].join(''));
    write('manifest.json', JSON.stringify({
      icons: [
        { src: './img/icons/android-chrome-192x192.png' },
        { src: './img/icons/android-chrome-512x512.png' },
      ],
    }));

    const result = validate();

    expect(result).toMatchObject({ status: 0, stderr: '' });
  });

  it('rejects a theme icon hidden inside an IE conditional comment', () => {
    write('favicon.png');
    write('favicon-light.png');
    write('favicon-dark.png');
    write('manifest.json', JSON.stringify({ icons: [] }));
    write('index.html', [
      '<html lang="ko"><head>',
      '<!--[if IE]><link rel="icon" href="/favicon-light.png" media="(prefers-color-scheme: light)"><![endif]-->',
      '<link rel="icon" href="/favicon-dark.png" media="(prefers-color-scheme: dark)">',
      '<link rel="icon" href="/favicon.png">',
      '</head></html>',
    ].join(''));

    const result = validate();

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('theme_icon_not_visible:favicon-light.png');
  });

  it('rejects local icon URLs whose files are missing', () => {
    write('favicon.png');
    write('favicon-light.png');
    write('favicon-dark.png');
    write('index.html', [
      '<html lang="ko"><head>',
      '<link rel="icon" href="/favicon.png">',
      '<link rel="icon" href="/favicon-light.png" media="(prefers-color-scheme: light)">',
      '<link rel="icon" href="/favicon-dark.png" media="(prefers-color-scheme: dark)">',
      '<link rel="mask-icon" href="/img/icons/safari-pinned-tab.svg">',
      '</head></html>',
    ].join(''));
    write('manifest.json', JSON.stringify({
      icons: [{ src: './img/icons/android-chrome-maskable-192x192.png' }],
    }));

    const result = validate();

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('missing_icon_asset:img/icons/safari-pinned-tab.svg');
  });

  it('rejects manifest icons outside the approved 192 and 512 assets', () => {
    write('favicon.png');
    write('favicon-light.png');
    write('favicon-dark.png');
    write('img/icons/android-chrome-maskable-192x192.png');
    write('index.html', [
      '<html lang="ko"><head>',
      '<link rel="icon" href="/favicon.png">',
      '<link rel="icon" href="/favicon-light.png" media="(prefers-color-scheme: light)">',
      '<link rel="icon" href="/favicon-dark.png" media="(prefers-color-scheme: dark)">',
      '</head></html>',
    ].join(''));
    write('manifest.json', JSON.stringify({
      icons: [{ src: './img/icons/android-chrome-maskable-192x192.png' }],
    }));

    const result = validate();

    expect(result.status).toBe(1);
    expect(result.stderr).toContain(
      'unexpected_manifest_icons:img/icons/android-chrome-maskable-192x192.png',
    );
  });
});
