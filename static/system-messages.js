/* Settings > Agent > System message. Drafts stay local until Save succeeds. */
const SystemMessageSettings = (() => {
  let catalog = null, draft = null, busy = false, loading = 0, notice = '', failed = false;
  const current = () => ({
    system_message_variant: S.settings.system_message_variant || 'compact',
    system_message_custom: S.settings.system_message_custom || '',
    assistant_name: S.settings.assistant_name || 'Assistant',
  });
  const active = () => S.settingsTab === 'systemmessage';
  const changed = () => draft && Object.keys(current()).some(key => draft[key] !== current()[key]);
  const preset = () => catalog.presets.find(p => p.id === draft.system_message_variant) || catalog.presets[0];
  const text = () => draft.system_message_variant === 'custom' ? draft.system_message_custom
    : preset()[S.mode === 'cloud' || S.mode === 'auto' ? 'cloud_text' : 'local_text'];
  const preview = value => {
    const name = draft.assistant_name.replace(/[\x00-\x1f\x7f]/g, ' ').trim().replace(/\s+/g, ' ')
      .slice(0, catalog.max_name_chars) || 'Assistant';
    return value.replaceAll('{NAME}', () => name).replaceAll('{PERSONA_BLOCK}', '').replaceAll('{ROLE_BLOCK}', '');
  };
  const valid = () => draft.system_message_custom.length <= catalog.max_custom_chars
    && (draft.system_message_variant !== 'custom' || !!draft.system_message_custom.trim());

  async function open() {
    const request = ++loading;
    const body = $('settings-body');
    if (!catalog) {
      body.innerHTML = '<h3>System message</h3><p class="lead" role="status">Loading presets…</p>';
      try { catalog = await api('/api/system-messages'); }
      catch (e) {
        if (active() && request === loading) body.innerHTML = `<h3>System message</h3><p class="lead" role="alert">Couldn't load the presets: ${esc(e.message)}</p><button class="btn" onclick="renderSystemMessage()">Retry</button>`;
        return;
      }
    }
    if (!active() || request !== loading) return;
    if (!draft) draft = current();
    paint();
  }

  function status() {
    if (!active() || !draft || !catalog) return;
    const field = $('system-message-text'), count = $('system-message-count'), state = $('system-message-status');
    const custom = draft.system_message_variant === 'custom';
    if (field && !custom) field.value = preview(text());
    if (count) count.textContent = custom ? `${draft.system_message_custom.length.toLocaleString()} / ${catalog.max_custom_chars.toLocaleString()} characters`
      : `${preview(text()).length.toLocaleString()} characters · preset preview`;
    if (state) {
      state.textContent = notice || (!valid() ? 'Write a custom message within the character limit before saving.' : changed() ? 'Unsaved changes' : '');
      state.classList.toggle('error', failed);
    }
    const saveButton = $('system-message-save');
    if (saveButton) saveButton.disabled = busy || !changed() || !valid();
    const cancelButton = $('system-message-cancel');
    if (cancelButton) cancelButton.disabled = busy || !changed();
  }

  function paint() {
    if (!active() || !draft || !catalog) return;
    const custom = draft.system_message_variant === 'custom', disabled = busy ? ' disabled' : '';
    $('settings-body').innerHTML = `<h3>System message</h3>
      <p class="lead">Choose the assistant's base instructions for chat and voice. Changes apply to new turns in Enhanced mode. Raw sends only the conversation.</p>
      <div class="set-group">
        ${rowHtml('Variant', 'Compact is your shorter message with the agreed wording fixes.', `<select class="input" aria-label="System message variant" onchange="SystemMessageSettings.choose(this.value)"${disabled}>${catalog.presets.map(p => `<option value="${esc(p.id)}"${draft.system_message_variant === p.id ? ' selected' : ''}>${esc(p.label)}</option>`).join('')}</select>`)}
        ${rowHtml('Assistant name', 'Used wherever the message contains {NAME}.', `<input id="system-message-name" class="input" aria-label="Assistant name" maxlength="${catalog.max_name_chars}" value="${esc(draft.assistant_name)}" oninput="SystemMessageSettings.name(this.value)"${disabled}>`)}
      </div>
      <div class="system-message-actions">
        <button id="system-message-save" class="btn primary" onclick="SystemMessageSettings.save()"${disabled}>${busy ? 'Saving…' : 'Save changes'}</button>
        <button id="system-message-cancel" class="btn" onclick="SystemMessageSettings.cancel()"${disabled}>Cancel changes</button>
        <button class="btn" onclick="SystemMessageSettings.choose('compact')"${disabled}>Use Compact</button>
      </div>
      <p id="system-message-status" class="system-message-meta" role="status" aria-live="polite" aria-atomic="true"></p>
      <div class="set-group system-message-panel">
        <div class="system-message-heading"><label class="gt" for="system-message-text">${custom ? 'Your custom message' : `${esc(preset().label)} preview`}</label>
          ${custom ? '' : `<button class="btn sm" onclick="SystemMessageSettings.copy()"${disabled}>Edit a copy</button>`}</div>
        <p class="lead">${esc(preset().description)}</p>
        <textarea id="system-message-text" class="input system-message-editor" rows="16" spellcheck="false" aria-describedby="system-message-help system-message-count"${custom ? ` oninput="SystemMessageSettings.edit(this.value)"` : ' readonly'}${disabled}>${esc(custom ? text() : preview(text()))}</textarea>
        <div id="system-message-count" class="system-message-meta"></div>
        <p id="system-message-help" class="system-message-meta">Custom replaces the base message. {NAME} uses the name above; {PERSONA_BLOCK} is filled by the selected persona, which is added automatically. The app's permission checks still apply.</p>
      </div>`;
    status();
  }

  function dirty() { notice = ''; failed = false; status(); }
  function choose(value) {
    if (busy || !catalog || !draft || !catalog.presets.some(p => p.id === value)) return;
    draft.system_message_variant = value; notice = ''; failed = false; paint();
  }
  function copy() {
    if (busy || !draft || draft.system_message_variant === 'custom') return;
    draft.system_message_custom = text(); draft.system_message_variant = 'custom'; notice = ''; failed = false;
    paint(); $('system-message-text')?.focus();
  }
  function edit(value) { if (!busy && draft) { draft.system_message_custom = value; dirty(); } }
  function name(value) { if (!busy && draft) { draft.assistant_name = value; dirty(); } }
  function cancel() { if (!busy) { draft = current(); notice = ''; failed = false; paint(); } }

  async function save() {
    if (busy || !draft || !changed() || !valid()) return;
    const payload = { ...draft };
    busy = true; notice = ''; failed = false; paint();
    try {
      const data = await api('/api/settings', payload);
      if (!data.settings) throw new Error('The server did not confirm the saved settings.');
      S.settings = data.settings; store.set('settings', S.settings); applyAppearance();
      draft = current();
      notice = S.settings.response_mode === 'raw' ? 'Saved. Switch to Enhanced to use this message.' : 'Saved. Applies to the next text or voice turn.';
    } catch (e) { notice = `Could not save: ${e.message}`; failed = true; }
    finally { busy = false; paint(); }
  }

  return { open, choose, copy, edit, name, cancel, save };
})();

function renderSystemMessage() { return SystemMessageSettings.open(); }
