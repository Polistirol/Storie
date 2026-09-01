/**
 * Carica visual/config/graph.json e applica etichette + valori di default ai controlli UI.
 */
(function () {
  const CONFIG_URL = 'config/graph.json';

  /** Percorso annidato in oggetto: getPath(obj, 'simulation.linkDistance') */
  function getPath(obj, path) {
    return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
  }

  /** Mappa chiavi labels → selettore CSS (testo visibile). */
  const LABEL_SELECTORS = {
    'app.title': 'title',
    'app.loading': '#loading',
    'leftPanel.title': '#panel-left .panel-heading h1',
    'leftPanel.subtitle': '#panel-left .panel-heading .subtitle',
    'leftPanel.centralNode': '#retrieval-panel h2',
    'leftPanel.pickFolder': '#layout-csv-pick',
    'leftPanel.layoutAnimMs': '#layout-anim-ms-label',
    'leftPanel.layoutLinkWidth': '#layout-link-width-label',
    'leftPanel.layoutHint': '#layout-options-hint',
    'leftPanel.releaseLayout': '#layout-panel-release',
    'leftPanel.noLayout': '#layout-panel-status',
    'leftPanel.loadingMaps': '#layout-list-loading',
    'rightPanel.title': '#panel-right .panel-heading h1',
    'rightPanel.subtitle': '#panel-right .panel-heading .subtitle',
    'rightPanel.simulation': '#section-simulation summary',
    'rightPanel.breatheSection': '#section-breathe summary',
    'rightPanel.breatheEnabled': '#breathe-enabled-label',
    'rightPanel.breatheAlphaTarget': '#breathe-alpha-target-label',
    'rightPanel.breatheForceStrength': '#breathe-force-strength-label',
    'rightPanel.breatheSpeed': '#breathe-speed-label',
    'rightPanel.breatheVelocityDecay': '#breathe-velocity-decay-label',
    'rightPanel.breatheAlphaDecay': '#breathe-alpha-decay-label',
    'rightPanel.breatheHint': '#breathe-hint',
    'rightPanel.linkDistance': '#sim-link-distance-label',
    'rightPanel.chargeStrength': '#sim-charge-label',
    'rightPanel.fixDragged': '#sim-fix-dragged-label',
    'rightPanel.nodeGlow': '#sim-node-glow-enabled-label',
    'rightPanel.glowBlur': '#sim-node-glow-blur-label',
    'rightPanel.glowAlpha': '#sim-node-glow-alpha-label',
    'rightPanel.interaction': '#section-interaction summary',
    'rightPanel.selectionEnabled': '#selection-highlight-enabled-label',
    'rightPanel.selectionDegree': '#selection-highlight-degree-label',
    'rightPanel.selectionHint': '#selection-hint',
    'rightPanel.eraSection': '#section-era summary',
    'rightPanel.nodeSizeSection': '#section-node-size summary',
    'rightPanel.nodeSizeByType': '#node-size-mode-type-label',
    'rightPanel.nodeSizeByDegree': '#node-size-mode-degree-label',
    'rightPanel.degreeTableFrom': '#degree-size-table th:nth-child(2)',
    'rightPanel.degreeTableTo': '#degree-size-table th:nth-child(3)',
    'rightPanel.degreeTableSize': '#degree-size-table th:nth-child(4)',
    'rightPanel.degreeHint': '#degree-size-hint',
    'rightPanel.nodeTypesSection': '#section-node-types summary',
    'rightPanel.nodeTypeCol': '#node-types-table th:nth-child(1)',
    'rightPanel.nodeSizeCol': '#node-types-table th:nth-child(2)',
    'rightPanel.nodeColorCol': '#node-types-table th:nth-child(3)',
    'rightPanel.edgesSection': '#section-edges summary',
    'rightPanel.showLinks': '#global-show-links-label',
    'rightPanel.coloredLinks': '#global-colored-links-label',
    'rightPanel.curvedLinks': '#global-curved-label',
    'rightPanel.curvature': '#global-curvature-label',
    'rightPanel.linkWidth': '#global-link-width-label',
    'rightPanel.arrows': '#global-arrows-label',
    'rightPanel.particles': '#global-particles-label',
    'chat.title': '.chat-dock-title',
    'chat.placeholder': '#chat-input',
    'chat.send': '#chat-send',
    'chat.clear': '#chat-clear',
    'chat.connecting': '#chat-status'
  };

  /** Mappa input → percorso in defaults. */
  const INPUT_BINDINGS = [
    { id: 'layout-anim-ms', path: 'layout.animMs', type: 'number' },
    { id: 'layout-link-width', path: 'edges.linkWidthLayout', type: 'number' },
    { id: 'breathe-enabled', path: 'simulation.breathe.enabled', type: 'checkbox' },
    { id: 'breathe-alpha-target', path: 'simulation.breathe.alphaTarget', type: 'number' },
    { id: 'breathe-force-strength', path: 'simulation.breathe.forceStrength', type: 'number' },
    { id: 'breathe-speed', path: 'simulation.breathe.speed', type: 'number' },
    { id: 'breathe-velocity-decay', path: 'simulation.breathe.velocityDecay', type: 'number' },
    { id: 'breathe-alpha-decay', path: 'simulation.breathe.alphaDecay', type: 'number' },
    { id: 'sim-link-distance', path: 'simulation.linkDistance', type: 'number' },
    { id: 'sim-charge', path: 'simulation.chargeStrength', type: 'number' },
    { id: 'sim-fix-dragged', path: 'simulation.fixDraggedNodes', type: 'checkbox' },
    { id: 'sim-node-glow-enabled', path: 'simulation.nodeGlow.enabled', type: 'checkbox' },
    { id: 'sim-node-glow-blur', path: 'simulation.nodeGlow.blur', type: 'number' },
    { id: 'sim-node-glow-alpha', path: 'simulation.nodeGlow.alpha', type: 'number' },
    { id: 'selection-highlight-enabled', path: 'selection.enabled', type: 'checkbox' },
    { id: 'selection-highlight-degree', path: 'selection.degree', type: 'number' },
    { id: 'global-show-links', path: 'edges.showLinks', type: 'checkbox' },
    { id: 'global-colored-links', path: 'edges.coloredLinks', type: 'checkbox' },
    { id: 'global-curved', path: 'edges.curved', type: 'checkbox' },
    { id: 'global-curvature', path: 'edges.curvature', type: 'number' },
    { id: 'global-link-width', path: 'edges.linkWidth', type: 'number' },
    { id: 'global-arrows', path: 'edges.arrows', type: 'checkbox' },
    { id: 'global-particles', path: 'edges.particles', type: 'checkbox' }
  ];

  function flattenLabels(labels, prefix = '') {
    const out = {};
    for (const [key, val] of Object.entries(labels || {})) {
      const path = prefix ? `${prefix}.${key}` : key;
      if (val && typeof val === 'object' && !Array.isArray(val)) {
        Object.assign(out, flattenLabels(val, path));
      } else {
        out[path] = val;
      }
    }
    return out;
  }

  function applyLabels(labels) {
    const flat = flattenLabels(labels);
    for (const [key, text] of Object.entries(flat)) {
      const sel = LABEL_SELECTORS[key];
      if (!sel || text == null) continue;
      document.querySelectorAll(sel).forEach(el => {
        if (el.tagName === 'INPUT' && el.type === 'text') {
          el.placeholder = text;
        } else if (el.tagName === 'TEXTAREA') {
          el.placeholder = text;
        } else {
          el.textContent = text;
        }
      });
    }
  }

  function applyInputDefaults(defaults) {
    for (const { id, path, type } of INPUT_BINDINGS) {
      const el = document.getElementById(id);
      const val = getPath(defaults, path);
      if (!el || val === undefined) continue;
      if (type === 'checkbox') {
        el.checked = !!val;
      } else {
        el.value = val;
      }
    }

    const layoutDir = getPath(defaults, 'paths.layoutDir');
    const dirInput = document.getElementById('layout-csv-dir');
    if (dirInput && layoutDir) {
      dirInput.value = layoutDir.endsWith('/') ? layoutDir : layoutDir + '/';
    }

    const mode = getPath(defaults, 'nodeSize.mode');
    if (mode) {
      document.querySelectorAll('input[name="node-size-mode"]').forEach(input => {
        input.checked = input.value === mode;
      });
    }

    const glowEnabled = getPath(defaults, 'simulation.nodeGlow.enabled');
    const glowBlur = document.getElementById('sim-node-glow-blur');
    const glowAlpha = document.getElementById('sim-node-glow-alpha');
    if (glowBlur && glowAlpha) {
      const disabled = !glowEnabled;
      glowBlur.disabled = disabled;
      glowAlpha.disabled = disabled;
    }

    const breatheEnabled = getPath(defaults, 'simulation.breathe.enabled');
    document.querySelectorAll('#section-breathe input[type="number"]').forEach(el => {
      el.disabled = !breatheEnabled;
    });
  }

  async function load(url = CONFIG_URL) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`Config non trovato: ${url}`);
    const cfg = await r.json();
    applyLabels(cfg.labels);
    applyInputDefaults(cfg.defaults);
    return cfg;
  }

  window.GraphConfig = { load, applyLabels, applyInputDefaults, getPath };
})();
