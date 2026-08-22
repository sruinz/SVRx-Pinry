#!/usr/bin/env node

const fs = require('fs');
const path = require('path');


function attribute(tag, name) {
  const match = tag.match(new RegExp(`${name}=["']([^"']+)["']`, 'i'));
  return match ? match[1] : null;
}

function localAssetPath(url) {
  if (!url || /^(?:[a-z]+:)?\/\//i.test(url) || url.startsWith('data:')) {
    return null;
  }
  return url.split(/[?#]/, 1)[0].replace(/^\.\//, '').replace(/^\//, '');
}

function validateBuildArtifacts(buildDirectory) {
  const html = fs.readFileSync(path.join(buildDirectory, 'index.html'), 'utf8');
  const manifest = JSON.parse(fs.readFileSync(
    path.join(buildDirectory, 'manifest.json'),
    'utf8',
  ));
  if (!/<html\b[^>]*\blang=["']ko["']/i.test(html)) {
    throw new Error('html_language_not_korean');
  }
  const noScriptMatch = html.match(/<noscript\b[^>]*>([\s\S]*?)<\/noscript>/i);
  const noScriptText = noScriptMatch
    ? noScriptMatch[1].replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim()
    : '';
  if (!noScriptText.includes('SVRx Pinry')
    || !noScriptText.includes('JavaScript')
    || !/[가-힣]/.test(noScriptText)) {
    throw new Error('noscript_message_not_korean');
  }

  const visibleHtml = html.replace(/<!--[\s\S]*?-->/g, '');
  ['favicon-light.png', 'favicon-dark.png'].forEach((filename) => {
    const escaped = filename.replace('.', '\\.');
    const themeIcon = new RegExp(
      `<link\\b(?=[^>]*\\brel=["']icon["'])(?=[^>]*\\bhref=["'][^"']*${escaped}["'])(?=[^>]*\\bmedia=["']\\(prefers-color-scheme: (?:light|dark)\\)["'])[^>]*>`,
      'i',
    );
    if (!themeIcon.test(visibleHtml)) {
      throw new Error(`theme_icon_not_visible:${filename}`);
    }
  });

  const iconUrls = [];
  const linkTags = visibleHtml.match(/<link\b[^>]*>/gi) || [];
  linkTags.forEach((tag) => {
    const rel = attribute(tag, 'rel');
    if (rel && rel.toLowerCase().includes('icon')) {
      iconUrls.push(attribute(tag, 'href'));
    }
  });
  const metaTags = visibleHtml.match(/<meta\b[^>]*>/gi) || [];
  metaTags.forEach((tag) => {
    if ((attribute(tag, 'name') || '').toLowerCase() === 'msapplication-tileimage') {
      iconUrls.push(attribute(tag, 'content'));
    }
  });
  (manifest.icons || []).forEach(icon => iconUrls.push(icon.src));

  iconUrls.forEach((url) => {
    const relativePath = localAssetPath(url);
    if (relativePath && !fs.existsSync(path.join(buildDirectory, relativePath))) {
      throw new Error(`missing_icon_asset:${relativePath}`);
    }
  });

  const manifestIconPaths = (manifest.icons || [])
    .map(icon => localAssetPath(icon.src))
    .sort();
  const approvedManifestIconPaths = [
    'img/icons/android-chrome-192x192.png',
    'img/icons/android-chrome-512x512.png',
  ];
  if (JSON.stringify(manifestIconPaths) !== JSON.stringify(approvedManifestIconPaths)) {
    throw new Error(`unexpected_manifest_icons:${manifestIconPaths.join(',')}`);
  }
}

try {
  const buildDirectory = path.resolve(process.argv[2] || 'dist');
  validateBuildArtifacts(buildDirectory);
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
}
