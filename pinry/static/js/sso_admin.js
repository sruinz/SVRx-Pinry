/* 관리자 가이드는 주소를 표시할 뿐 인증 설정을 저장하지 않는다. */
(function () {
  'use strict';
  function initialize() {
    const config = document.getElementById('sso-provider-guides');
    if (!config) return;
    const guides = JSON.parse(config.textContent);
    const selfHosted = ['authentik', 'synology', 'oidc'];
    const kindSelect = document.getElementById('id_kind');
    const base = document.getElementById('id_public_base_url');
    if (base) {
      if (!base.value && window.location.protocol === 'https:') base.value = window.location.origin;
      function updateForm() {
        const kind = kindSelect ? kindSelect.value : base.dataset.providerKind;
        const guide = guides[kind];
        if (!guide) return;
        ['issuer', 'discovery_url', 'tenant_id', 'internal_cidrs'].forEach(function (name) {
          const field = document.getElementById('id_' + name);
          const visible = name === 'tenant_id' ? kind === 'microsoft' : selfHosted.includes(kind);
          if (field) {
            field.closest('.form-row').hidden = !visible;
            field.disabled = !visible;
          }
        });
        [['issuer', 'issuer_example'], ['discovery_url', 'discovery_example'], ['client_id', 'client_example']].forEach(function (pair) {
          document.getElementById('id_' + pair[0]).placeholder = guide[pair[1]] || '';
        });
        document.getElementById('id_allowed_endpoint_origins').placeholder = guide.origin_example;
      }
      updateForm();
      if (kindSelect) kindSelect.addEventListener('change', updateForm);
    }
    if (!document.getElementById('sso-setup-guide')) return;
    const kind = document.getElementById('guide-kind');
    const issuer = document.getElementById('guide-issuer');
    const tenant = document.getElementById('guide-tenant');
    const discoveryInput = document.getElementById('guide-discovery-input');
    const discovery = document.getElementById('guide-discovery');
    function updateGuide() {
      const info = guides[kind.value];
      const hosted = selfHosted.includes(kind.value);
      document.getElementById('guide-issuer-row').hidden = !hosted;
      document.getElementById('guide-discovery-row').hidden = !hosted;
      document.getElementById('guide-tenant-row').hidden = kind.value !== 'microsoft';
      issuer.placeholder = info.issuer_example || '';
      discoveryInput.placeholder = info.discovery_example || '';
      let address = '';
      let example = false;
      if (kind.value === 'google') address = 'https://accounts.google.com/.well-known/openid-configuration';
      else if (kind.value === 'microsoft') {
        if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(tenant.value.trim())) {
          address = 'https://login.microsoftonline.com/' + tenant.value.trim().toLowerCase() + '/v2.0/.well-known/openid-configuration';
        }
      } else if (hosted) {
        address = discoveryInput.value.trim() || (issuer.value.trim() ? issuer.value.trim().replace(/\/+$/, '') + '/.well-known/openid-configuration' : '');
        if (!address) { address = info.discovery_example; example = true; }
      }
      let valid = false;
      try {
        const parsed = new URL(address);
        valid = parsed.protocol === 'https:' && !parsed.username && !parsed.password && !parsed.search && !parsed.hash;
      } catch (_) { /* 미완성 입력은 복사하지 않는다. */ }
      discovery.textContent = valid ? address : (kind.value === 'github' ? '해당 없음 — GitHub OAuth는 Discovery URL을 사용하지 않습니다.' : '올바른 HTTPS 주소 또는 테넌트 UUID를 입력하세요.');
      document.getElementById('guide-discovery-label').textContent = example ? 'Discovery URL 예시 (실제 IdP 주소로 바꾸세요)' : 'Discovery URL (입력값 기준)';
      document.getElementById('guide-discovery-copy').hidden = !valid || example;
      document.getElementById('guide-note').textContent = info.note;
      const list = document.getElementById('guide-steps');
      list.replaceChildren();
      info.steps.forEach(function (step) { const item = document.createElement('li'); item.textContent = step; list.appendChild(item); });
      document.getElementById('guide-docs').href = info.docs;
    }
    [kind, issuer, tenant, discoveryInput].forEach(function (field) { field.addEventListener('input', updateGuide); });
    updateGuide();
    document.querySelectorAll('[data-copy]').forEach(function (button) {
      button.addEventListener('click', async function () {
        const output = document.getElementById(button.dataset.copy);
        const status = document.getElementById('guide-copy-status');
        try {
          await navigator.clipboard.writeText(output.textContent);
          status.textContent = '복사했습니다.';
        } catch (_) {
          const range = document.createRange(); range.selectNodeContents(output);
          const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
          status.textContent = '자동 복사를 사용할 수 없습니다. 선택된 주소를 직접 복사하세요.';
        }
      });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize);
  else initialize();
}());
