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

  function koreanDocument(head, noScript = 'SVRx Pinry를 사용하려면 JavaScript를 활성화해 주세요.') {
    return [
      '<html lang="ko"><head>',
      ...head,
      '</head><body><noscript><strong>',
      noScript,
      '</strong></noscript></body></html>',
    ].join('');
  }

  it('accepts Korean HTML with visible theme icons and existing manifest assets', () => {
    write('favicon.png');
    write('favicon-light.png');
    write('favicon-dark.png');
    write('img/icons/android-chrome-192x192.png');
    write('img/icons/android-chrome-512x512.png');
    write('index.html', koreanDocument([
      '<link rel="icon" href="/favicon.png">',
      '<link rel="icon" href="/favicon-light.png" media="(prefers-color-scheme: light)">',
      '<link rel="icon" href="/favicon-dark.png" media="(prefers-color-scheme: dark)">',
      '<link rel="apple-touch-icon" href="/img/icons/android-chrome-192x192.png">',
      '<meta name="msapplication-TileImage" content="/img/icons/android-chrome-192x192.png">',
    ]));
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
    write('index.html', koreanDocument([
      '<!--[if IE]><link rel="icon" href="/favicon-light.png" media="(prefers-color-scheme: light)"><![endif]-->',
      '<link rel="icon" href="/favicon-dark.png" media="(prefers-color-scheme: dark)">',
      '<link rel="icon" href="/favicon.png">',
    ]));

    const result = validate();

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('theme_icon_not_visible:favicon-light.png');
  });

  it('rejects local icon URLs whose files are missing', () => {
    write('favicon.png');
    write('favicon-light.png');
    write('favicon-dark.png');
    write('index.html', koreanDocument([
      '<link rel="icon" href="/favicon.png">',
      '<link rel="icon" href="/favicon-light.png" media="(prefers-color-scheme: light)">',
      '<link rel="icon" href="/favicon-dark.png" media="(prefers-color-scheme: dark)">',
      '<link rel="mask-icon" href="/img/icons/safari-pinned-tab.svg">',
    ]));
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
    write('index.html', koreanDocument([
      '<link rel="icon" href="/favicon.png">',
      '<link rel="icon" href="/favicon-light.png" media="(prefers-color-scheme: light)">',
      '<link rel="icon" href="/favicon-dark.png" media="(prefers-color-scheme: dark)">',
    ]));
    write('manifest.json', JSON.stringify({
      icons: [{ src: './img/icons/android-chrome-maskable-192x192.png' }],
    }));

    const result = validate();

    expect(result.status).toBe(1);
    expect(result.stderr).toContain(
      'unexpected_manifest_icons:img/icons/android-chrome-maskable-192x192.png',
    );
  });

  it('rejects an English-only no-JavaScript message in Korean HTML', () => {
    write('favicon.png');
    write('favicon-light.png');
    write('favicon-dark.png');
    write('img/icons/android-chrome-192x192.png');
    write('img/icons/android-chrome-512x512.png');
    write('index.html', koreanDocument([
      '<link rel="icon" href="/favicon.png">',
      '<link rel="icon" href="/favicon-light.png" media="(prefers-color-scheme: light)">',
      '<link rel="icon" href="/favicon-dark.png" media="(prefers-color-scheme: dark)">',
    ], 'SVRx Pinry requires JavaScript.'));
    write('manifest.json', JSON.stringify({
      icons: [
        { src: './img/icons/android-chrome-192x192.png' },
        { src: './img/icons/android-chrome-512x512.png' },
      ],
    }));

    const result = validate();

    expect(result.status).toBe(1);
    expect(result.stderr).toContain('noscript_message_not_korean');
  });
});
