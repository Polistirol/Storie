/**
 * Chat Adriano — client SSE verso inference/server.py
 */
(function () {
  const params = new URLSearchParams(window.location.search);
  const API_BASE = (params.get('api') || localStorage.getItem('adriano_api') || 'http://127.0.0.1:8000').replace(/\/$/, '');

  let sessionId = '';
  let busy = false;
  let lastCentralNodeId = '';

  const els = {};

  function $(id) {
    return document.getElementById(id);
  }

  /** Solo in memoria — ogni refresh = nuova chat (modello server resta caldo). */
  function persistSession(id) {
    sessionId = id || '';
  }

  /** All'avvio: scarta sessione precedente (localStorage + cleanup server). */
  async function resetChatOnPageLoad() {
    const staleId = localStorage.getItem('adriano_session_id');
    localStorage.removeItem('adriano_session_id');
    sessionId = '';
    lastCentralNodeId = '';
    window.RetrievalPanel?.clear?.();
    if (staleId) {
      try {
        await fetch(`${API_BASE}/api/chat/clear`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: staleId })
        });
      } catch (_) { /* server offline ok */ }
    }
  }

  function setStatus(text, kind) {
    if (!els.status) return;
    els.status.textContent = text;
    els.status.dataset.kind = kind || '';
  }

  function appendMessage(role, text, extraClass) {
    const row = document.createElement('div');
    row.className = 'chat-msg chat-msg-' + role + (extraClass ? ' ' + extraClass : '');
    const label = document.createElement('div');
    label.className = 'chat-msg-label';
    label.textContent = role === 'user' ? 'You' : 'Adriano';
    const body = document.createElement('div');
    body.className = 'chat-msg-body';
    body.textContent = text;
    row.appendChild(label);
    row.appendChild(body);
    els.messages.appendChild(row);
    els.messages.scrollTop = els.messages.scrollHeight;
    return body;
  }

  function setBusy(on) {
    busy = on;
    els.input.disabled = on;
    els.send.disabled = on;
    els.clear.disabled = on;
  }

  /** Parse SSE da stream fetch (POST). */
  async function consumeSse(response, handlers) {
    if (!response.ok) {
      const errText = await response.text();
      throw new Error(errText || `HTTP ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let sep;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const block = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        let event = 'message';
        let data = '';
        for (const line of block.split('\n')) {
          if (line.startsWith('event:')) event = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        }
        if (data && handlers[event]) handlers[event](JSON.parse(data));
      }
    }
  }

  async function sendMessage() {
    const text = els.input.value.trim();
    if (!text || busy) return;

    els.input.value = '';
    appendMessage('user', text);
    const replyBody = appendMessage('assistant', '…', 'chat-msg-streaming');
    setBusy(true);
    window.RetrievalPanel?.pending?.();
    setStatus('Retrieval…', 'busy');

    let reply = '';

    try {
      const res = await fetch(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, session_id: sessionId || null })
      });

      await consumeSse(res, {
        retrieval(data) {
          if (data.session_id) persistSession(data.session_id);
          if (data.central_node_id) lastCentralNodeId = data.central_node_id;
          window.RetrievalPanel?.update?.(data);
          const t = data.timings?.total_s;
          setStatus(
            data.central_node_id
              ? (t != null ? `${data.central_node_id} · ${t}s` : data.central_node_id)
              : (t != null ? `Retrieval ${t}s` : 'Ready'),
            'ok'
          );
        },
        token(data) {
          reply += data.t || '';
          replyBody.textContent = reply;
          els.messages.scrollTop = els.messages.scrollHeight;
        },
        done(data) {
          if (data.session_id) persistSession(data.session_id);
          replyBody.classList.remove('chat-msg-streaming');
          setStatus(lastCentralNodeId || 'Ready', lastCentralNodeId ? 'ok' : 'ok');
        },
        error(data) {
          throw new Error(data.message || 'Server error');
        }
      });
    } catch (err) {
      replyBody.textContent = reply || `(error: ${err.message})`;
      replyBody.classList.add('chat-msg-error');
      setStatus(err.message, 'error');
    } finally {
      setBusy(false);
      if (!replyBody.textContent || replyBody.textContent === '…') {
        replyBody.textContent = '(no reply)';
      }
    }
  }

  async function clearChat() {
    if (busy) return;
    if (sessionId) {
      try {
        await fetch(`${API_BASE}/api/chat/clear`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId })
        });
      } catch (_) { /* ignore */ }
    }
    persistSession('');
    lastCentralNodeId = '';
    els.messages.innerHTML = '';
    window.RetrievalPanel?.clear?.();
    setStatus('History cleared', 'ok');
  }

  async function pingHealth() {
    try {
      const r = await fetch(`${API_BASE}/api/health`);
      if (!r.ok) throw new Error('offline');
      const j = await r.json();
      setStatus(j.backend || (j.model ? `Connected · ${j.model}` : 'Connected'), 'ok');
    } catch {
      setStatus(`API unreachable (${API_BASE})`, 'error');
    }
  }

  async function init() {
    els.messages = $('chat-messages');
    els.input = $('chat-input');
    els.send = $('chat-send');
    els.clear = $('chat-clear');
    els.status = $('chat-status');
    els.panel = $('chat-panel');
    els.toggle = $('chat-toggle');
    if (!els.messages) return;

    await resetChatOnPageLoad();

    els.toggle?.addEventListener('click', () => {
      const collapsed = els.panel.classList.toggle('collapsed');
      els.toggle.textContent = collapsed ? '▴' : '▾';
      els.toggle.title = collapsed ? 'Expand messages' : 'Collapse messages';
    });

    els.send.addEventListener('click', sendMessage);
    els.clear.addEventListener('click', clearChat);
    els.input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    pingHealth();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
