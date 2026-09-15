/* One lifecycle for dialogs, forms, menus and the command picker. */
(function(root, factory){
  const common = typeof module === 'object' && module.exports;
  const api = factory(common ? require('./ui.js') : (root.AgentBridgeUI || root.DeepboxUI));
  if(common) module.exports = api;
  else root.AgentBridgeDialogs = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function(UI){
  'use strict';
  const esc = UI.escapeHtml;

  function createDialogs(document){
    const window = document.defaultView || globalThis;
    let active = null, menuState = null, sequence = 0, toast = null, toastTimer = null, dead = false;
    function isCurrent(element){ return active?.element === element; }
    function closeMenu(){
      if(!menuState) return;
      const old = menuState; menuState = null;
      document.removeEventListener('pointerdown', old.outside, true);
      document.removeEventListener('keydown', old.key);
      old.element.remove();
    }
    function close(){ closeMenu(); active?.done(null); }

    function overlay(html, onReady, kind='dialog'){
      if(dead) return Promise.resolve(null);
      close();
      return new Promise((resolve,reject)=>{
        const element = document.createElement('div');
        element.className = 'overlay'; element.id = 'overlay'; element.innerHTML = html;
        const previous = document.activeElement;
        let settled = false;
        function done(value, error){
          if(settled) return;
          settled = true;
          document.removeEventListener('keydown', key);
          element.remove();
          if(isCurrent(element)) active = null;
          if(previous?.isConnected) previous.focus();
          error ? reject(error) : resolve(value);
        }
        function key(event){
          if(!isCurrent(element) || event.isComposing) return;
          if(event.key === 'Escape'){ event.preventDefault(); done(null); }
          if(event.key !== 'Tab') return;
          const fields = [...element.querySelectorAll('input,select,textarea,button,a[href]')]
            .filter(node=>!node.disabled && !node.hidden && (node.getClientRects ? node.getClientRects().length > 0 : true));
          if(!fields.length) return;
          const first = fields[0], last = fields[fields.length-1];
          if(event.shiftKey && document.activeElement === first){ event.preventDefault(); last.focus(); }
          else if(!event.shiftKey && document.activeElement === last){ event.preventDefault(); first.focus(); }
        }
        active = {element, done, kind};
        element.onclick = event=>{ if(event.target === element) done(null); };
        document.addEventListener('keydown', key);
        document.body.appendChild(element);
        try { onReady?.(element, done); } catch(error) { done(null,error); }
      });
    }

    function modal({title, desc, bodyHtml='', actions, onReady}){
      const buttons = actions || [{label:'OK',primary:true,value:true}];
      return overlay(`<div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title||'')}">
        <header class="modal-head"><h3>${esc(title||'')}</h3>${desc ? `<p>${esc(desc)}</p>` : ''}</header>
        ${bodyHtml ? `<div class="modal-body">${bodyHtml}</div>` : ''}
        <footer class="modal-actions">${buttons.map((button,index)=>`<button type="button" class="${button.primary?'':'ghost'}${button.danger?' danger':''}" data-action-index="${index}">${esc(button.label)}</button>`).join('')}</footer>
      </div>`, (element,done)=>{
        element.querySelectorAll('[data-action-index]').forEach(button=>{
          button.onclick = ()=>done(buttons[Number(button.dataset.actionIndex)].value);
        });
        onReady?.(element);
        element.querySelector('.modal-actions button')?.focus();
      });
    }

    function alert(title, message){ return modal({title,desc:message}); }
    function confirm(title, message, label='Confirm'){
      return modal({title,desc:message,actions:[
        {label:'Cancel',value:false}, {label,primary:true,danger:true,value:true},
      ]});
    }

    function form({title, desc, fields, submit='Save', extraHtml='', onReady}){
      const prefix = 'dialog-' + (++sequence) + '-';
      const body = fields.map((field,index)=>{
        const id = prefix + index;
        const input = field.type === 'select'
          ? `<select id="${id}" data-field="${esc(field.name)}">${(field.options||[]).map(option=>{
              const value = typeof option === 'object' ? option.value : option;
              const label = typeof option === 'object' ? option.label : option;
              return `<option value="${esc(value)}"${String(value) === String(field.value) ? ' selected' : ''}>${esc(label)}</option>`;
            }).join('')}</select>`
          : field.type === 'textarea'
            ? `<textarea id="${id}" data-field="${esc(field.name)}" placeholder="${esc(field.placeholder||'')}">${esc(field.value||'')}</textarea>`
            : `<input id="${id}" data-field="${esc(field.name)}" type="${esc(field.type||'text')}" value="${esc(field.value??'')}" placeholder="${esc(field.placeholder||'')}"/>`;
        return `<div class="field"><label for="${id}">${esc(field.label)}</label>${input}${field.helpHtml||''}</div>`;
      }).join('');
      return overlay(`<form class="modal" role="dialog" aria-modal="true" aria-label="${esc(title||'')}">
        <header class="modal-head"><h3>${esc(title||'')}</h3>${desc ? `<p>${esc(desc)}</p>` : ''}</header>
        <div class="modal-body">${body}${extraHtml}</div><p class="modal-err" data-error role="alert"></p>
        <footer class="modal-actions"><button type="button" class="ghost" data-cancel>Cancel</button><button type="submit" data-submit>${esc(submit)}</button></footer>
      </form>`, (element,done)=>{
        const actual = element.querySelector('form');
        actual.onsubmit = event=>{
          event.preventDefault();
          if(!isCurrent(element)) return;
          const values = {};
          for(const [index,field] of fields.entries()) values[field.name] = element.querySelector('#'+prefix+index).value.trim();
          const missing = fields.find(field=>field.required && !values[field.name]);
          if(missing){ element.querySelector('[data-error]').textContent = missing.label + ' is required.'; return; }
          done(values);
        };
        element.querySelector('[data-cancel]').onclick = ()=>done(null);
        onReady?.(element);
        if(isCurrent(element)) element.querySelector('input,select,textarea')?.focus();
      });
    }

    function pick({title='Choose an agent', placeholder='Search…', items}){
      const prefix = 'picker-' + (++sequence) + '-';
      return overlay(`<div class="modal picker" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <header class="picker-head"><input data-search aria-label="${esc(title)}" placeholder="${esc(placeholder)}" autocomplete="off"/><kbd>Esc</kbd></header>
        <div class="picker-results" data-results></div><footer class="picker-hint">↑ ↓ to choose · Enter to open</footer>
      </div>`, (element,done)=>{
        const search = element.querySelector('[data-search]'), results = element.querySelector('[data-results]');
        const available = items.filter(item=>!item.disabled).map(item=>({...item,title:item.label,subtitle:item.detail}));
        let matches = [], selected = 0;
        function render(){
          matches = UI.filterCommands(available, search.value);
          selected = Math.min(selected, Math.max(0,matches.length-1));
          results.innerHTML = matches.length ? matches.map((item,index)=>`<button type="button" id="${prefix+index}" class="picker-row${selected === index ? ' is-selected' : ''}" data-pick="${index}"><span>${esc(item.label)}</span><small>${esc(item.detail||'')}</small></button>`).join('')
            : '<p class="empty-hint">No matching agents or actions.</p>';
          results.querySelectorAll('[data-pick]').forEach(button=>{
            button.onclick = ()=>done(matches[Number(button.dataset.pick)].value);
          });
          results.querySelector('.is-selected')?.scrollIntoView?.({block:'nearest'});
        }
        search.oninput = ()=>{ selected = 0; render(); };
        search.onkeydown = event=>{
          if(event.isComposing) return;
          if(event.key === 'ArrowDown' || event.key === 'ArrowUp'){
            event.preventDefault();
            selected = UI.moveSelection(selected, event.key === 'ArrowDown' ? 1 : -1, matches.length); render();
          } else if(event.key === 'Enter' && matches[selected]){ event.preventDefault(); done(matches[selected].value); }
        };
        render(); search.focus();
      }, 'picker');
    }

    function menu(anchor, items){
      if(dead) return;
      closeMenu();
      const element = document.createElement('div'); element.className = 'context-menu';
      element.setAttribute('role','menu');
      for(const item of items){
        if(item.separator){ const line = document.createElement('hr'); element.appendChild(line); continue; }
        const button = document.createElement('button'); button.type = 'button'; button.setAttribute('role','menuitem');
        button.textContent = item.label; button.disabled = !!item.disabled; button.className = item.danger ? 'danger' : '';
        button.onclick = ()=>{
          if(button.disabled) return;
          closeMenu();
          Promise.resolve().then(()=>{if(!dead)return item.action?.();}).catch(error=>notice(error.message || 'Action failed.'));
        };
        element.appendChild(button);
      }
      document.body.appendChild(element);
      const box = anchor.getBoundingClientRect();
      element.style.left = Math.max(8,Math.min(box.right-element.offsetWidth,window.innerWidth-element.offsetWidth-8))+'px';
      element.style.top = Math.max(8,Math.min(box.bottom+5,window.innerHeight-element.offsetHeight-8))+'px';
      const outside = event=>{ if(!element.contains(event.target) && !anchor.contains(event.target)) closeMenu(); };
      const key = event=>{ if(event.key === 'Escape'){ event.preventDefault(); closeMenu(); if(anchor.isConnected) anchor.focus(); } };
      menuState = {element,outside,key};
      document.addEventListener('pointerdown',outside,true); document.addEventListener('keydown',key);
      [...element.querySelectorAll('button')].find(button=>!button.disabled)?.focus();
    }

    function notice(message){
      if(dead) return;
      if(toastTimer) window.clearTimeout(toastTimer);
      toast?.remove(); toast = document.createElement('div'); toast.className = 'toast'; toast.setAttribute('role','status');
      toast.textContent = String(message); document.body.appendChild(toast);
      toastTimer = window.setTimeout(()=>{ toast?.remove(); toast=null; toastTimer=null; },4500);
    }

    function destroy(){ close(); dead=true; if(toastTimer) window.clearTimeout(toastTimer); toast?.remove(); toast=null; toastTimer=null; }
    return {modal,alert,confirm,form,pick,menu,notice,close,closeMenu,isCurrent,
      isPicker:()=>active?.kind === 'picker', isOpen:()=>!!active, destroy};
  }
  return {createDialogs};
});
