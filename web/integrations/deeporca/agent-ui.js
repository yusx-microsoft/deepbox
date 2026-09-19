/* Path-free runtime configuration and provisioning presentation. */
(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AgentBridgeRuntimeCatalog = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
  'use strict';
  function agentConfiguration(capability) {
    const config = capability?.agent_config || {};
    return {
      canCreate: Array.isArray(config.profile_modes) && config.profile_modes.includes('create'),
      // Binding is intentionally not implemented until exclusive profile ownership exists.
      templates: (Array.isArray(config.configuration_templates) ? config.configuration_templates : [])
        .filter(item => item && typeof item.id === 'string' && /^[a-zA-Z0-9_.:-]{1,128}$/.test(item.id))
        .map(item => ({value:item.id, label:String(item.label || item.id)})),
    };
  }
  function creationConfig(capability, template) {
    const config = agentConfiguration(capability);
    if (!config.canCreate) throw new Error('This Machine does not advertise managed DeepOrca profile creation. Update its local setup and reconnect.');
    if (config.templates.length && !config.templates.some(item => item.value === template))
      throw new Error('The configuration template is no longer available. Reopen Add agent to refresh.');
    if (!config.templates.length && template !== 'connector-default') throw new Error('Invalid configuration template.');
    return {integration_version:1, profile:{mode:'create', configuration_template_ref:template}};
  }
  function creationConfigFromValues(capability, values) {
    return creationConfig(capability, values.template);
  }
  function runtimeStatus(status) {
    const state = ['pending','provisioning','ready','needs_configuration','error'].includes(status?.state) ? status.state : 'pending';
    const messages = {
      pending:'Agent created. Waiting for the Connector to provision its local profile.',
      provisioning:'Provisioning the local DeepOrca profile…',
      ready:'Local profile ready. Model authentication has not been verified by DeepBox.',
      needs_configuration:'Repair the local model configuration on the Connector, then Retry initialization.',
      error:'Check the Connector configuration and local logs, repair the issue, then Retry initialization.',
    };
    return {state, message:messages[state] || messages.pending, code:typeof status?.code === 'string' ? status.code : ''};
  }
  function creationFields(config) {
    return [{name:'template', label:'DeepOrca configuration template', type:'select',
      value:config.templates[0]?.value || 'connector-default',
      options:config.templates.length ? config.templates : [{value:'connector-default',label:'Connector default (local configuration may be needed)'}],
      helpHtml:'<small><b>Automatic managed profile.</b> An independent private local profile is automatically created for this Agent; no manual profile creation is needed. Model credentials stay on the Connector. Tools follow local policy without interactive approval.</small>'}];
  }
  function bindCreation(root, runtime, config, runtimeId = 'deeporca') {
    const update = () => {
      const selected = runtime.value === runtimeId;
      root.querySelector('[data-field="template"]').closest('.field').hidden = !selected;
      root.querySelector('[data-error]').textContent = selected && !config.canCreate
        ? 'This Machine does not advertise managed profile creation. Update the Connector setup.' : '';
    };
    runtime.addEventListener('change', update); update();
  }
  const settingsDescription = 'DeepOrca · Rename this Agent or inspect its existing binding. New conversations are opened separately.';
  const settingsHtml = '<div data-agent-metadata></div><div data-runtime-status role="status" aria-live="polite"></div>' +
    '<p>Runtime, profile mode, project and template are immutable. Credentials and configuration stay on the Connector. Tools follow local policy without interactive approval.</p>' +
    '<button type="button" class="ghost" data-refresh-status>Refresh status</button> ' +
    '<button type="button" class="ghost" data-retry-runtime>Retry initialization</button>';
  const retryable = agent => ['needs_configuration','error'].includes(agent.runtime_status?.state);
  function renderSettings(root, box, agent, UI) {
    const esc = UI.escapeHtml, profile = agent.runtime_config?.profile || {};
    const safeRef = value => typeof value === 'string' && /^[a-zA-Z0-9_.:-]{1,128}$/.test(value) ? value : 'Unavailable';
    const project = UI.localProjectOptions(box.projects).find(item => item.id === agent.local_project_id);
    const status = runtimeStatus(agent.runtime_status);
    root.querySelector('[data-agent-metadata]').innerHTML = `<dl><dt>Runtime</dt><dd>DeepOrca (deeporca)</dd><dt>Handle (fixed)</dt><dd>${esc(agent.handle || '')}</dd><dt>Profile</dt><dd>${profile.mode === 'create' || !profile.mode ? 'Automatic · managed local profile' : 'Unavailable'}</dd><dt>Registered local project</dt><dd>${esc(project?.name || 'Unavailable project')} · ${esc(safeRef(agent.local_project_id))}</dd><dt>Configuration template</dt><dd>${esc(safeRef(profile.configuration_template_ref || 'connector-default'))}</dd></dl>`;
    root.querySelector('[data-runtime-status]').innerHTML = `<p><b>Readiness: ${esc(status.state.replace(/_/g,' '))}</b></p><p>${esc(status.message)}</p>` +
      (/^[a-z][a-z0-9_]{0,63}$/.test(status.code) ? `<p>Status code: ${esc(status.code)}</p>` : '') +
      (box.online === false ? '<p>Connector offline. Initialization requires it to reconnect; a pending request is not proof of success.</p>' : '');
    const retry = root.querySelector('[data-retry-runtime]');
    retry.hidden = retry.disabled = !retryable(agent);
  }
  return {agentConfiguration, creationConfig, creationConfigFromValues, runtimeStatus, creationFields, bindCreation,
    settingsDescription, settingsHtml, retryable, renderSettings};
});
