/* DeepOrca-only Agent configuration. Provider keys exist only in the live password
 * input and WebCrypto memory; only a Connector-sealed envelope leaves the browser. */
(function(root, factory) {
  const common = typeof module === 'object' && module.exports;
  const api = factory(common ? require('../../ui.js') : (root.AgentBridgeUI || root.DeepboxUI));
  if (common) module.exports = api;
  else root.AgentBridgeRuntimeCatalog = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(UI) {
  'use strict';
  const esc = UI.escapeHtml;
  const EFFORTS = ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
  const FIELD_NAMES = ['base_url', 'model', 'auth_mode', 'api_key', 'context_window', 'reasoning_effort'];
  const NATIVE_REF = /^native-[0-9a-f]{32}$/;
  const NATIVE_CONSENT = 'I have stopped native DeepOrca for this profile and will keep it stopped until the Connector stops';
  const NATIVE_DETAILS = '<p>Reuses this native profile’s model, security, persona, memory, skills and MCP. DeepBox does not edit or copy its configuration. Old native chats are not imported as DeepBox chats.</p><p>Execution stays non-interactive: this confirmation is not tool approval. Work requiring approval is blocked by the native profile’s policy.</p>';
  const CRYPTO_HELP = 'API-key encryption is unavailable. Update the Connector to advertise its credential key and open this workbench over HTTPS (or localhost). No authentication is available for keyless endpoints.';
  const HELP = {
    template_missing:'The Connector default template is unavailable. Update or repair the Connector, then retry provisioning.',
    model_not_configured:'Complete Model connection and Model behavior below, then save settings.',
    context_window_not_configured:'Set the model’s context window in tokens below, then save settings.',
    credential_unavailable:'Replace the API key below, or explicitly choose No authentication, then save settings.',
    binding_conflict:'This managed binding conflicts with local state. Review it on the Connector before retrying.',
    agent_busy:'The managed profile is busy. Wait for its active conversation to finish, then retry.',
    project_unavailable:'The registered project is unavailable. Restore its local registration before retrying.',
    profile_configuration_failed:'The Connector could not configure this profile. Review the settings below and retry.',
  };
  const NATIVE_HELP = {
    existing_profile_unavailable:'The native profile is unavailable. Restore it on the Connector, then refresh status and retry. This binding cannot be redirected to another profile.',
    existing_profile_api_unavailable:'This Connector cannot bind native profiles. Update its DeepOrca SDK and Connector, then refresh status and retry.',
    configuration_busy:'The native profile is busy. Stop native DeepOrca for this profile and wait for other use to end before retrying. Keep it stopped until the Connector stops.',
    profile_unavailable:'The native profile is unavailable or in use. Check the profile on the Connector and stop native DeepOrca before retrying. Keep it stopped until the Connector stops.',
  };
  function runtimeStatus(value, bound=false) {
    const labels = {pending:'Pending', provisioning:'Provisioning', ready:'Ready', needs_configuration:'Needs configuration', error:'Provisioning error'};
    const state = Object.hasOwn(labels, value?.state) ? value.state : 'pending';
    const code = typeof value?.code === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(value.code) ? value.code : '';
    const message = state === 'ready'
      ? 'Profile is ready. Provider connectivity and credentials have not been verified.'
      : (Object.hasOwn(HELP, code) ? HELP[code] : '') || (state === 'needs_configuration'
        ? 'Complete the model settings below and save to configure this Agent.'
        : state === 'error' ? 'Provisioning failed. Review the Connector and these settings, then retry.'
        : 'Waiting for the Connector to provision the managed profile.');
    if (bound) return {state, code, label:labels[state] || state, message:state === 'ready'
      ? 'Native profile is ready. Keep native DeepOrca stopped until the Connector stops. Provider connectivity and credentials have not been verified.'
      : (Object.hasOwn(NATIVE_HELP, code) ? NATIVE_HELP[code] : '') ||
        (state === 'error' || state === 'needs_configuration'
          ? 'The native profile is unavailable or needs local attention. Stop native DeepOrca, review this profile on the Connector, then retry. No model settings can be changed here.'
          : 'Waiting for the Connector to bind the native profile. Keep native DeepOrca stopped until the Connector stops.')};
    return {state, code, label:labels[state] || state, message};
  }
  function agentConfiguration(descriptor) {
    const config = descriptor?.agent_config || {};
    const modes = Array.isArray(config.profile_modes) ? config.profile_modes : [];
    const templates = (Array.isArray(config.configuration_templates) ? config.configuration_templates : [])
      .filter(item=>item && typeof item.id === 'string' && /^[a-zA-Z0-9_.:-]{1,128}$/.test(item.id))
      .map(item=>({value:item.id,label:String(item.label || item.id)}));
    const profiles = (Array.isArray(config.existing_profiles) ? config.existing_profiles : [])
      .filter(item=>item && typeof item.id === 'string' && NATIVE_REF.test(item.id))
      .map(item=>({value:item.id,label:typeof item.label === 'string' && item.label ? item.label : item.id}));
    return {canCreate:modes.includes('create'), canBind:modes.includes('bind'),
      modes, templates, profiles, executionPolicy:config.execution_policy || 'non_interactive'};
  }
  function creationConfig(descriptor, template='connector-default') {
    const config = agentConfiguration(descriptor);
    if (!config.canCreate || template !== 'connector-default' || (config.templates.length && !config.templates.some(item=>item.value === template)))
      throw new Error('This Connector does not advertise a supported DeepOrca managed profile. Update the Connector and refresh.');
    return {integration_version:1, profile:{mode:'create', configuration_template_ref:'connector-default'}};
  }
  function modelConfig(values) {
    const base_url = (values.base_url || '').trim(), model = (values.model || '').trim();
    if (!base_url) throw new Error('Endpoint is required. Enter an OpenAI-compatible base URL.');
    let url;
    try { url = new URL(base_url); } catch (_) { /* reported without echoing input */ }
    const shape = /^https?:\/\/((?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?)(\/[A-Za-z0-9._~!$&'()*+,;=:@/%-]*)?$/i.exec(base_url);
    const host = shape?.[1].split(':')[0].replace(/\.$/, '');
    const labelsValid = shape?.[1].startsWith('[') || (host && host.split('.').every(label=>/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(label)));
    if (!url || !shape || !labelsValid || !['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.port === '0' || base_url.length > 2048 || /[\s\x00-\x1f\x7f\\?#]/.test(base_url) || /%(?![0-9A-Fa-f]{2})/.test(base_url) || base_url.includes('${'))
      throw new Error('Endpoint must be an HTTP(S) base URL without embedded credentials, query parameters or a fragment.');
    if (!model) throw new Error('Model is required. Use the exact identifier from your provider.');
    if (!/^[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,255}$/.test(model))
      throw new Error('Model must be a provider identifier of at most 256 characters. Provider/model slashes are supported.');
    const raw = String(values.context_window ?? '').trim();
    const context_window = Number(raw);
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(context_window) || context_window <= 0)
      throw new Error('Context window is required: enter a whole token count from 1 to 9,007,199,254,740,991. Use your model’s documented limit.');
    const reasoning_effort = values.reasoning_effort ?? '';
    if (!EFFORTS.includes(reasoning_effort)) throw new Error('Choose a supported reasoning effort.');
    // Do not normalize this URL: the exact serialized value is authenticated by the envelope.
    return {provider:'openai', base_url, model, context_window, reasoning_effort};
  }
  function credentialKey(descriptor, crypto=globalThis.crypto) {
    const key = descriptor?.agent_config?.credential_key;
    return crypto?.subtle && crypto?.getRandomValues && key?.version === 1 && key.algorithm === 'RSA-OAEP-256+A256GCM'
      && /^[0-9a-f]{64}$/.test(key.key_id || '') && key.public_key?.kty === 'RSA' && !key.public_key.d ? key : null;
  }
  function base64(value) {
    let raw = '';
    for (const byte of new Uint8Array(value)) raw += String.fromCharCode(byte);
    return globalThis.btoa(raw);
  }
  async function sealCredential(secret, baseUrl, descriptor, crypto=globalThis.crypto) {
    const advertised = credentialKey(descriptor, crypto);
    if (!advertised || typeof globalThis.TextEncoder !== 'function') throw new Error(CRYPTO_HELP);
    if (typeof secret !== 'string' || !secret.trim()) throw new Error('API key is required, or choose No authentication for a keyless endpoint.');
    if (/[\s\x00-\x20\x7f\x85]/.test(secret) || secret.includes('${'))
      throw new Error('API key must not contain whitespace, control characters or environment placeholders.');
    const bytes = new TextEncoder().encode(secret);
    if (bytes.length > 4096) { bytes.fill(0); throw new Error('API key must be at most 4,096 UTF-8 bytes.'); }
    let rawKey;
    try {
      const publicKey = await crypto.subtle.importKey('jwk', advertised.public_key, {name:'RSA-OAEP', hash:'SHA-256'}, true, ['encrypt']);
      if (publicKey.algorithm.modulusLength !== 2048) throw new Error('unsupported key');
      const spki = await crypto.subtle.exportKey('spki', publicKey);
      const digest = await crypto.subtle.digest('SHA-256', spki);
      const keyId = Array.from(new Uint8Array(digest), byte=>byte.toString(16).padStart(2,'0')).join('');
      if (keyId !== advertised.key_id) throw new Error('key mismatch');
      rawKey = crypto.getRandomValues(new Uint8Array(32));
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const aes = await crypto.subtle.importKey('raw', rawKey, 'AES-GCM', false, ['encrypt']);
      const additionalData = new TextEncoder().encode('agentbridge/deeporca/credential/v1\0' + keyId + '\0' + baseUrl);
      const ciphertext = await crypto.subtle.encrypt({name:'AES-GCM', iv, additionalData, tagLength:128}, aes, bytes);
      // No OAEP label: the protocol uses the empty label.
      const wrapped = await crypto.subtle.encrypt({name:'RSA-OAEP'}, publicKey, rawKey);
      return {mode:'sealed', key_id:keyId, wrapped_key:base64(wrapped), iv:base64(iv), ciphertext:base64(ciphertext)};
    } catch (_) {
      // Never propagate WebCrypto inputs or a raw provider key into the error UI/logs.
      throw new Error(CRYPTO_HELP);
    } finally {
      bytes.fill(0); rawKey?.fill(0);
    }
  }
  async function configuredPayload(values, descriptor, agent) {
    if (agent?.runtime_config?.profile?.mode === 'bind') throw new Error('Native profile configuration is read-only. Only the Agent name can be changed here.');
    if (!agent && values.profile_mode === 'bind') {
      const config = agentConfiguration(descriptor);
      if (!config.canBind) throw new Error('This Connector does not support binding native profiles. Update its DeepOrca SDK and Connector, then refresh.');
      if (!NATIVE_REF.test(values.profile_ref || '') || !config.profiles.some(item=>item.value === values.profile_ref))
        throw new Error('Select an available native profile from this Connector’s inventory. If it is missing, restore it on the Connector and refresh projects to reload the list.');
      if (values.native_stopped !== true) throw new Error('Confirm that you have stopped native DeepOrca for this profile and will keep it stopped until the Connector stops.');
      // Deliberately do not read, validate, encrypt or serialize any managed draft.
      return {integration_version:1,profile:{mode:'bind',profile_ref:values.profile_ref,native_stopped:true}};
    }
    if (!agent && values.profile_mode && values.profile_mode !== 'create') throw new Error('Choose a supported profile mode.');
    const llm = modelConfig(values);
    const config = agent ? {integration_version:agent.runtime_config?.integration_version || 1,
      profile:{mode:'create', configuration_template_ref:'connector-default', ...agent.runtime_config?.profile}}
      : creationConfig(descriptor);
    config.llm = llm;
    if (agent?.runtime_config?.model !== undefined) {
      config.model = agent.runtime_config.model;
      if (llm.model !== config.model) throw new Error('This legacy binding has a fixed model override. Create a new Agent to use a different model.');
    }
    if (values.auth_mode === 'api_key') config.credential = await sealCredential(values.api_key, llm.base_url, descriptor);
    else if (values.auth_mode === 'none') config.credential = {mode:'none'};
    else if (values.auth_mode === 'keep' && agent) {
      if (llm.base_url !== (agent.runtime_config?.llm?.base_url || ''))
        throw new Error('Endpoint changed. Replace the API key or explicitly choose No authentication before saving.');
      // Server merges its current envelope. Never read back or replay stored secrets.
    } else throw new Error('Choose API key or No authentication.');
    return config;
  }
  function configurationFields(agent) {
    const llm = agent?.runtime_config?.llm || {};
    return [
      {name:'base_url', label:'Endpoint', value:llm.base_url || '', placeholder:'https://api.example.com/v1',
        helpHtml:'<p class="hint">Any OpenAI-compatible endpoint. Local example: <code>http://localhost:11434/v1</code>. Localhost refers to the Connector, not this browser.</p>'},
      {name:'model', label:'Model', value:llm.model || agent?.runtime_config?.model || '', placeholder:'Exact provider model identifier',
        helpHtml:agent?.runtime_config?.model !== undefined
          ? '<p class="hint">This legacy binding has a fixed model override. Create a new Agent to change it.</p>'
          : '<p class="hint">Use your provider’s exact ID; slashes such as <code>provider/model-name</code> are supported.</p>'},
      {name:'auth_mode', label:'Authentication', type:'select', value:agent ? 'keep' : 'api_key', options:[
        ...(agent ? [{value:'keep',label:'Keep current authentication'}] : []),
        {value:'api_key',label:agent ? 'Replace API key' : 'API key'}, {value:'none',label:'No authentication'}],
        helpHtml:'<p class="hint" data-auth-hint></p>'},
      {name:'api_key', label:agent ? 'New API key' : 'API key', type:'password', value:'', placeholder:'Never shown again',
        helpHtml:'<p class="hint">Encrypted in this browser for the Connector. Never saved in browser storage or sent as plaintext to the Server.</p><p class="hint do-key-warning" data-key-warning role="status"></p>'},
      {name:'context_window', label:'Context window · tokens', type:'text', value:llm.context_window ?? '', placeholder:'Your model’s documented token limit',
        helpHtml:'<p class="hint">Required. Enter the actual context capacity, including input and output. No limit is guessed for you.</p>'},
      {name:'reasoning_effort', label:'Reasoning effort', type:'select', value:llm.reasoning_effort || '', options:EFFORTS.map(value=>({value,label:value || 'Provider default'})),
        helpHtml:'<p class="hint">Provider default leaves reasoning effort unspecified. Other values depend on model support.</p>'},
    ];
  }
  function creationFields(config={}) {
    return [
      {name:'profile_mode',label:'Profile',type:'select',value:'create',options:[{value:'create',label:'Create a new profile'},
        ...(config.canBind ? [{value:'bind',label:'Bind an existing profile'}] : [])]},
      {name:'profile_ref',label:'Existing native profile',type:'select',value:'',options:[{value:'',label:'Select an existing profile'},...(config.profiles || [])]},
      {name:'native_stopped',label:NATIVE_CONSENT,type:'checkbox',value:''},
      ...configurationFields(),
    ];
  }
  const ADVANCED = '<p class="hint do-security-default">New profiles default to minimal security: broad tools, no approvals. A trusted Connector template may override this default.</p><details class="do-agent-advanced"><summary>Managed profile &amp; execution</summary><p>Automatic managed profile, created on the Connector from its default template. Model settings above override the template; paths and runtime identity stay local and cannot be changed here.</p><p>Execution is non-interactive. Work that needs approval is blocked by local policy. Saving does not test the provider connection.</p></details>';
  function styleDialog(element) {
    const doc = element.ownerDocument;
    if (!doc.querySelector('[data-deeporca-agent-style]')) {
      const link = doc.createElement('link'); link.rel = 'stylesheet'; link.href = '/static/integrations/deeporca/agent-ui.css?v=bind-1';
      link.setAttribute('data-deeporca-agent-style', ''); (doc.head || doc.body).appendChild(link);
    }
    element.querySelector('[data-error]').setAttribute('role','alert');
  }
  function bindConfiguration({element, selected, descriptor, agent, runtimeId = agent?.runtime || 'deeporca'}) {
    const doc = element.ownerDocument, modal = element.querySelector('.modal');
    styleDialog(element);
    const input = name=>element.querySelector(`[data-field="${name}"]`);
    const mode = input('profile_mode'), profile = input('profile_ref'), consent = input('native_stopped');
    let nativeGroup, nativeHelp, inventoryKey;
    if (mode) {
      nativeGroup = doc.createElement('fieldset'); nativeGroup.className = 'do-native-profile';
      nativeGroup.innerHTML = '<legend>Existing native profile</legend><div class="do-native-details">' + NATIVE_DETAILS + '</div><p class="hint" data-native-inventory role="status"></p>';
      const first = profile.closest('.field'); first.parentNode.insertBefore(nativeGroup, first);
      nativeGroup.appendChild(first); nativeGroup.appendChild(consent.closest('.field'));
      consent.closest('.field').classList.add('do-native-consent');
      consent.setAttribute('aria-required','true'); profile.setAttribute('aria-required','true');
      nativeHelp = nativeGroup.querySelector('[data-native-inventory]');
      nativeHelp.id = profile.id + '-help'; profile.setAttribute('aria-describedby',nativeHelp.id);
    }
    const groups = [];
    for (const [title,names] of [['Model connection',['base_url','model','auth_mode','api_key']], ['Model behavior',['context_window','reasoning_effort']]]) {
      const group = doc.createElement('fieldset'); group.className = 'do-agent-group';
      const legend = doc.createElement('legend'); legend.textContent = title; group.appendChild(legend);
      const first = input(names[0]).closest('.field'); first.parentNode.insertBefore(group, first);
      for (const name of names) group.appendChild(input(name).closest('.field'));
      groups.push(group);
    }
    const advanced = doc.createElement('div'); advanced.className = 'do-agent-profile'; advanced.innerHTML = ADVANCED;
    groups[1].parentNode.insertBefore(advanced, groups[1].nextSibling);
    const key = input('api_key'), auth = input('auth_mode'), endpoint = input('base_url');
    for (const name of ['base_url','model','context_window']) input(name).setAttribute('aria-required','true');
    key.autocomplete = 'new-password'; key.setAttribute('autocomplete','new-password'); key.setAttribute('spellcheck','false');
    endpoint.setAttribute('spellcheck','false'); input('model').setAttribute('spellcheck','false');
    input('context_window').setAttribute('inputmode','numeric');
    for (const name of FIELD_NAMES) {
      const field = input(name), hints = field.closest('.field').querySelectorAll('.hint');
      const ids = [];
      hints.forEach((hint,index)=>{ hint.id = field.id + '-help-' + index; ids.push(hint.id); });
      if (ids.length) field.setAttribute('aria-describedby', ids.join(' '));
    }
    const authHint = element.querySelector('[data-auth-hint]'), warning = element.querySelector('[data-key-warning]');
    function update(nextAgent) {
      if (nextAgent?.runtime === runtimeId) agent = nextAgent;
      const active = !selected || selected() === runtimeId;
      const bound = mode?.value === 'bind';
      if (mode) {
        const config = agentConfiguration(descriptor?.());
        const signature = JSON.stringify([config.canBind,config.profiles]);
        if (signature !== inventoryKey) {
          const previous = profile.value;
          const choices = [{value:'',label:'Select an existing profile'},...config.profiles];
          if (previous && !choices.some(item=>item.value === previous)) choices.push({value:previous,label:'Previously selected profile is unavailable — select another'});
          profile.innerHTML = choices.map(item=>`<option value="${esc(item.value)}">${esc(item.label)}</option>`).join('');
          profile.value = previous;
          mode.innerHTML = '<option value="create">Create a new profile</option>' + (config.canBind || bound ? '<option value="bind">Bind an existing profile</option>' : '');
          mode.value = bound ? 'bind' : 'create';
          inventoryKey = signature;
        }
        mode.closest('.field').hidden = !active || !config.canBind; mode.disabled = !active || !config.canBind;
        nativeGroup.hidden = !active || !bound;
        profile.disabled = consent.disabled = !active || !bound || !config.canBind;
        profile.required = consent.required = active && bound && config.canBind;
        nativeHelp.textContent = !config.canBind ? 'This Connector no longer advertises native binding. Update its DeepOrca SDK and Connector, then refresh projects to reload capabilities.'
          : !config.profiles.length ? 'No native profiles are available. Check the native home on this Connector, then refresh projects to reload the list. Paths cannot be entered here.'
          : profile.value && !config.profiles.some(item=>item.value === profile.value) ? 'The selected profile is no longer available. Restore it on the Connector or explicitly select another profile.'
          : 'Profiles come from this Connector’s native home. If a profile is missing or busy, check it on the Connector and stop native DeepOrca before retrying.';
      }
      const managed = active && !bound;
      modal.classList.toggle('do-agent-modal', active);
      for (const node of [...groups, advanced]) node.hidden = !managed;
      for (const name of FIELD_NAMES) {
        const field = input(name); field.disabled = !managed;
        field.closest('.field').hidden = !managed;
      }
      input('model').readOnly = agent?.runtime_config?.model !== undefined;
      const apiKey = auth.value === 'api_key';
      key.disabled = !managed || !apiKey; key.closest('.field').hidden = !managed || !apiKey;
      key.setAttribute('aria-required', String(apiKey));
      // Validation happens in collect, yielding one accessible, draft-preserving error.
      authHint.textContent = agent && endpoint.value.trim() !== (agent.runtime_config?.llm?.base_url || '')
        ? 'Endpoint changed: replace the API key or explicitly choose No authentication. The current key cannot be reused for a different endpoint.'
        : auth.value === 'keep' ? 'Current authentication stays on the Server and Connector. No secret is read back.'
        : auth.value === 'none' ? 'Use only for endpoints that do not require a key. Any existing API key will be removed.'
        : 'Only this Connector can decrypt the API key. Use HTTPS for remote model endpoints.';
      warning.textContent = apiKey && !credentialKey(descriptor?.()) ? CRYPTO_HELP : '';
    }
    auth.addEventListener('change', update); endpoint.addEventListener('input', update);
    mode?.addEventListener('change', update);
    profile?.addEventListener('change', ()=>{ consent.checked = false; update(); });
    update();
    return {update};
  }
  function bindCreation(root, runtime, config, runtimeId='deeporca', descriptor) {
    const binding = bindConfiguration({element:root, selected:()=>runtime.value, descriptor:descriptor || (()=>({agent_config:{
      profile_modes:config.modes || [],existing_profiles:(config.profiles || []).map(item=>({id:item.value,label:item.label}))}})), runtimeId});
    runtime.addEventListener('change', binding.update);
    return binding;
  }
  const settingsDescription = 'DeepOrca · Configure the model for this Agent. No external configuration file is needed.';
  const settingsHtml = '<section class="do-agent-readiness"><div data-agent-metadata></div><div data-runtime-status role="status" aria-live="polite"></div>' +
    '<div class="do-agent-runtime-actions"><button type="button" class="ghost" data-refresh-status>Refresh status</button> ' +
    '<button type="button" class="ghost" data-retry-runtime>Retry initialization</button></div></section>';
  const retryable = agent => ['needs_configuration','error'].includes(agent.runtime_status?.state);
  function renderSettings(root, box, agent) {
    const project = UI.localProjectOptions(box.projects).find(item=>item.id === agent.local_project_id);
    const bound = agent.runtime_config?.profile?.mode === 'bind';
    const status = runtimeStatus(agent.runtime_status, bound);
    const ref = agent.runtime_config?.profile?.profile_ref;
    const native = bound ? agentConfiguration(UI.findRuntimeCapability(box.capabilities, agent.runtime)).profiles.find(item=>item.value === ref) : null;
    root.querySelector('[data-agent-metadata]').innerHTML = `<p class="hint">DeepOrca · ${esc(project?.name || 'Registered project unavailable')} · @${esc(agent.handle || '')}</p>` + (bound
      ? `<div class="do-native-summary"><p><strong>Bound native profile · ${esc(native?.label || 'Profile unavailable in current inventory')}</strong></p><p>Profile reference: <code>${esc(NATIVE_REF.test(ref || '') ? ref : 'Unavailable')}</code></p>${NATIVE_DETAILS}<p>Profile, runtime and project are fixed. Only the Agent name can be changed here. Keep native DeepOrca stopped until the Connector stops.</p></div>` : '');
    root.querySelector('[data-runtime-status]').innerHTML = `<div class="do-agent-status"><span class="pill">${esc(status.label)}</span><p>${esc(status.message)}</p></div>` +
      (/^[a-z][a-z0-9_]{0,63}$/.test(status.code || '') ? `<p class="hint">Status code: <code>${esc(status.code)}</code></p>` : '') +
      (box.online === false ? `<p class="hint">${bound ? 'Connector offline. Check its status locally before restarting native DeepOrca.' : 'Connector offline. Configuration will apply when it reconnects.'}</p>` : '') +
      (bound ? '' : '<p class="hint">Runtime and project are fixed. End active conversations before changing model settings.</p>');
    const retry = root.querySelector('[data-retry-runtime]');
    retry.hidden = retry.disabled = !retryable(agent);
  }
  function bindSettings(root, agent, descriptor) {
    if (agent.runtime_config?.profile?.mode === 'bind') {
      styleDialog(root); root.querySelector('.modal').classList.add('do-agent-modal');
      root.querySelector('.modal-head p').textContent = 'DeepOrca · Bound native profile. Manage its configuration locally on the Connector.';
      return {update:()=>{}};
    }
    const binding = bindConfiguration({element:root, agent, descriptor});
    const readiness = root.querySelector('.do-agent-readiness'), group = root.querySelector('.do-agent-group');
    group.parentNode.insertBefore(readiness, group);
    return binding;
  }
  function afterSave(root, result) {
    const key = root.querySelector('[data-field="api_key"]'); if (key) key.value = '';
    const auth = root.querySelector('[data-field="auth_mode"]');
    if (result && auth?.querySelector('option[value="keep"]')) auth.value = 'keep';
  }
  async function settingsPayload(values, agent, descriptor) {
    const name = {display_name:values.display_name};
    if (agent.runtime_config?.profile?.mode === 'bind') return name;
    // A legacy/unconfigured Agent can still be renamed without forcing model
    // setup. Once any model field is edited, require the complete configuration.
    if (!agent.runtime_config?.llm && !values.base_url && !values.context_window
        && !values.reasoning_effort && !values.api_key && values.auth_mode === 'keep'
        && values.model === (agent.runtime_config?.model || '')) return name;
    return {...name, runtime_config:await configuredPayload(values, descriptor, agent)};
  }
  return {agentConfiguration, creationConfig, runtimeStatus, modelConfig, sealCredential, configuredPayload, EFFORTS,
    creationFields, bindCreation,
    creationConfigFromValues:(descriptor, values)=>configuredPayload(values, descriptor),
    settingsFields:agent=>agent.runtime_config?.profile?.mode === 'bind' ? [] : configurationFields(agent), bindSettings, settingsDescription, settingsHtml, renderSettings, retryable,
    settingsSubmit:'Save settings', afterSave,
    settingsPayload};
});
