/**
 * OI Conditions Dashboard - Live Client Controller
 * Renders indices (ATM ±1 summed), top 5 bullish stocks, top 5 bearish stocks,
 * strike detail table, live alerts feed, and real-time clock.
 */

(function () {
  'use strict';

  // State
  let activeIndexKey = 'NSE_INDEX|Nifty 50';
  let lastData = null;
  let sseSource = null;
  let updateCycle = 1;
  let countdownSec = 180; // 3 minutes standard OI cycle

  // Elements
  const elClock = document.getElementById('clock-display');
  const elFeedPill = document.getElementById('feed-status-pill');
  const elFeedDot = document.getElementById('feed-status-dot');
  const elFeedText = document.getElementById('feed-status-text');
  const elBaseline = document.getElementById('baseline-display');
  const elOiUpdate = document.getElementById('oi-update-display');
  const elMode = document.getElementById('mode-display');
  const elTradesCounter = document.getElementById('trades-counter-text');

  const tbodyIndices = document.getElementById('tbody-indices');
  const tbodyBullish = document.getElementById('tbody-stocks-bullish');
  const tbodyBearish = document.getElementById('tbody-stocks-bearish');
  const tbodyStrikes = document.getElementById('tbody-strikes') || document.getElementById('tbody-strike-detail');
  const elStrikeTitle = document.getElementById('strike-table-title') || document.getElementById('strike-detail-title');
  const alertsContainer = document.getElementById('alerts-container');

  // Formatters
  function fmtNum(n, dec = 2, prefix = '') {
    if (n === null || n === undefined || isNaN(n)) return '<span class="text-dim">-</span>';
    const num = Number(n);
    const sign = prefix && num > 0 ? '+' : '';
    return `<span class="font-bold text-bright">${sign}${num.toFixed(dec)}</span>`;
  }

  function fmtI(n) {
    if (n === null || n === undefined || isNaN(n)) return '<span class="text-dim">-</span>';
    const num = Number(n);
    const sign = num > 0 ? '+' : '';
    const cls = num > 0 ? 'text-cyan font-bold' : (num < 0 ? 'text-amber font-bold' : 'text-muted font-bold');
    return `<span class="${cls}">${sign}${num.toFixed(2)}</span>`;
  }

  function fmtPct(n) {
    if (n === null || n === undefined || isNaN(n)) return '<span class="text-dim">-</span>';
    const num = Number(n);
    const sign = num > 0 ? '+' : '';
    const cls = num > 0 ? 'text-green' : (num < 0 ? 'text-red' : 'text-muted');
    return `<span class="${cls}">${sign}${num.toFixed(2)}%</span>`;
  }

  function fmtLakh(n) {
    if (n === null || n === undefined || isNaN(n)) return '<span class="text-dim">-</span>';
    const num = Number(n);
    const lakh = num / 100000.0;
    const sign = lakh > 0 ? '+' : '';
    const cls = lakh > 0 ? 'text-green' : (lakh < 0 ? 'text-red' : 'text-muted');
    return `<span class="${cls}">${sign}${lakh.toFixed(2)} L</span>`;
  }

  function fmtBuildup(bu) {
    if (!bu || bu === 'Neutral') return `<span class="bu-badge bu-neu">NEU</span>`;
    const b = String(bu).toUpperCase().trim();
    if (b === 'LB') return `<span class="bu-badge bu-lb">LB</span>`;
    if (b === 'SB') return `<span class="bu-badge bu-sb">SB</span>`;
    if (b === 'SC') return `<span class="bu-badge bu-sc">SC</span>`;
    if (b === 'LU') return `<span class="bu-badge bu-lu">LU</span>`;
    return `<span class="bu-badge bu-neu">NEU</span>`;
  }

  function fmtGate(gate) {
    if (!gate || gate === 'OFF') return `<span class="gate-off">OFF</span>`;
    if (gate === 'Pass') return `<span class="gate-pass">Pass</span>`;
    return `<span class="gate-block">Block</span>`;
  }

  function fmtAction(action, side) {
    if (!action || action === 'NO_TRADE' || action === 'WAIT') {
      return `<span class="act-badge act-no-trade">NO TRADE</span>`;
    }
    if (action.includes('ENTER CE') || side === 'CE' || side === 'BUY_CE') {
      return `<span class="act-badge act-enter-ce">ENTER CE</span>`;
    }
    if (action.includes('ENTER PE') || side === 'PE' || side === 'BUY_PE') {
      return `<span class="act-badge act-enter-pe">ENTER PE</span>`;
    }
    if (action.includes('EXIT 1/2') || action.includes('HALF')) {
      return `<span class="act-badge act-exit-half">EXIT 1/2</span>`;
    }
    return `<span class="act-badge act-no-trade">${action}</span>`;
  }

  function renderIndexAction(idx) {
    const key = idx.key || idx.name || '';
    const rawName = idx.name || key || 'INDEX';
    let shortName = rawName.replace(' 50', '').replace('NSE_INDEX|', '').replace('BSE_INDEX|', '').replace(' ', '');
    const act = (idx.action || '').toUpperCase();
    const dec = (idx.decision || '').toUpperCase();

    if (act.includes('EXIT') || idx.current_position || (idx.hold && idx.hold !== '-' && idx.hold !== '')) {
      return `<button class="btn-table-action act-exit-half" data-action="exit" data-key="${key}" title="Exit open position">EXIT 1/2</button>`;
    }
    if (act.includes('ENTER CE') || dec.includes('CE BUY')) {
      return `<button class="btn-table-action act-enter-ce" data-action="enter" data-key="${key}" data-side="CE" title="Execute Strategy CE Entry">ENTER CE</button>`;
    }
    if (act.includes('ENTER PE') || dec.includes('PE BUY')) {
      return `<button class="btn-table-action act-enter-pe" data-action="enter" data-key="${key}" data-side="PE" title="Execute Strategy PE Entry">ENTER PE</button>`;
    }
    return `<button class="btn-table-action act-arm" data-action="arm" data-key="${key}" title="Arm ${shortName} now">ARM ${shortName.toUpperCase()}</button>`;
  }

  function renderStockAction(st) {
    const sym = st.symbol || '';
    const act = (st.action || '').toUpperCase();
    const dec = (st.decision || '').toUpperCase();

    if (act.includes('EXIT')) {
      return `<button class="btn-table-action act-exit-half" data-action="exit" data-key="${sym}" title="Exit ${sym}">EXIT 1/2</button>`;
    }
    if (act.includes('ENTER CE') || dec.includes('CE BUY')) {
      return `<button class="btn-table-action act-enter-ce" data-action="enter" data-key="${sym}" data-side="CE" title="Enter CE for ${sym}">ENTER CE</button>`;
    }
    if (act.includes('ENTER PE') || dec.includes('PE BUY')) {
      return `<button class="btn-table-action act-enter-pe" data-action="enter" data-key="${sym}" data-side="PE" title="Enter PE for ${sym}">ENTER PE</button>`;
    }
    return `<span class="act-badge act-no-trade">NO TRADE</span>`;
  }

  // 1. Render Indices Table (Sum Signal Strategy)
  function renderIndices(indices) {
    if (!tbodyIndices) return;
    if (!indices || !indices.length) {
      tbodyIndices.innerHTML = `<tr><td colspan="12" class="text-center text-muted">Awaiting index data...</td></tr>`;
      return;
    }

    let html = '';
    indices.forEach(idx => {
      const name = idx.name || idx.key || 'INDEX';
      const spot = idx.spot_price ? Number(idx.spot_price).toFixed(2) : '-';
      const atm = idx.atm_strike ? Number(idx.atm_strike) : '-';
      const changePct = idx.net_change_pct || 0.0;
      const spotCls = changePct >= 0 ? 'text-green' : 'text-red';
      const changeSign = changePct >= 0 ? '+' : '';

      const ceBase = idx.ce_baseline_sum ? Number(idx.ce_baseline_sum).toFixed(2) : '-';
      const ceCurr = idx.ce_current_sum ? Number(idx.ce_current_sum).toFixed(2) : '-';
      const cePct = idx.ce_pct !== undefined ? idx.ce_pct : 0.0;

      const peBase = idx.pe_baseline_sum ? Number(idx.pe_baseline_sum).toFixed(2) : '-';
      const peCurr = idx.pe_current_sum ? Number(idx.pe_current_sum).toFixed(2) : '-';
      const pePct = idx.pe_pct !== undefined ? idx.pe_pct : 0.0;

      const score = idx.score !== undefined ? idx.score : (cePct - pePct);
      const scoreSign = score > 0 ? '+' : '';
      const scoreCls = score > 2.0 ? 'text-green font-bold' : (score < -2.0 ? 'text-red font-bold' : 'text-muted font-bold');

      const rawSig = idx.raw_signal || 'Neutral';
      let rawSigBadge = '<span class="text-dim">Neutral</span>';
      if (rawSig === 'CE Buy') {
        rawSigBadge = '<span class="bu-badge bu-lb font-bold">CE Buy</span>';
      } else if (rawSig === 'PE Buy') {
        rawSigBadge = '<span class="bu-badge bu-sb font-bold">PE Buy</span>';
      }

      let holdBadge = '<span class="text-dim">-</span>';
      if (idx.confirmed_signal) {
        holdBadge = `<span class="gate-pass font-bold">CONFIRMED (${Math.round(idx.whipsaw_sec || 300)}s)</span>`;
      } else if (idx.hold_elapsed_sec > 0) {
        holdBadge = `<span class="text-cyan font-bold">${Math.round(idx.hold_elapsed_sec)}s / ${Math.round(idx.whipsaw_sec || 300)}s</span>`;
      }

      let decBadge = '<span class="text-dim">No trade</span>';
      if (idx.confirmed_signal === 'CE Buy' || idx.decision === 'CE Buy') {
        decBadge = '<span class="text-green font-bold">CE Buy</span>';
      } else if (idx.confirmed_signal === 'PE Buy' || idx.decision === 'PE Buy') {
        decBadge = '<span class="text-red font-bold">PE Buy</span>';
      } else if (rawSig !== 'Neutral') {
        decBadge = `<span class="text-amber font-bold">${rawSig} (Holding)</span>`;
      }

      html += `<tr>
        <td class="text-left">
          <strong class="font-bold text-bright">${name}</strong><br>
          <span class="${spotCls}">${spot} (${changeSign}${changePct.toFixed(2)}%)</span>
          <span class="text-dim font-bold">&middot; ATM ${atm}</span>
        </td>
        
        <!-- CE BASKET -->
        <td class="text-right">${ceBase}</td>
        <td class="text-right font-bold text-bright">${ceCurr}</td>
        <td class="text-right">${fmtPct(cePct)}</td>

        <!-- PE BASKET -->
        <td class="text-right">${peBase}</td>
        <td class="text-right font-bold text-bright">${peCurr}</td>
        <td class="text-right">${fmtPct(pePct)}</td>

        <!-- SUM SIGNAL -->
        <td class="text-center"><span class="${scoreCls}">${scoreSign}${Number(score).toFixed(2)}%</span></td>
        <td class="text-center">${rawSigBadge}</td>

        <!-- CONFIRMATION & DECISION -->
        <td class="text-center">${holdBadge}</td>
        <td class="text-center">${decBadge}</td>
        <td class="text-center">${renderIndexAction(idx)}</td>
      </tr>`;
    });

    tbodyIndices.innerHTML = html;
  }

  // 2. Render Top Bullish Stocks (All Live Engine & Screener Bound)
  function renderBullishStocks(stocks) {
    if (!tbodyBullish) return;
    const headingEl = document.getElementById('heading-count-bullish');
    if (headingEl && stocks) headingEl.textContent = stocks.length;

    if (!stocks || !stocks.length) {
      tbodyBullish.innerHTML = `<tr><td colspan="15" class="text-center text-muted">Awaiting bullish screener data...</td></tr>`;
      return;
    }

    let html = '';
    stocks.forEach((st, i) => {
      const sym = st.symbol || 'STOCK';
      const score = st.score !== undefined && st.score !== null ? st.score : (st.clv !== undefined ? Math.round(st.clv * 100) : null);
      const scoreDisp = score !== null ? (score > 0 ? `+${score}` : `${score}`) : '-';

      const spot = (st.spot_price && Number(st.spot_price) > 0)
        ? `₹${Number(st.spot_price).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
        : '-';
      const changePct = st.net_change_pct !== undefined && st.net_change_pct !== null ? Number(st.net_change_pct) : Number(st.change_pct || 0);
      const spotCls = changePct >= 0 ? 'text-green font-mono' : 'text-red font-mono';
      const changeSign = changePct >= 0 ? '+' : '';
      const gapDisp = st.gap_pct !== undefined && st.gap_pct !== null ? `${st.gap_pct > 0 ? '+' : ''}${Number(st.gap_pct).toFixed(2)}%` : '-';
      const rvolDisp = st.rvol ? `${Number(st.rvol).toFixed(1)}x` : '-';
      const atmDisp = st.atm_strike ? `ATM ${Number(st.atm_strike).toLocaleString()}` : '';

      html += `<tr>
        <td class="text-center text-dim font-bold">${i + 1}</td>
        <td class="text-left">
          <strong class="font-bold text-bright">${sym}</strong><br>
          <span class="${spotCls}">${spot} (${changeSign}${changePct.toFixed(2)}%)</span>
          <span class="text-dim font-bold">&middot; Gap ${gapDisp} &middot; RVOL ${rvolDisp}${atmDisp ? ' &middot; ' + atmDisp : ''}</span>
        </td>
        <td class="text-right text-green font-bold">${scoreDisp}</td>
        
        <!-- CE -->
        <td class="text-right">${fmtPct(st.ce_oi_pct !== undefined ? st.ce_oi_pct : st.delta_ce_pct)}</td>
        <td class="text-right">${fmtPct(st.ce_price_pct)}</td>
        <td class="text-center">${fmtBuildup(st.ce_buildup)}</td>

        <!-- PE -->
        <td class="text-right">${fmtPct(st.pe_oi_pct !== undefined ? st.pe_oi_pct : st.delta_pe_pct)}</td>
        <td class="text-right">${fmtPct(st.pe_price_pct)}</td>
        <td class="text-center">${fmtBuildup(st.pe_buildup)}</td>

        <!-- OPTION -->
        <td class="text-right">${fmtI(st.oi_balance_i)}</td>
        <td class="text-center font-bold text-bright">${st.rule_number ? st.rule_number : '-'}</td>

        <!-- FUTURES -->
        <td class="text-right">${fmtPct(st.futures_price_pct !== undefined ? st.futures_price_pct : st.change_pct)}</td>
        <td class="text-right">${fmtPct(st.futures_oi_pct !== undefined ? st.futures_oi_pct : st.fut_oi_change_pct)}</td>
        <td class="text-center">${fmtBuildup(st.fut_buildup || st.futures_buildup)}</td>

        <!-- DECISION -->
        <td class="text-center font-bold">
          <span class="text-gold">${st.rank || '★★'}</span> ${st.size || 'Normal'}
        </td>
        <td class="text-center">${renderStockAction(st)}</td>
      </tr>`;
    });

    tbodyBullish.innerHTML = html;
  }

  // 3. Render Top Bearish Stocks (All Live Engine & Screener Bound)
  function renderBearishStocks(stocks) {
    if (!tbodyBearish) return;
    const headingEl = document.getElementById('heading-count-bearish');
    if (headingEl && stocks) headingEl.textContent = stocks.length;

    if (!stocks || !stocks.length) {
      tbodyBearish.innerHTML = `<tr><td colspan="15" class="text-center text-muted">Awaiting bearish screener data...</td></tr>`;
      return;
    }

    let html = '';
    stocks.forEach((st, i) => {
      const sym = st.symbol || 'STOCK';
      const score = st.score !== undefined && st.score !== null ? st.score : (st.clv !== undefined ? Math.round((st.clv - 1) * 100) : null);
      const scoreDisp = score !== null ? (score > 0 ? `+${score}` : `${score}`) : '-';

      const spot = (st.spot_price && Number(st.spot_price) > 0)
        ? `₹${Number(st.spot_price).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`
        : '-';
      const changePct = st.net_change_pct !== undefined && st.net_change_pct !== null ? Number(st.net_change_pct) : Number(st.change_pct || 0);
      const spotCls = changePct >= 0 ? 'text-green font-mono' : 'text-red font-mono';
      const changeSign = changePct >= 0 ? '+' : '';
      const gapDisp = st.gap_pct !== undefined && st.gap_pct !== null ? `${st.gap_pct > 0 ? '+' : ''}${Number(st.gap_pct).toFixed(2)}%` : '-';
      const rvolDisp = st.rvol ? `${Number(st.rvol).toFixed(1)}x` : '-';
      const atmDisp = st.atm_strike ? `ATM ${Number(st.atm_strike).toLocaleString()}` : '';

      html += `<tr>
        <td class="text-center text-dim font-bold">${i + 1}</td>
        <td class="text-left">
          <strong class="font-bold text-bright">${sym}</strong><br>
          <span class="${spotCls}">${spot} (${changeSign}${changePct.toFixed(2)}%)</span>
          <span class="text-dim font-bold">&middot; Gap ${gapDisp} &middot; RVOL ${rvolDisp}${atmDisp ? ' &middot; ' + atmDisp : ''}</span>
        </td>
        <td class="text-right text-red font-bold">${scoreDisp}</td>
        
        <!-- CE -->
        <td class="text-right">${fmtPct(st.ce_oi_pct !== undefined ? st.ce_oi_pct : st.delta_ce_pct)}</td>
        <td class="text-right">${fmtPct(st.ce_price_pct)}</td>
        <td class="text-center">${fmtBuildup(st.ce_buildup)}</td>

        <!-- PE -->
        <td class="text-right">${fmtPct(st.pe_oi_pct !== undefined ? st.pe_oi_pct : st.delta_pe_pct)}</td>
        <td class="text-right">${fmtPct(st.pe_price_pct)}</td>
        <td class="text-center">${fmtBuildup(st.pe_buildup)}</td>

        <!-- OPTION -->
        <td class="text-right">${fmtI(st.oi_balance_i)}</td>
        <td class="text-center font-bold text-bright">${st.rule_number ? st.rule_number : '-'}</td>

        <!-- FUTURES -->
        <td class="text-right">${fmtPct(st.futures_price_pct !== undefined ? st.futures_price_pct : st.change_pct)}</td>
        <td class="text-right">${fmtPct(st.futures_oi_pct !== undefined ? st.futures_oi_pct : st.fut_oi_change_pct)}</td>
        <td class="text-center">${fmtBuildup(st.fut_buildup || st.futures_buildup)}</td>

        <!-- DECISION -->
        <td class="text-center font-bold">
          <span class="text-gold">${st.rank || '★★'}</span> ${st.size || 'Normal'}
        </td>
        <td class="text-center">${renderStockAction(st)}</td>
      </tr>`;
    });

    tbodyBearish.innerHTML = html;
  }

  // 4. Render Strike Detail (ATM ±1 or ATM ±3)
  function renderStrikeDetail(strikes, indexName, atmStrike) {
    if (!tbodyStrikes) return;
    if (!strikes || !strikes.length) {
      tbodyStrikes.innerHTML = `<tr><td colspan="13" class="text-center text-muted">Awaiting strike detail for ${indexName}...</td></tr>`;
      return;
    }

    if (elStrikeTitle) {
      elStrikeTitle.innerHTML = `${indexName} &middot; strike detail (ATM &plusmn;1)`;
    }

    let html = '';
    strikes.forEach(s => {
      const isAtm = Number(s.strike) === Number(atmStrike);
      const atmBadge = isAtm ? `<span class="atm-strike-badge">ATM</span>` : '';

      const ceLtp = (s.ce_ltp && Number(s.ce_ltp) > 0)
        ? `<span class="font-mono font-bold text-bright">₹${Number(s.ce_ltp).toFixed(2)}</span>`
        : '<span class="text-dim">-</span>';
      const peLtp = (s.pe_ltp && Number(s.pe_ltp) > 0)
        ? `<span class="font-mono font-bold text-bright">₹${Number(s.pe_ltp).toFixed(2)}</span>`
        : '<span class="text-dim">-</span>';

      html += `<tr class="${isAtm ? 'row-atm' : ''}">
        <!-- CE -->
        <td class="text-right">${fmtLakh(s.ce_oi)}</td>
        <td class="text-right">${fmtLakh(s.ce_delta_oi)}</td>
        <td class="text-right">${fmtPct(s.ce_delta_oi_pct)}</td>
        <td class="text-right">${fmtPct(s.ce_price_pct)}</td>
        <td class="text-right">${ceLtp}</td>
        <td class="text-center">${fmtBuildup(s.ce_buildup)}</td>

        <!-- STRIKE -->
        <td class="text-center font-bold text-bright">${Number(s.strike).toLocaleString()} ${atmBadge}</td>

        <!-- PE -->
        <td class="text-center">${fmtBuildup(s.pe_buildup)}</td>
        <td class="text-right">${peLtp}</td>
        <td class="text-right">${fmtPct(s.pe_price_pct)}</td>
        <td class="text-right">${fmtPct(s.pe_delta_oi_pct)}</td>
        <td class="text-right">${fmtLakh(s.pe_delta_oi)}</td>
        <td class="text-right">${fmtLakh(s.pe_oi)}</td>
      </tr>`;
    });

    tbodyStrikes.innerHTML = html;
  }

  // 5. Append Alert
  function appendAlert(alert) {
    const empty = alertsContainer.querySelector('.alert-empty-state');
    if (empty) empty.remove();

    const div = document.createElement('div');
    div.className = 'alert-item';

    const timeStr = alert.time || alert.timestamp_ist || new Date().toTimeString().split(' ')[0];
    const lvl = (alert.level || alert.action || 'DATA').toUpperCase();

    let tagCls = 'tag-data';
    if (lvl.includes('ENTRY')) tagCls = 'tag-entry';
    else if (lvl.includes('EXIT')) tagCls = 'tag-exit';
    else if (lvl.includes('BLOCK')) tagCls = 'tag-blocked';
    else if (lvl.includes('UPGRADE')) tagCls = 'tag-upgrade';
    else if (lvl.includes('WARN')) tagCls = 'tag-warning';

    div.innerHTML = `
      <span class="alert-time">${timeStr}</span>
      <span class="alert-tag ${tagCls}">${lvl}</span>
      <span class="alert-msg">${alert.message || alert.action_taken || alert.text || ''}</span>
    `;

    alertsContainer.prepend(div);

    // Keep only last 50 alerts in DOM
    while (alertsContainer.children.length > 50) {
      alertsContainer.removeChild(alertsContainer.lastChild);
    }
  }

  // 6. Main Dashboard Refresh
  async function fetchDashboardData() {
    try {
      const res = await fetch('/api/dashboard-data');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      lastData = data;

      // Update feed indicators
      if (data.feed_connected) {
        elFeedDot.className = 'status-dot dot-live';
        elFeedText.textContent = `Feed live (${data.feed_instruments || 0} keys)`;
        if (elFeedPill) elFeedPill.classList.remove('is-disconnected', 'is-error');
      } else {
        elFeedDot.className = 'status-dot dot-error';
        elFeedText.textContent = 'Disconnected';
        if (elFeedPill) {
          elFeedPill.classList.add('is-disconnected');
          elFeedPill.classList.remove('is-error');
        }
      }

      if (data.mode) elMode.textContent = `${data.mode.toUpperCase()} MODE`;
      if (data.baseline_time) elBaseline.textContent = `Baseline: ${data.baseline_time} snapshot`;
      if (data.today_trades !== undefined) {
        elTradesCounter.textContent = `Today's Trades: ${data.today_trades} | Net P&L: ₹${(data.today_net_pnl || 0).toFixed(2)}`;
      }

      // Render Tables
      renderIndices(data.indices || []);
      renderBullishStocks(data.top_bullish || []);
      renderBearishStocks(data.top_bearish || []);

      // Selected Index Strike Detail
      const currentIdx = (data.indices || []).find(x => x.key === activeIndexKey) || (data.indices || [])[0];
      if (currentIdx) {
        renderStrikeDetail(currentIdx.strikes || [], currentIdx.name || 'NIFTY', currentIdx.atm_strike);
      }

      // Alerts
      if (data.recent_alerts && data.recent_alerts.length) {
        data.recent_alerts.reverse().forEach(a => appendAlert(a));
      }

      // Sync Pre-Defined Controls Pills
      if (data.config) {
        syncConfigPills(data.config);
      }

    } catch (err) {
      console.warn('Dashboard poll error:', err);
      elFeedDot.className = 'status-dot dot-error';
      elFeedText.textContent = 'Reconnecting...';
      if (elFeedPill) elFeedPill.classList.add('is-error');
    }
  }

  // 7. Live SSE Connection
  function setupSSE() {
    try {
      sseSource = new EventSource('/api/stream');

      sseSource.onopen = () => {
        elFeedDot.className = 'status-dot dot-live';
        if (elFeedPill) elFeedPill.classList.remove('is-error');
      };

      sseSource.addEventListener('strategy_update', e => {
        try {
          const payload = JSON.parse(e.data);
          // Trigger instant refresh
          fetchDashboardData();
        } catch (_) {}
      });

      sseSource.addEventListener('alert', e => {
        try {
          const alert = JSON.parse(e.data);
          appendAlert(alert);
        } catch (_) {}
      });

      sseSource.addEventListener('tick', () => {
        // Feed is streaming ticks
        elFeedDot.className = 'status-dot dot-live';
        if (elFeedPill) elFeedPill.classList.remove('is-error', 'is-disconnected');
      });

      sseSource.onerror = () => {
        elFeedDot.className = 'status-dot dot-error';
        if (elFeedPill) elFeedPill.classList.add('is-error');
      };
    } catch (e) {
      console.debug('SSE setup failed:', e);
    }
  }

  // 8. Clock & Live Market Interval Synchronizer
  setInterval(() => {
    const now = new Date();
    elClock.textContent = now.toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }) + ' IST';

    // Market 3-minute interval calculation synchronized with exchange clock (Asia/Kolkata)
    const istHours = (now.getUTCHours() + 5 + Math.floor((now.getUTCMinutes() + 30) / 60)) % 24;
    const istMins = (now.getUTCMinutes() + 30) % 60;
    const istSecs = now.getUTCSeconds();
    const curTotalSec = istHours * 3600 + istMins * 60 + istSecs;
    const openSec = 9 * 3600 + 15 * 60; // 09:15:00 IST
    const closeSec = 15 * 3600 + 30 * 60; // 15:30:00 IST

    if (curTotalSec < openSec) {
      const waitSec = openSec - curTotalSec;
      const m = Math.floor(waitSec / 60);
      const s = waitSec % 60;
      elOiUpdate.textContent = `Market opens in ${m}m ${s < 10 ? '0' : ''}${s}s`;
    } else if (curTotalSec > closeSec) {
      elOiUpdate.textContent = `Market closed · EOD`;
    } else {
      const elapsedSec = curTotalSec - openSec;
      const cycleNum = Math.floor(elapsedSec / 180) + 1;
      const nextInSec = 180 - (elapsedSec % 180);
      const m = Math.floor(nextInSec / 60);
      const s = nextInSec % 60;
      elOiUpdate.textContent = `OI update: #${cycleNum} · next ~ ${m}:${s < 10 ? '0' : ''}${s}`;
    }
  }, 1000);

  // 9. Feedback & Quick Control Helpers
  const qcFeedback = document.getElementById('qc-feedback');
  function showFeedback(msg, isError = false) {
    if (!qcFeedback) return;
    qcFeedback.textContent = msg;
    qcFeedback.style.color = isError ? '#ef4444' : '#10b981';
    qcFeedback.style.opacity = '1';
    clearTimeout(qcFeedback._timer);
    qcFeedback._timer = setTimeout(() => {
      qcFeedback.textContent = 'Auto-Saved';
      qcFeedback.style.color = '#94a3b8';
    }, 3500);
  }

  function formatParamName(param) {
    switch (param) {
      case 'max_bullish_stocks': return 'Bullish Stocks';
      case 'max_bearish_stocks': return 'Bearish Stocks';
      case 'lots_full': return 'Full Lots';
      case 'lots_normal': return 'Normal Lots';
      case 'lots_scalp': return 'Scalp Lots';
      default: return param;
    }
  }

  function updatePillLabel(param, val) {
    if (param === 'max_bullish_stocks') {
      const el = document.getElementById('lbl-val-bullish');
      if (el) el.textContent = val;
      const titleEl = document.getElementById('heading-count-bullish');
      if (titleEl) titleEl.textContent = val;
    } else if (param === 'max_bearish_stocks') {
      const el = document.getElementById('lbl-val-bearish');
      if (el) el.textContent = val;
      const titleEl = document.getElementById('heading-count-bearish');
      if (titleEl) titleEl.textContent = val;
    } else if (param === 'lots_full') {
      const el = document.getElementById('lbl-val-full');
      if (el) el.textContent = val;
    } else if (param === 'lots_normal') {
      const el = document.getElementById('lbl-val-normal');
      if (el) el.textContent = val;
    } else if (param === 'lots_scalp') {
      const el = document.getElementById('lbl-val-scalp');
      if (el) el.textContent = val;
    }
  }

  function updateIndexLotBadge(key, val) {
    const badge = document.getElementById(`badge-lots-${key}`);
    if (!badge) return;
    let lotSize = 1;
    if (key.includes('Nifty 50')) lotSize = 65;
    else if (key.includes('Bank')) lotSize = 15;
    else if (key.includes('SENSEX')) lotSize = 10;

    const totalQty = val * lotSize;
    const lotWord = val === 1 ? '1 Lot' : `${val} Lots`;
    badge.textContent = `${lotWord} (${totalQty} Qty)`;
  }

  function syncConfigPills(cfg) {
    if (!cfg) return;
    ['max_bullish_stocks', 'max_bearish_stocks', 'lots_full', 'lots_normal', 'lots_scalp'].forEach(param => {
      if (cfg[param] !== undefined) {
        const val = cfg[param];
        updatePillLabel(param, val);
        const group = document.querySelector(`.qc-group[data-param="${param}"] .pill-group`);
        if (group) {
          group.querySelectorAll('.pill-btn').forEach(btn => {
            if (parseInt(btn.dataset.val, 10) === val) {
              btn.classList.add('active');
            } else {
              btn.classList.remove('active');
            }
          });
        }
      }
    });

    if (cfg.index_lots && typeof cfg.index_lots === 'object') {
      Object.entries(cfg.index_lots).forEach(([ukey, lotVal]) => {
        const val = parseInt(lotVal, 10);
        updateIndexLotBadge(ukey, val);
        const group = document.querySelector(`.pill-group[data-idx-key="${ukey}"]`);
        if (group) {
          group.querySelectorAll('.btn-idx-lot').forEach(btn => {
            if (parseInt(btn.dataset.val, 10) === val) {
              btn.classList.add('active');
            } else {
              btn.classList.remove('active');
            }
          });
        }
      });
    }
  }

  // 10. Event Listeners: Quick Control Pills
  document.querySelectorAll('.pill-group').forEach(group => {
    group.addEventListener('click', async e => {
      const btn = e.target.closest('.pill-btn');
      if (!btn) return;
      const param = btn.dataset.param;
      const val = parseInt(btn.dataset.val, 10);
      if (!param || isNaN(val)) return;

      group.querySelectorAll('.pill-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      updatePillLabel(param, val);

      try {
        const payload = {};
        payload[param] = val;
        await fetch('/api/strategy/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        showFeedback(`Set ${formatParamName(param)} to ${val}`);
        fetchDashboardData();
      } catch (err) {
        showFeedback(`Failed to save: ${err.message}`, true);
      }
    });
  });

  // 11. Event Listeners: Dynamic Action Buttons (ENTER CE / ENTER PE / ARM / EXIT)
  document.addEventListener('click', async e => {
    const btn = e.target.closest('.btn-table-action');
    if (!btn) return;

    const action = btn.dataset.action;
    const key = btn.dataset.key;
    const side = btn.dataset.side || 'CE';

    btn.disabled = true;
    const origText = btn.textContent;
    btn.textContent = '...';
    btn.style.opacity = '0.7';

    try {
      if (action === 'enter') {
        const res = await fetch('/api/strategy/manual-enter', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ underlying_key: key, side: side })
        });
        const d = await res.json();
        if (d.success) {
          showFeedback(`MANUAL ENTRY: ${side} for ${key}`);
        } else {
          showFeedback(`Entry rejected: check spread/filters`, true);
        }
      } else if (action === 'arm') {
        await fetch('/api/strategy/arm-now', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ underlying_key: key })
        });
        showFeedback(`Armed ${key} immediately`);
      } else if (action === 'exit') {
        await fetch('/api/strategy/exit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ underlying_key: key })
        });
        showFeedback(`Exited position for ${key}`);
      }
      fetchDashboardData();
    } catch (err) {
      showFeedback(`Action failed: ${err.message}`, true);
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
      btn.style.opacity = '1.0';
    }
  });

  // 12. Top Header Buttons
  document.querySelectorAll('.btn-arm-idx').forEach(btn => {
    btn.addEventListener('click', async () => {
      const key = btn.dataset.key;
      btn.disabled = true;
      try {
        await fetch('/api/strategy/arm-now', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ underlying_key: key })
        });
        showFeedback(`Armed ${btn.textContent}`);
        fetchDashboardData();
      } catch (err) {
        showFeedback(`Arm error: ${err.message}`, true);
      } finally {
        btn.disabled = false;
      }
    });
  });

  const btnArmAll = document.getElementById('btn-arm-all');
  if (btnArmAll) {
    btnArmAll.addEventListener('click', async () => {
      btnArmAll.disabled = true;
      try {
        await fetch('/api/strategy/arm-now', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ underlying_key: 'INDICES' })
        });
        showFeedback('Armed ALL 3 Indices');
        fetchDashboardData();
      } catch (err) {
        showFeedback(`Arm all error: ${err.message}`, true);
      } finally {
        btnArmAll.disabled = false;
      }
    });
  }

  // 13. Other Header / Tab Event Listeners
  document.getElementById('strike-index-tabs').addEventListener('click', e => {
    const btn = e.target.closest('.tab-btn');
    if (!btn) return;
    document.querySelectorAll('#strike-index-tabs .tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeIndexKey = btn.dataset.index;
    if (lastData && lastData.indices) {
      const idx = lastData.indices.find(x => x.key === activeIndexKey);
      if (idx) {
        renderStrikeDetail(idx.strikes || [], idx.name || 'INDEX', idx.atm_strike);
      }
    }
  });

  document.getElementById('btn-clear-alerts').addEventListener('click', () => {
    alertsContainer.innerHTML = '<div class="alert-empty-state">Alerts cleared. Listening for new events...</div>';
  });

  document.getElementById('btn-re-eval').addEventListener('click', () => {
    fetchDashboardData();
  });

  document.getElementById('btn-emergency-exit').addEventListener('click', async () => {
    if (!confirm('Are you sure you want to emergency squareoff all open positions?')) return;
    try {
      await fetch('/api/strategy/exit?scope=ALL', { method: 'POST' });
      showFeedback('Emergency Squareoff Sent');
      fetchDashboardData();
    } catch (err) {
      showFeedback('Exit error: ' + err.message, true);
    }
  });

  // 14. Top-level Application Tabs (Live Dashboard vs Trade Book vs Activity Logs vs Bot Settings)
  const navBtnDash = document.getElementById('nav-btn-dashboard');
  const navBtnTrades = document.getElementById('nav-btn-trades');
  const navBtnLogs = document.getElementById('nav-btn-logs');
  const navBtnSettings = document.getElementById('nav-btn-settings');
  const paneDash = document.getElementById('tab-dashboard');
  const paneTrades = document.getElementById('tab-trades');
  const paneLogs = document.getElementById('tab-logs');
  const paneSettings = document.getElementById('tab-settings');
  const navCurrentText = document.getElementById('nav-current-tab-text');
  const btnBackToDash = document.getElementById('btn-back-to-dashboard');
  const badgeTradesCount = document.getElementById('badge-trades-count');
  const badgeLogsCount = document.getElementById('badge-logs-count');

  // Trade Book Elements
  const selectTradeDate = document.getElementById('select-trade-date');
  const btnExportCsv = document.getElementById('btn-export-trades-csv');
  const btnRefreshTrades = document.getElementById('btn-refresh-trades');
  const tbodyTradesJournal = document.getElementById('tbody-trades-journal');
  const summaryTotalTrades = document.getElementById('summary-total-trades');
  const summaryWinTrades = document.getElementById('summary-win-trades');
  const summaryLossTrades = document.getElementById('summary-loss-trades');
  const summaryWinRate = document.getElementById('summary-win-rate');
  const summaryDayPnl = document.getElementById('summary-day-pnl');

  // Activity Logs Elements
  const selectLogDate = document.getElementById('select-log-date');
  const selectLogLevel = document.getElementById('select-log-level');
  const btnExportLogsCsv = document.getElementById('btn-export-logs-csv');
  const btnRefreshLogs = document.getElementById('btn-refresh-logs');
  const tbodyLogsJournal = document.getElementById('tbody-logs-journal');
  const summaryTotalLogs = document.getElementById('summary-total-logs');
  const summaryEntriesCount = document.getElementById('summary-entries-count');
  const summaryExitsCount = document.getElementById('summary-exits-count');
  const summaryBlocksCount = document.getElementById('summary-blocks-count');
  const summaryWarningsCount = document.getElementById('summary-warnings-count');

  function switchTab(target) {
    [navBtnDash, navBtnTrades, navBtnLogs, navBtnSettings].forEach(b => { if (b) b.classList.remove('active'); });
    [paneDash, paneTrades, paneLogs, paneSettings].forEach(p => { if (p) p.style.display = 'none'; });

    if (target === 'settings') {
      if (navBtnSettings) navBtnSettings.classList.add('active');
      if (paneSettings) paneSettings.style.display = 'block';
      if (navCurrentText) navCurrentText.textContent = 'BOT SETTINGS';
    } else if (target === 'trades') {
      if (navBtnTrades) navBtnTrades.classList.add('active');
      if (paneTrades) paneTrades.style.display = 'block';
      if (navCurrentText) navCurrentText.textContent = 'TRADE BOOK';
      loadTradesJournal();
    } else if (target === 'logs') {
      if (navBtnLogs) navBtnLogs.classList.add('active');
      if (paneLogs) paneLogs.style.display = 'block';
      if (navCurrentText) navCurrentText.textContent = 'ACTIVITY LOGS';
      loadAlertsJournal();
    } else {
      if (navBtnDash) navBtnDash.classList.add('active');
      if (paneDash) paneDash.style.display = 'block';
      if (navCurrentText) navCurrentText.textContent = 'LIVE DASHBOARD';
    }
  }

  if (navBtnDash) navBtnDash.addEventListener('click', () => switchTab('dashboard'));
  if (navBtnTrades) navBtnTrades.addEventListener('click', () => switchTab('trades'));
  if (navBtnLogs) navBtnLogs.addEventListener('click', () => switchTab('logs'));
  if (navBtnSettings) navBtnSettings.addEventListener('click', () => switchTab('settings'));
  if (btnBackToDash) btnBackToDash.addEventListener('click', () => switchTab('dashboard'));

  // 15. Trade Book & Journal Manager (Day-by-Day + Millisecond Timestamps + CSV Export)
  let currentSelectedDate = 'today';

  async function loadTradeDates() {
    if (!selectTradeDate) return;
    try {
      const res = await fetch('/api/strategy/trade-dates');
      if (!res.ok) return;
      const data = await res.json();
      const dates = data.dates || [];
      const today = data.today || new Date().toISOString().split('T')[0];

      const prevVal = selectTradeDate.value || 'today';
      let html = `<option value="today">Today (${today})</option>`;
      dates.forEach(d => {
        if (d !== today) {
          html += `<option value="${d}">${d}</option>`;
        }
      });
      html += `<option value="all">All Dates</option>`;
      selectTradeDate.innerHTML = html;
      selectTradeDate.value = prevVal;
    } catch (err) {
      console.warn('Error loading trade dates:', err);
    }
  }

  function formatTimeWithMs(timeStr, tsMs) {
    if (timeStr && timeStr.includes('.')) return timeStr;
    if (tsMs) {
      const d = new Date(Number(tsMs));
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      const ss = String(d.getSeconds()).padStart(2, '0');
      const ms = String(d.getMilliseconds()).padStart(3, '0');
      return `${hh}:${mm}:${ss}.${ms}`;
    }
    return timeStr || '-';
  }

  function formatExitBadge(reason) {
    if (!reason) return '<span class="badge-exit">-</span>';
    const r = String(reason).toUpperCase();
    if (r.includes('STOP_LOSS') || r.includes('SL')) {
      return '<span class="badge-exit badge-exit-sl">SL 25%</span>';
    }
    if (r.includes('TARGET') || r.includes('PROFIT')) {
      return '<span class="badge-exit badge-exit-target">TARGET 40%</span>';
    }
    if (r.includes('MANUAL')) {
      return '<span class="badge-exit badge-exit-manual">MANUAL</span>';
    }
    if (r.includes('15:15') || r.includes('EOD') || r.includes('SQUAREOFF')) {
      return '<span class="badge-exit badge-exit-eod">EOD 15:15</span>';
    }
    return `<span class="badge-exit">${reason}</span>`;
  }

  async function loadTradesJournal() {
    if (!tbodyTradesJournal) return;
    const selectedDate = selectTradeDate ? selectTradeDate.value : 'today';
    currentSelectedDate = selectedDate;

    try {
      const res = await fetch(`/api/strategy/trades?date=${encodeURIComponent(selectedDate)}&limit=1000`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const trades = data.trades || [];

      if (badgeTradesCount) {
        badgeTradesCount.textContent = trades.length;
      }

      // Calculate Day Summary Metrics
      let winCount = 0;
      let lossCount = 0;
      let totalPnl = 0.0;

      if (!trades.length) {
        tbodyTradesJournal.innerHTML = `<tr><td colspan="12" class="text-center text-muted font-mono" style="padding: 24px;">No trades recorded for ${selectedDate}.</td></tr>`;
      } else {
        let html = '';
        trades.forEach((t, i) => {
          const pnl = Number(t.net_pnl || 0.0);
          totalPnl += pnl;
          if (pnl > 0) winCount++;
          else if (pnl < 0) lossCount++;

          const pnlSign = pnl > 0 ? '+' : '';
          const pnlCls = pnl > 0 ? 'text-green font-bold' : (pnl < 0 ? 'text-red font-bold' : 'text-muted font-bold');

          const entryTime = formatTimeWithMs(t.entry_time_ist, t.entry_timestamp_ms);
          const exitTime = t.status === 'OPEN' ? '<span class="text-cyan font-bold font-mono">ACTIVE</span>' : formatTimeWithMs(t.exit_time_ist, t.exit_timestamp_ms);

          const exitPriceDisp = t.status === 'OPEN'
            ? `<span class="text-cyan font-mono font-bold">₹${Number(t.current_price || t.exit_price || t.entry_price || 0).toFixed(2)} (Live)</span>`
            : (t.exit_price !== null && t.exit_price !== undefined ? `<span class="font-mono text-bright font-bold">₹${Number(t.exit_price).toFixed(2)}</span>` : '<span class="text-dim">-</span>');

          const sideBadge = (t.side || '').toUpperCase().includes('CE')
            ? '<span class="act-badge act-enter-ce">BUY CE</span>'
            : '<span class="act-badge act-enter-pe">BUY PE</span>';

          const statusBadge = t.status === 'OPEN'
            ? '<span class="badge-status-open">OPEN</span>'
            : '<span class="badge-status-closed">CLOSED</span>';

          const ptsVal = Number(t.pnl_points || 0.0);
          const ptsSign = ptsVal > 0 ? '+' : '';
          const ptsCls = ptsVal > 0 ? 'text-green font-mono' : (ptsVal < 0 ? 'text-red font-mono' : 'text-muted font-mono');

          html += `<tr>
            <td class="text-center text-dim font-bold font-mono">${trades.length - i}</td>
            <td class="text-left font-bold text-bright font-mono">${t.symbol || t.underlying_key || '-'}</td>
            <td class="text-center">${sideBadge}</td>
            <td class="text-right font-mono text-bright font-bold">${Number(t.strike || 0).toLocaleString()}</td>
            <td class="text-right font-mono text-bright font-bold">₹${Number(t.entry_price || 0).toFixed(2)}</td>
            <td class="text-center font-mono text-bright font-bold">${entryTime}</td>
            <td class="text-right font-mono">${exitPriceDisp}</td>
            <td class="text-center font-mono font-bold">${exitTime}</td>
            <td class="text-right ${ptsCls} font-bold">${ptsSign}${ptsVal.toFixed(2)}</td>
            <td class="text-right font-mono ${pnlCls}">${pnlSign}₹${pnl.toFixed(2)}</td>
            <td class="text-center">${formatExitBadge(t.exit_reason)}</td>
            <td class="text-center">${statusBadge}</td>
          </tr>`;
        });

        tbodyTradesJournal.innerHTML = html;
      }

      // Update Summary KPI Cards
      if (summaryTotalTrades) summaryTotalTrades.textContent = trades.length;
      if (summaryWinTrades) summaryWinTrades.textContent = `${winCount} W`;
      if (summaryLossTrades) summaryLossTrades.textContent = `${lossCount} L`;

      const completedTrades = winCount + lossCount;
      const winRatePct = completedTrades > 0 ? ((winCount / completedTrades) * 100).toFixed(1) : '0.0';
      if (summaryWinRate) summaryWinRate.textContent = `${winRatePct}%`;

      if (summaryDayPnl) {
        const sign = totalPnl > 0 ? '+' : '';
        const pnlCls = totalPnl > 0 ? 'text-green' : (totalPnl < 0 ? 'text-red' : 'text-bright');
        summaryDayPnl.className = `summary-val font-mono ${pnlCls}`;
        summaryDayPnl.textContent = `${sign}₹${totalPnl.toFixed(2)}`;
      }

    } catch (err) {
      console.warn('Error loading trades journal:', err);
      if (tbodyTradesJournal) {
        tbodyTradesJournal.innerHTML = `<tr><td colspan="12" class="text-center text-red">Error loading trades: ${err.message}</td></tr>`;
      }
    }
  }

  // Date picker change & refresh events
  if (selectTradeDate) {
    selectTradeDate.addEventListener('change', () => {
      loadTradesJournal();
    });
  }

  if (btnRefreshTrades) {
    btnRefreshTrades.addEventListener('click', () => {
      loadTradesJournal();
    });
  }

  if (btnExportCsv) {
    btnExportCsv.addEventListener('click', () => {
      const selectedDate = selectTradeDate ? selectTradeDate.value : 'today';
      window.location.href = `/api/strategy/trades/export.csv?date=${encodeURIComponent(selectedDate)}`;
    });
  }

  // 16. Activity Logs Manager (Day-by-Day + Level Filter + CSV Export)
  async function loadAlertDates() {
    if (!selectLogDate) return;
    try {
      const res = await fetch('/api/strategy/alert-dates');
      if (!res.ok) return;
      const data = await res.json();
      const dates = data.dates || [];
      const today = data.today || new Date().toISOString().split('T')[0];

      const prevVal = selectLogDate.value || 'today';
      let html = `<option value="today">Today (${today})</option>`;
      dates.forEach(d => {
        if (d !== today) {
          html += `<option value="${d}">${d}</option>`;
        }
      });
      html += `<option value="all">All Dates</option>`;
      selectLogDate.innerHTML = html;
      selectLogDate.value = prevVal;
    } catch (err) {
      console.warn('Error loading alert dates:', err);
    }
  }

  async function loadAlertsJournal() {
    if (!tbodyLogsJournal) return;
    try {
      const selectedDate = selectLogDate ? selectLogDate.value : 'today';
      const selectedLevel = selectLogLevel ? selectLogLevel.value : 'ALL';
      let url = `/api/strategy/alerts?limit=500&date=${encodeURIComponent(selectedDate)}`;
      if (selectedLevel && selectedLevel !== 'ALL') {
        url += `&level=${encodeURIComponent(selectedLevel)}`;
      }

      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const alerts = data.alerts || [];

      if (badgeLogsCount) badgeLogsCount.textContent = alerts.length;

      if (!alerts.length) {
        tbodyLogsJournal.innerHTML = `<tr><td colspan="6" class="text-center text-muted">No activity logs recorded for ${selectedDate}.</td></tr>`;
        if (summaryTotalLogs) summaryTotalLogs.textContent = '0';
        if (summaryEntriesCount) summaryEntriesCount.textContent = '0 Entries';
        if (summaryExitsCount) summaryExitsCount.textContent = '0 Exits';
        if (summaryBlocksCount) summaryBlocksCount.textContent = '0';
        if (summaryWarningsCount) summaryWarningsCount.textContent = '0';
      } else {
        let html = '';
        let entriesCount = 0;
        let exitsCount = 0;
        let blocksCount = 0;
        let warningsCount = 0;

        alerts.forEach((a, i) => {
          const typeStr = (a.alert_type || a.type || 'EVENT').toUpperCase();
          const lvl = (a.level || 'INFO').toUpperCase();

          if (typeStr.includes('ENTRY')) entriesCount++;
          else if (typeStr.includes('EXIT')) exitsCount++;
          else if (typeStr.includes('BLOCK') || typeStr.includes('GUARD') || typeStr.includes('REJECT')) blocksCount++;
          else if (lvl.includes('WARN') || lvl.includes('ERR')) warningsCount++;

          let tagCls = 'tag-data';
          if (typeStr.includes('ENTRY')) tagCls = 'tag-entry';
          else if (typeStr.includes('EXIT')) tagCls = 'tag-exit';
          else if (typeStr.includes('BLOCK') || typeStr.includes('GUARD') || typeStr.includes('REJECT')) tagCls = 'tag-blocked';
          else if (lvl.includes('WARN')) tagCls = 'tag-warning';
          else if (lvl.includes('ERR')) tagCls = 'tag-exit';

          let lvlBadgeCls = 'badge-level-info';
          if (lvl.includes('WARN')) lvlBadgeCls = 'badge-level-warning';
          else if (lvl.includes('ERR')) lvlBadgeCls = 'badge-level-error';

          html += `<tr>
            <td class="text-center text-dim font-bold font-mono">${alerts.length - i}</td>
            <td class="text-center font-mono text-bright font-bold">${a.timestamp_ist || '-'}</td>
            <td class="text-center"><span class="badge-level ${lvlBadgeCls}">${lvl}</span></td>
            <td class="text-center"><span class="alert-tag ${tagCls}">${typeStr}</span></td>
            <td class="text-left font-bold text-bright font-mono">${a.symbol || '-'}</td>
            <td class="text-left text-dim font-mono">${a.message || ''}</td>
          </tr>`;
        });

        tbodyLogsJournal.innerHTML = html;

        if (summaryTotalLogs) summaryTotalLogs.textContent = alerts.length;
        if (summaryEntriesCount) summaryEntriesCount.textContent = `${entriesCount} Entries`;
        if (summaryExitsCount) summaryExitsCount.textContent = `${exitsCount} Exits`;
        if (summaryBlocksCount) summaryBlocksCount.textContent = blocksCount;
        if (summaryWarningsCount) summaryWarningsCount.textContent = warningsCount;
      }
    } catch (err) {
      console.warn('Error loading alerts journal:', err);
      if (tbodyLogsJournal) {
        tbodyLogsJournal.innerHTML = `<tr><td colspan="6" class="text-center text-red">Error loading logs: ${err.message}</td></tr>`;
      }
    }
  }

  if (selectLogDate) selectLogDate.addEventListener('change', () => loadAlertsJournal());
  if (selectLogLevel) selectLogLevel.addEventListener('change', () => loadAlertsJournal());
  if (btnRefreshLogs) btnRefreshLogs.addEventListener('click', () => loadAlertsJournal());
  if (btnExportLogsCsv) {
    btnExportLogsCsv.addEventListener('click', () => {
      const selectedDate = selectLogDate ? selectLogDate.value : 'today';
      const selectedLevel = selectLogLevel ? selectLogLevel.value : 'ALL';
      window.location.href = `/api/strategy/alerts/export.csv?date=${encodeURIComponent(selectedDate)}&level=${encodeURIComponent(selectedLevel)}`;
    });
  }

  // Index specific lot pills handler
  document.querySelectorAll('.btn-idx-lot').forEach(btn => {
    btn.addEventListener('click', async () => {
      const key = btn.dataset.key;
      const val = parseInt(btn.dataset.val, 10);
      if (!key || isNaN(val)) return;

      const parentGroup = btn.closest('.pill-group');
      if (parentGroup) {
        parentGroup.querySelectorAll('.btn-idx-lot').forEach(b => b.classList.remove('active'));
      }
      btn.classList.add('active');
      updateIndexLotBadge(key, val);

      try {
        const payload = { index_lots: {} };
        payload.index_lots[key] = val;
        await fetch('/api/strategy/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        showFeedback(`Set ${key.split('|')[1] || key} to ${val} lots`);
        fetchDashboardData();
      } catch (err) {
        showFeedback(`Failed to update index lot: ${err.message}`, true);
      }
    });
  });

  // Initial Boot
  fetchDashboardData();
  loadTradeDates();
  loadTradesJournal();
  loadAlertDates();
  loadAlertsJournal();
  setupSSE();
  // Continuous fallback polling every 2 seconds
  setInterval(() => {
    fetchDashboardData();
    if (paneTrades && paneTrades.style.display !== 'none') {
      loadTradesJournal();
    }
    if (paneLogs && paneLogs.style.display !== 'none') {
      loadAlertsJournal();
    }
  }, 2000);

})();
