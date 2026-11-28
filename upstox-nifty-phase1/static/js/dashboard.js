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
  const tbodyStrikes = document.getElementById('tbody-strikes') || document.getElementById('tbody-strike-detail');
  const tbodyMomentum = document.getElementById('tbody-momentum');
  const elStrikeTitle = document.getElementById('strike-table-title') || document.getElementById('strike-detail-title');
  const alertsContainer = document.getElementById('alerts-container');

  let activeMomentumFilter = 'ALL';
  let isMomentumPaused = false;
  let momentumHistory = [];
  const lastTrackedMetrics = {};

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
    return `<span class="act-badge act-no-trade">WATCHING</span>`;
  }

  // 1. Render Indices Table (6 ITM Strikes Only - No OTM)
  function renderIndices(indices) {
    if (!tbodyIndices) return;
    if (!indices || !indices.length) {
      tbodyIndices.innerHTML = `<tr><td colspan="11" class="text-center text-muted">Awaiting index data...</td></tr>`;
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

      const rawSig = idx.raw_signal || 'Neutral';
      const score = idx.score !== undefined ? Number(idx.score) : (Number(cePct) - Number(pePct));
      const scoreSign = score > 0 ? '+' : '';
      const scoreCls = score > 0 ? 'text-green' : (score < 0 ? 'text-red' : 'text-dim');
      let rawSigBadge = '<span class="text-dim">Neutral</span>';
      if (rawSig === 'CE Buy') {
        rawSigBadge = '<span class="bu-badge bu-lb font-bold">CE Buy</span>';
      } else if (rawSig === 'PE Buy') {
        rawSigBadge = '<span class="bu-badge bu-sb font-bold">PE Buy</span>';
      }
      rawSigBadge += `<br><span class="font-mono text-xs ${scoreCls}" title="Score = CE % - PE %">Score: ${scoreSign}${score.toFixed(2)}</span>`;

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
        
        <!-- CE BASKET (6 ITM) -->
        <td class="text-right">${ceBase}</td>
        <td class="text-right font-bold text-bright">${ceCurr}</td>
        <td class="text-right">${fmtPct(cePct)}</td>

        <!-- PE BASKET (6 ITM) -->
        <td class="text-right">${peBase}</td>
        <td class="text-right font-bold text-bright">${peCurr}</td>
        <td class="text-right">${fmtPct(pePct)}</td>

        <!-- SIGNAL EVALUATION -->
        <td class="text-center">${rawSigBadge}</td>

        <!-- CONFIRMATION & DECISION -->
        <td class="text-center">${holdBadge}</td>
        <td class="text-center">${decBadge}</td>
        <td class="text-center">${renderIndexAction(idx)}</td>
      </tr>`;
    });

    tbodyIndices.innerHTML = html;
  }

  // 4. Render Strike Detail (ATM ±5 Basket for Sum Signal Strategy)
  function renderStrikeDetail(strikes, indexName, atmStrike) {
    if (!tbodyStrikes) return;
    if (!strikes || !strikes.length) {
      tbodyStrikes.innerHTML = `<tr><td colspan="7" class="text-center text-muted">Awaiting strike basket for ${indexName}...</td></tr>`;
      return;
    }

    if (elStrikeTitle) {
      const isStock = !indexName.includes('NIFTY') && !indexName.includes('SENSEX');
      const basketLabel = isStock ? 'ATM &plusmn; 5 Strike Basket (11 CE + 11 PE = 22 Strikes)' : 'ATM &plusmn; 10 Strike Basket (21 CE + 21 PE = 42 Strikes)';
      elStrikeTitle.innerHTML = `${indexName} &middot; ${basketLabel}`;
    }

    let html = '';
    strikes.forEach(s => {
      const isAtm = Number(s.strike) === Number(atmStrike);
      const atmBadge = isAtm ? `<span class="atm-strike-badge">ATM</span>` : '';

      const ceBase = (s.ce_snap_ltp && Number(s.ce_snap_ltp) > 0)
        ? `₹${Number(s.ce_snap_ltp).toFixed(2)}`
        : '<span class="text-dim">-</span>';
      const ceLtp = (s.ce_ltp && Number(s.ce_ltp) > 0)
        ? `<span class="font-mono font-bold text-bright">₹${Number(s.ce_ltp).toFixed(2)}</span>`
        : '<span class="text-dim">-</span>';

      const peBase = (s.pe_snap_ltp && Number(s.pe_snap_ltp) > 0)
        ? `₹${Number(s.pe_snap_ltp).toFixed(2)}`
        : '<span class="text-dim">-</span>';
      const peLtp = (s.pe_ltp && Number(s.pe_ltp) > 0)
        ? `<span class="font-mono font-bold text-bright">₹${Number(s.pe_ltp).toFixed(2)}</span>`
        : '<span class="text-dim">-</span>';

      html += `<tr class="${isAtm ? 'row-atm' : ''}">
        <!-- CALLS (CE) -->
        <td class="text-right">${ceBase}</td>
        <td class="text-right">${ceLtp}</td>
        <td class="text-right">${fmtPct(s.ce_price_pct)}</td>

        <!-- STRIKE -->
        <td class="text-center font-bold text-bright">${Number(s.strike).toLocaleString()} ${atmBadge}</td>

        <!-- PUTS (PE) -->
        <td class="text-right">${fmtPct(s.pe_price_pct)}</td>
        <td class="text-right">${peLtp}</td>
        <td class="text-right">${peBase}</td>
      </tr>`;
    });

    tbodyStrikes.innerHTML = html;
  }

  // 4b. Render 09:14:55 Pre-Market Locked Baseline (Settings Card)
  let activeLockedIndexKey = 'NSE_INDEX|Nifty 50';
  const tbodyLockedBaseline = document.getElementById('tbody-locked-baseline');
  const elLockedStatusBadge = document.getElementById('locked-status-badge');

  function renderLockedBaseline(lockedData) {
    if (!tbodyLockedBaseline) return;
    if (!lockedData || typeof lockedData !== 'object') {
      tbodyLockedBaseline.innerHTML = `<tr><td colspan="9" class="text-center text-muted" style="padding: 20px;">Awaiting 09:14:55 baseline snapshot...</td></tr>`;
      return;
    }

    const idxInfo = lockedData[activeLockedIndexKey] || Object.values(lockedData)[0];
    if (!idxInfo) {
      tbodyLockedBaseline.innerHTML = `<tr><td colspan="9" class="text-center text-muted" style="padding: 20px;">No baseline data for selected index.</td></tr>`;
      return;
    }

    if (elLockedStatusBadge) {
      if (idxInfo.locked) {
        elLockedStatusBadge.textContent = 'LOCKED AT 09:14:55 IST';
        elLockedStatusBadge.className = 'badge-status-closed';
      } else {
        elLockedStatusBadge.textContent = 'AWAITING 09:14:55 SNAPSHOT';
        elLockedStatusBadge.className = 'badge-status-open';
      }
    }

    const contracts = idxInfo.contracts || [];
    if (!contracts.length) {
      tbodyLockedBaseline.innerHTML = `<tr><td colspan="9" class="text-center text-muted" style="padding: 20px;">Planning ATM &plusmn; 10 contracts (21 CE + 21 PE = 42 Options)...</td></tr>`;
      return;
    }

    const atmK = Number(idxInfo.atm_strike || 0);
    let html = '';
    contracts.forEach((c, i) => {
      const side = (c.side || '').toUpperCase();
      const sideBadge = side === 'CE'
        ? '<span class="act-badge act-enter-ce">CALL CE</span>'
        : '<span class="act-badge act-enter-pe">PUT PE</span>';

      const stk = Number(c.strike || 0);
      let moneyness = 'ATM';
      if (atmK > 0) {
        if (side === 'CE') moneyness = stk < atmK ? 'ITM' : (stk > atmK ? 'OTM' : 'ATM');
        else moneyness = stk > atmK ? 'ITM' : (stk < atmK ? 'OTM' : 'ATM');
      }

      const refVal = Number(c.ref_ltp || 0);
      const refSrc = c.ref_source || (refVal > 0 ? 'live_091455' : 'MISSING_091455');
      const isMissing = refVal <= 0 || refSrc === 'MISSING_091455';
      const statusBadge = isMissing
        ? '<span class="badge-status-open" style="background: rgba(239,68,68,0.2); color: #f87171;">MISSING 09:14:55</span>'
        : (idxInfo.locked
          ? '<span class="badge-status-closed">LOCKED</span>'
          : '<span class="badge-status-open">PENDING 09:14:55</span>');

      html += `<tr>
        <td class="text-center font-mono text-dim">${i + 1}</td>
        <td class="text-left font-bold text-bright font-mono">${c.symbol || c.instrument_key || '-'}</td>
        <td class="text-center">${sideBadge}</td>
        <td class="text-center"><span class="atm-strike-badge" style="background: rgba(14, 165, 233, 0.15); color: #38bdf8; border: 1px solid rgba(14, 165, 233, 0.3);">${moneyness}</span></td>
        <td class="text-right font-mono font-bold text-bright">${stk.toLocaleString()}</td>
        <td class="text-right font-mono font-bold ${isMissing ? 'text-red' : 'text-cyan'}">₹${refVal.toFixed(2)}</td>
        <td class="text-center font-mono text-dim text-xs">${refSrc}</td>
        <td class="text-center font-mono text-dim text-xs">${c.lock_time || '09:14:55'}</td>
        <td class="text-center">${statusBadge}</td>
      </tr>`;
    });

    tbodyLockedBaseline.innerHTML = html;
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

  // 5b. Render Momentum Feed
  function renderMomentumFeed() {
    if (!tbodyMomentum) return;

    let entries = momentumHistory;
    if (activeMomentumFilter !== 'ALL') {
      entries = entries.filter(e => (e.key === activeMomentumFilter) || (e.name && e.name.includes(activeMomentumFilter.replace('NSE_INDEX|', '').replace('BSE_INDEX|', ''))));
    }

    if (!entries.length) {
      tbodyMomentum.innerHTML = `<tr><td colspan="6" class="text-center text-muted" style="padding: 22px;">Awaiting live price percentage changes...</td></tr>`;
      return;
    }

    let html = '';
    entries.slice(0, 60).forEach(e => {
      // CE classes
      const ceCls = e.ce_pct > 0 ? 'text-green font-bold' : (e.ce_pct < 0 ? 'text-red font-bold' : 'text-muted');
      const dCe = Number(e.delta_ce || 0);
      const dCeSign = dCe > 0 ? '+' : '';
      const dCeCls = dCe > 0 ? 'text-green' : (dCe < 0 ? 'text-red' : 'text-dim');
      const dCeArrow = dCe > 0 ? '▲' : (dCe < 0 ? '▼' : '');

      // PE classes
      const peCls = e.pe_pct > 0 ? 'text-red font-bold' : (e.pe_pct < 0 ? 'text-green font-bold' : 'text-muted');
      const dPe = Number(e.delta_pe || 0);
      const dPeSign = dPe > 0 ? '+' : '';
      const dPeCls = dPe > 0 ? 'text-red' : (dPe < 0 ? 'text-green' : 'text-dim');
      const dPeArrow = dPe > 0 ? '▲' : (dPe < 0 ? '▼' : '');

      // Signal Badge
      let sigBadge = `<span class="bu-badge bu-neu">Neutral</span>`;
      if (e.confirmed) {
        sigBadge = e.raw_signal === 'CE Buy'
          ? `<span class="bu-badge bu-lb font-bold">🎯 CE BUY CONFIRMED</span>`
          : `<span class="bu-badge bu-sb font-bold">🎯 PE BUY CONFIRMED</span>`;
      } else if (e.raw_signal === 'CE Buy') {
        sigBadge = `<span class="bu-badge bu-lb">CE Buy (${e.hold_sec || 0}s/300s)</span>`;
      } else if (e.raw_signal === 'PE Buy') {
        sigBadge = `<span class="bu-badge bu-sb">PE Buy (${e.hold_sec || 0}s/300s)</span>`;
      }

      const idxCls = e.name.includes('BANK') ? 'text-purple' : (e.name.includes('SENSEX') ? 'text-amber' : 'text-cyan');

      html += `<tr>
        <td class="font-mono text-dim text-xs">${e.time || '--:--:--'}</td>
        <td><strong class="font-bold ${idxCls}">${e.name}</strong></td>
        <td class="text-right">
          <span class="text-dim font-mono text-xs">${Number(e.prev_ce_pct || 0) > 0 ? '+' : ''}${Number(e.prev_ce_pct || 0).toFixed(2)}%</span>
          <span class="text-dim">&rarr;</span>
          <span class="font-mono ${ceCls}">${Number(e.ce_pct || 0) > 0 ? '+' : ''}${Number(e.ce_pct || 0).toFixed(2)}%</span>
          <span class="font-mono text-xs ${dCeCls}" style="margin-left: 6px;">(${dCeSign}${dCe.toFixed(2)}% ${dCeArrow})</span>
        </td>
        <td class="text-right">
          <span class="text-dim font-mono text-xs">${Number(e.prev_pe_pct || 0) > 0 ? '+' : ''}${Number(e.prev_pe_pct || 0).toFixed(2)}%</span>
          <span class="text-dim">&rarr;</span>
          <span class="font-mono ${peCls}">${Number(e.pe_pct || 0) > 0 ? '+' : ''}${Number(e.pe_pct || 0).toFixed(2)}%</span>
          <span class="font-mono text-xs ${dPeCls}" style="margin-left: 6px;">(${dPeSign}${dPe.toFixed(2)}% ${dPeArrow})</span>
        </td>
        <td class="text-center font-mono">
          <span class="${ceCls}">CE ${Number(e.ce_pct || 0) > 0 ? '+' : ''}${Number(e.ce_pct || 0).toFixed(2)}%</span>
          <span class="text-dim">&middot;</span>
          <span class="${peCls}">PE ${Number(e.pe_pct || 0) > 0 ? '+' : ''}${Number(e.pe_pct || 0).toFixed(2)}%</span>
        </td>
        <td class="text-center">${sigBadge}</td>
      </tr>`;
    });

    tbodyMomentum.innerHTML = html;
  }

  function updateMomentumHistory(data) {
    if (!data) return;

    // 1. Ingest backend momentum feed history if local is empty
    if (momentumHistory.length === 0 && data.momentum_feed && Array.isArray(data.momentum_feed) && data.momentum_feed.length) {
      momentumHistory = data.momentum_feed.slice();
    }

    // 2. Client-side diff check per index
    if (data.indices && Array.isArray(data.indices)) {
      data.indices.forEach(idx => {
        const key = idx.key || idx.name;
        if (!key) return;
        const cePct = Number(Number(idx.ce_pct || 0).toFixed(2));
        const pePct = Number(Number(idx.pe_pct || 0).toFixed(2));
        const score = Number(Number(idx.score !== undefined ? idx.score : (cePct - pePct)).toFixed(2));
        const rawSig = idx.raw_signal || 'Neutral';
        const holdSec = Math.round(idx.hold_elapsed_sec || 0);

        const last = lastTrackedMetrics[key];
        if (last) {
          const deltaCe = Number((cePct - last.ce_pct).toFixed(2));
          const deltaPe = Number((pePct - last.pe_pct).toFixed(2));
          const deltaScore = Number((score - last.score).toFixed(2));

          if (deltaCe !== 0 || deltaPe !== 0 || rawSig !== last.rawSig) {
            const rawName = (idx.name || key).replace('NSE_INDEX|', '').replace('BSE_INDEX|', '').replace(' 50', '');
            const newEntry = {
              id: 'client_' + Date.now() + '_' + Math.random().toString(36).substr(2, 4),
              time: new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata', hour12: false }),
              key: key,
              name: rawName,
              ce_pct: cePct,
              prev_ce_pct: last.ce_pct,
              delta_ce: deltaCe,
              pe_pct: pePct,
              prev_pe_pct: last.pe_pct,
              delta_pe: deltaPe,
              score: score,
              prev_score: last.score,
              delta_score: deltaScore,
              raw_signal: rawSig,
              hold_sec: holdSec,
              confirmed: Boolean(idx.confirmed_signal),
            };
            momentumHistory.unshift(newEntry);
            if (momentumHistory.length > 100) momentumHistory.pop();
          }
        }
        lastTrackedMetrics[key] = { ce_pct: cePct, pe_pct: pePct, score: score, rawSig: rawSig };
      });
    }

    if (!isMomentumPaused) {
      renderMomentumFeed();
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
      renderStockRankings(data.stock_rankings || [], data.top_bullish || [], data.top_bearish || []);
      updateMomentumHistory(data);
    
      // Selected Index Strike Detail
      const currentIdx = (data.indices || []).find(x => x.key === activeIndexKey) || (data.indices || [])[0];
      if (currentIdx) {
        renderStrikeDetail(currentIdx.strikes || [], currentIdx.name || 'NIFTY', currentIdx.atm_strike);
      }

      // Alerts
      if (data.recent_alerts && data.recent_alerts.length) {
        data.recent_alerts.reverse().forEach(a => appendAlert(a));
      }

      // Render 09:14:55 Pre-Market Locked Baseline Basket (Settings Card)
      if (data.locked_091455) {
        renderLockedBaseline(data.locked_091455);
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

    if (elOiUpdate) {
      if (curTotalSec < openSec) {
        const waitSec = openSec - curTotalSec;
        const m = Math.floor(waitSec / 60);
        const s = waitSec % 60;
        elOiUpdate.textContent = `Market opens in ${m}m ${s < 10 ? '0' : ''}${s}s`;
      } else if (curTotalSec > closeSec) {
        elOiUpdate.textContent = `Market closed · EOD`;
      } else {
        elOiUpdate.textContent = `Market Open · Live Ticks`;
      }
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
    const dynLotSizes = (lastData && lastData.config && lastData.config.index_lot_sizes) || {};
    let lotSize = Number(dynLotSizes[key] || 0);
    if (!lotSize) {
      if (key.includes('Nifty 50')) lotSize = 65;
      else if (key.includes('Bank')) lotSize = 30;
      else if (key.includes('SENSEX')) lotSize = 20;
      else lotSize = 1;
    }

    const totalQty = val * lotSize;
    const lotWord = val === 1 ? '1 Lot' : `${val} Lots`;
    badge.textContent = `${lotWord} (${totalQty} Qty)`;
  }

  let activeStockRankFilter = 'ALL';
  function renderStockRankings(rankings, topBullish, topBearish) {
    const tbodyStocks = document.getElementById('tbody-stock-rankings');
    const lblStockCount = document.getElementById('lbl-stock-count');
    const elBullList = document.getElementById('top-bullish-list');
    const elBearList = document.getElementById('top-bearish-list');

    if (lblStockCount) lblStockCount.textContent = (rankings || []).length;

    if (elBullList) {
      if (!topBullish || !topBullish.length) {
        elBullList.innerHTML = '<span class="text-dim">Awaiting 300s confirmed bullish stocks...</span>';
      } else {
        elBullList.innerHTML = topBullish.map(s =>
          `<span class="bu-badge bu-lb" style="margin: 2px 4px 2px 0; display: inline-block;">#${s.rank} ${s.symbol} (Score +${Number(s.score || 0).toFixed(2)} | CE ${Number(s.ce_pct || 0).toFixed(2)}%)</span>`
        ).join('');
      }
    }

    if (elBearList) {
      if (!topBearish || !topBearish.length) {
        elBearList.innerHTML = '<span class="text-dim">Awaiting 300s confirmed bearish stocks...</span>';
      } else {
        elBearList.innerHTML = topBearish.map(s =>
          `<span class="bu-badge bu-sb" style="margin: 2px 4px 2px 0; display: inline-block;">#${s.rank} ${s.symbol} (Score ${Number(s.score || 0).toFixed(2)} | PE +${Number(s.pe_pct || 0).toFixed(2)}%)</span>`
        ).join('');
      }
    }

    if (!tbodyStocks) return;
    let rows = Array.isArray(rankings) ? rankings : [];
    if (activeStockRankFilter === 'BULLISH') {
      rows = rows.filter(r => r.raw_signal === 'CE Buy' || Number(r.score || 0) > 0);
    } else if (activeStockRankFilter === 'BEARISH') {
      rows = rows.filter(r => r.raw_signal === 'PE Buy' || Number(r.score || 0) < 0);
    } else if (activeStockRankFilter === 'CONFIRMED') {
      rows = rows.filter(r => Boolean(r.confirmed_signal) || Boolean(r.has_position));
    }

    if (!rows.length) {
      tbodyStocks.innerHTML = `<tr><td colspan="9" class="text-center text-muted" style="padding: 18px;">No stock rows match current filter (${activeStockRankFilter}).</td></tr>`;
      return;
    }

    let html = '';
    rows.forEach(r => {
      const score = Number(r.score || 0);
      const scoreCls = score > 0 ? 'text-green font-bold' : (score < 0 ? 'text-red font-bold' : 'text-dim');
      const scoreSign = score > 0 ? '+' : '';
      let sigHtml = '<span class="text-dim">Neutral</span>';
      if (r.confirmed_signal) {
        const cls = r.confirmed_signal === 'CE Buy' ? 'bu-lb' : 'bu-sb';
        sigHtml = `<span class="bu-badge ${cls} font-bold">${r.confirmed_signal} (CONFIRMED)</span>`;
      } else if (r.raw_signal && r.raw_signal !== 'Neutral') {
        const cls = r.raw_signal === 'CE Buy' ? 'bu-lb' : 'bu-sb';
        sigHtml = `<span class="bu-badge ${cls}">${r.raw_signal} (${Math.round(r.hold_elapsed_sec || 0)}s/${Math.round(r.whipsaw_sec || 300)}s)</span>`;
      }
      const statusHtml = r.has_position
        ? '<span class="badge-status-open">IN POSITION</span>'
        : `<span class="text-dim font-mono text-xs">${r.state || 'ARMED'}</span>`;

      html += `<tr>
        <td class="text-center font-mono font-bold text-dim">#${r.rank || '-'}</td>
        <td class="text-left">
          <strong class="font-bold text-bright">${r.symbol || r.key}</strong><br>
          <span class="text-dim font-mono text-xs">₹${Number(r.spot_price || 0).toFixed(2)} &middot; ATM ${Number(r.atm_strike || 0).toLocaleString()} (Lot ${r.lot_size || '-'})</span>
        </td>
        <td class="text-right font-mono text-xs">${Number(r.ce_baseline_sum || 0).toFixed(1)} / <strong class="text-bright">${Number(r.ce_current_sum || 0).toFixed(1)}</strong></td>
        <td class="text-right font-mono">${fmtPct(r.ce_pct)}</td>
        <td class="text-right font-mono text-xs">${Number(r.pe_baseline_sum || 0).toFixed(1)} / <strong class="text-bright">${Number(r.pe_current_sum || 0).toFixed(1)}</strong></td>
        <td class="text-right font-mono">${fmtPct(r.pe_pct)}</td>
        <td class="text-right font-mono ${scoreCls}">${scoreSign}${score.toFixed(2)}</td>
        <td class="text-center">${sigHtml}</td>
        <td class="text-center">${statusHtml}</td>
      </tr>`;
    });
    tbodyStocks.innerHTML = html;
  }

  function syncConfigPills(cfg) {
    if (!cfg) return;

    if (cfg.index_lot_sizes && typeof cfg.index_lot_sizes === 'object') {
      const lblNifty = document.getElementById('lbl-lotsize-nifty');
      const lblBn = document.getElementById('lbl-lotsize-banknifty');
      const lblSx = document.getElementById('lbl-lotsize-sensex');
      if (lblNifty && cfg.index_lot_sizes['NSE_INDEX|Nifty 50']) lblNifty.textContent = `(Lot Size: ${cfg.index_lot_sizes['NSE_INDEX|Nifty 50']})`;
      if (lblBn && cfg.index_lot_sizes['NSE_INDEX|Nifty Bank']) lblBn.textContent = `(Lot Size: ${cfg.index_lot_sizes['NSE_INDEX|Nifty Bank']})`;
      if (lblSx && cfg.index_lot_sizes['BSE_INDEX|SENSEX']) lblSx.textContent = `(Lot Size: ${cfg.index_lot_sizes['BSE_INDEX|SENSEX']})`;
    }

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

    // Sync ITM Depth, Whipsaw, Max Bullish/Bearish pills & Premium band
    document.querySelectorAll('.btn-cfg-pill').forEach(btn => {
      const k = btn.dataset.cfg;
      const v = Number(btn.dataset.val);
      if (cfg[k] !== undefined && Number(cfg[k]) === v) {
        btn.classList.add('active');
      } else if (cfg[k] !== undefined) {
        btn.classList.remove('active');
      }
    });
    const inpMin = document.getElementById('inp-min-premium');
    const inpMax = document.getElementById('inp-max-premium');
    if (inpMin && document.activeElement !== inpMin && cfg.min_entry_premium !== undefined) {
      inpMin.value = cfg.min_entry_premium;
    }
    if (inpMax && document.activeElement !== inpMax && cfg.max_entry_premium !== undefined) {
      inpMax.value = cfg.max_entry_premium;
    }
    const guardStrike = document.getElementById('guard-strike-filter');
    if (guardStrike && cfg.entry_itm_depth !== undefined) {
      guardStrike.textContent = `ITM ${cfg.entry_itm_depth} · ₹${Number(cfg.min_entry_premium || 50)} – ₹${Number(cfg.max_entry_premium || 2500)} Premium Band`;
    }
    const guardWhip = document.getElementById('guard-whipsaw');
    if (guardWhip && cfg.whipsaw_sec !== undefined) {
      guardWhip.textContent = `${Math.round(cfg.whipsaw_sec)}s (Independent Hold per Underlying)`;
    }
    if (cfg.max_bullish_stocks !== undefined) {
      const hBull = document.getElementById('heading-count-bullish');
      if (hBull) hBull.textContent = cfg.max_bullish_stocks;
    }
    if (cfg.max_bearish_stocks !== undefined) {
      const hBear = document.getElementById('heading-count-bearish');
      if (hBear) hBear.textContent = cfg.max_bearish_stocks;
    }
  }

  // 11. Event Listeners: Dynamic Action Buttons (ENTER CE / ENTER PE / EXIT)
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

  // Momentum Journal Elements
  const selectMomentumDate = document.getElementById('select-momentum-date');
  const btnExportMomentumCsv = document.getElementById('btn-export-momentum-csv');
  const btnRefreshMomentum = document.getElementById('btn-refresh-momentum');
  const tbodyMomentumJournal = document.getElementById('tbody-momentum-journal');
  const momentumJournalIndexTabs = document.getElementById('momentum-journal-index-tabs');
  const summaryTotalMomentum = document.getElementById('summary-total-momentum');
  const summaryMomentumIndex = document.getElementById('summary-momentum-index');
  const summaryBullishSignals = document.getElementById('summary-bullish-signals');
  const summaryBearishSignals = document.getElementById('summary-bearish-signals');
  const summaryConfirmedSignals = document.getElementById('summary-confirmed-signals');

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
      if (navCurrentText) navCurrentText.textContent = 'MOMENTUM JOURNAL';
      loadMomentumJournal();
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
    if (r.includes('TRAILING') || r.includes('TSL')) {
      return '<span class="badge-exit badge-exit-tsl">TRAILING SL</span>';
    }
    if (r.includes('STOP_LOSS') || r.includes('SL')) {
      return '<span class="badge-exit badge-exit-sl">SL 20%</span>';
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

  let activeTradeStrategy = 'ALL';

  async function loadTradesJournal() {
    if (!tbodyTradesJournal) return;
    const selectedDate = selectTradeDate ? selectTradeDate.value : 'today';
    currentSelectedDate = selectedDate;

    try {
      const res = await fetch(`/api/strategy/trades?date=${encodeURIComponent(selectedDate)}&limit=1000`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const allTrades = data.trades || [];

      if (badgeTradesCount) {
        badgeTradesCount.textContent = allTrades.length;
      }

      // Filter by strategy
      let trades = allTrades;
      if (activeTradeStrategy === 'PATH_1_0915') {
        trades = allTrades.filter(t => (t.strategy === 'PATH_1_0915' || (t.build_up && t.build_up.includes('PATH_1')) || (t.trade_id && t.trade_id.includes('P1_'))));
      } else if (activeTradeStrategy === 'SUM_SIGNAL') {
        trades = allTrades.filter(t => !(t.strategy === 'PATH_1_0915' || (t.build_up && t.build_up.includes('PATH_1')) || (t.trade_id && t.trade_id.includes('P1_'))));
      }

      // Calculate Day Summary Metrics for current view
      let winCount = 0;
      let lossCount = 0;
      let totalPnl = 0.0;

      if (!trades.length) {
        const stratName = activeTradeStrategy === 'ALL' ? '' : (activeTradeStrategy === 'PATH_1_0915' ? 'Path 1 (09:15 Tick) ' : 'Path 2 (Sum Signal) ');
        tbodyTradesJournal.innerHTML = `<tr><td colspan="13" class="text-center text-muted font-mono" style="padding: 24px;">No ${stratName}trades recorded for ${selectedDate}.</td></tr>`;
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

          const isPath1 = (t.strategy === 'PATH_1_0915' || t.strategy === 'PATH_1' || (t.build_up && t.build_up.includes('PATH_1')) || (t.trade_id && (t.trade_id.includes('P1_') || t.trade_id.startsWith('GAP-'))));
          const stratBadge = isPath1
            ? '<span class="bu-badge bu-lb font-bold" style="font-size: 10px; padding: 2px 6px;">09:15 TICK</span>'
            : '<span class="bu-badge bu-sb font-bold" style="font-size: 10px; padding: 2px 6px;">SUM % MOMENTUM</span>';

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
            <td class="text-center">${stratBadge}</td>
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
        tbodyTradesJournal.innerHTML = `<tr><td colspan="13" class="text-center text-red">Error loading trades: ${err.message}</td></tr>`;
      }
    }
  }

  // Strategy tabs in Trade Book
  const tradesStrategyTabs = document.getElementById('trades-strategy-tabs');
  if (tradesStrategyTabs) {
    tradesStrategyTabs.addEventListener('click', e => {
      const btn = e.target.closest('.tab-btn');
      if (!btn) return;
      tradesStrategyTabs.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeTradeStrategy = btn.dataset.strategy || 'ALL';
      loadTradesJournal();
    });
  }

  // Locked baseline tabs in Settings
  const lockedBaselineTabs = document.getElementById('locked-baseline-tabs');
  if (lockedBaselineTabs) {
    lockedBaselineTabs.addEventListener('click', e => {
      const btn = e.target.closest('.tab-btn');
      if (!btn) return;
      lockedBaselineTabs.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeLockedIndexKey = btn.dataset.index || 'NSE_INDEX|Nifty 50';
      if (lastData && lastData.locked_091455) {
        renderLockedBaseline(lastData.locked_091455);
      }
    });
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

  // 16. Momentum Journal Manager (Day-by-Day + Index Filter + Second-by-Second CSV Export)
  let activeMomentumJournalIndex = 'ALL';

  async function loadMomentumDates() {
    if (!selectMomentumDate) return;
    try {
      const res = await fetch('/api/strategy/momentum-dates');
      if (!res.ok) return;
      const data = await res.json();
      const dates = data.dates || [];
      const today = data.today || new Date().toISOString().split('T')[0];

      const prevVal = selectMomentumDate.value || 'today';
      let html = `<option value="today">Today (${today})</option>`;
      dates.forEach(d => {
        if (d !== today) {
          html += `<option value="${d}">${d}</option>`;
        }
      });
      html += `<option value="all">All Dates</option>`;
      selectMomentumDate.innerHTML = html;
      selectMomentumDate.value = prevVal;
    } catch (err) {
      console.warn('Error loading momentum dates:', err);
    }
  }

  async function loadMomentumJournal() {
    if (!tbodyMomentumJournal) return;
    try {
      const selectedDate = selectMomentumDate ? selectMomentumDate.value : 'today';
      let url = `/api/strategy/momentum-logs?limit=100000&date=${encodeURIComponent(selectedDate)}`;
      if (activeMomentumJournalIndex && activeMomentumJournalIndex !== 'ALL') {
        url += `&index=${encodeURIComponent(activeMomentumJournalIndex)}`;
      }

      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const logs = data.logs || [];

      if (badgeLogsCount) badgeLogsCount.textContent = logs.length;

      // Update KPI filter card text
      if (summaryMomentumIndex) {
        if (activeMomentumJournalIndex === 'ALL') summaryMomentumIndex.textContent = 'ALL INDICES';
        else if (activeMomentumJournalIndex.includes('Nifty Bank')) summaryMomentumIndex.textContent = 'BANK NIFTY';
        else if (activeMomentumJournalIndex.includes('Nifty 50')) summaryMomentumIndex.textContent = 'NIFTY 50';
        else if (activeMomentumJournalIndex.includes('SENSEX')) summaryMomentumIndex.textContent = 'SENSEX';
        else summaryMomentumIndex.textContent = activeMomentumJournalIndex;
      }

      if (!logs.length) {
        tbodyMomentumJournal.innerHTML = `<tr><td colspan="7" class="text-center text-muted" style="padding: 24px;">No momentum progression records found for ${selectedDate}.</td></tr>`;
        if (summaryTotalMomentum) summaryTotalMomentum.textContent = '0';
        if (summaryBullishSignals) summaryBullishSignals.textContent = '0 CE';
        if (summaryBearishSignals) summaryBearishSignals.textContent = '0 PE';
        if (summaryConfirmedSignals) summaryConfirmedSignals.textContent = '0';
      } else {
        let html = '';
        let bullishCount = 0;
        let bearishCount = 0;
        let confirmedCount = 0;

        logs.forEach((item, i) => {
          const sig = item.raw_signal || 'Neutral';
          const isConfirmed = Boolean(item.confirmed);
          if (isConfirmed) confirmedCount++;
          if (sig.includes('Bullish') || sig.includes('CE')) bullishCount++;
          else if (sig.includes('Bearish') || sig.includes('PE')) bearishCount++;

          const nameUpper = (item.index_name || '').toUpperCase();
          let idxBadge = `<span class="bu-badge" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; font-weight: 700; padding: 3px 8px; border-radius: 4px; font-size: 11px;">${item.index_name || 'NIFTY'}</span>`;
          if (nameUpper.includes('BANK')) {
            idxBadge = `<span class="bu-badge" style="background: rgba(168, 85, 247, 0.15); color: #c084fc; font-weight: 700; padding: 3px 8px; border-radius: 4px; font-size: 11px;">BANK NIFTY</span>`;
          } else if (nameUpper.includes('SENSEX')) {
            idxBadge = `<span class="bu-badge" style="background: rgba(234, 179, 8, 0.15); color: #facc15; font-weight: 700; padding: 3px 8px; border-radius: 4px; font-size: 11px;">SENSEX</span>`;
          }

          // CE Movement
          const prevCe = Number(item.prev_ce_pct || 0.0);
          const cePct = Number(item.ce_pct || 0.0);
          const deltaCe = Number(item.delta_ce !== undefined ? item.delta_ce : (cePct - prevCe));
          const ceArrow = deltaCe >= 0 ? '▲' : '▼';
          const ceDeltaSign = deltaCe >= 0 ? '+' : '';
          const ceDeltaCls = deltaCe >= 0 ? 'text-green' : 'text-red';

          // PE Movement
          const prevPe = Number(item.prev_pe_pct || 0.0);
          const pePct = Number(item.pe_pct || 0.0);
          const deltaPe = Number(item.delta_pe !== undefined ? item.delta_pe : (pePct - prevPe));
          const peArrow = deltaPe >= 0 ? '▲' : '▼';
          const peDeltaSign = deltaPe >= 0 ? '+' : '';
          const peDeltaCls = deltaPe >= 0 ? 'text-green' : 'text-red';

          // Momentum comparison column
          const cePctSign = cePct >= 0 ? '+' : '';
          const pePctSign = pePct >= 0 ? '+' : '';
          const ceColCls = cePct >= 0 ? 'text-green' : 'text-red';
          const peColCls = pePct >= 0 ? 'text-green' : 'text-red';

          // Signal & Hold badge
          let sigBadge = `<span class="bu-badge" style="padding: 3px 8px; font-size: 11px; background: rgba(148, 163, 184, 0.1); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.2);">Neutral</span>`;
          if (isConfirmed) {
            sigBadge = `<span class="bu-badge bu-lb font-bold" style="padding: 4px 10px; font-size: 11px; background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid #22c55e;">CONFIRMED ENTRY</span>`;
          } else if (sig.includes('Bullish')) {
            sigBadge = `<span class="bu-badge" style="padding: 4px 10px; font-size: 11px; background: rgba(34, 197, 94, 0.15); color: #4ade80;">BULLISH CE (${item.hold_sec || 0}s)</span>`;
          } else if (sig.includes('Bearish')) {
            sigBadge = `<span class="bu-badge" style="padding: 4px 10px; font-size: 11px; background: rgba(239, 68, 68, 0.15); color: #f87171;">BEARISH PE (${item.hold_sec || 0}s)</span>`;
          }

          html += `<tr>
            <td class="text-center text-dim font-bold font-mono">${logs.length - i}</td>
            <td class="text-center font-mono text-dim font-bold">${item.timestamp_ist || '-'}</td>
            <td class="text-left">${idxBadge}</td>
            <td class="text-right font-mono">
              <span class="text-dim">${prevCe >= 0 ? '+' : ''}${prevCe.toFixed(2)}%</span> &rarr;
              <strong class="text-bright">${cePct >= 0 ? '+' : ''}${cePct.toFixed(2)}%</strong>
              <span class="${ceDeltaCls} font-bold" style="margin-left: 6px;">(${ceDeltaSign}${deltaCe.toFixed(2)}% ${ceArrow})</span>
            </td>
            <td class="text-right font-mono">
              <span class="text-dim">${prevPe >= 0 ? '+' : ''}${prevPe.toFixed(2)}%</span> &rarr;
              <strong class="text-bright">${pePct >= 0 ? '+' : ''}${pePct.toFixed(2)}%</strong>
              <span class="${peDeltaCls} font-bold" style="margin-left: 6px;">(${peDeltaSign}${deltaPe.toFixed(2)}% ${peArrow})</span>
            </td>
            <td class="text-center font-mono font-bold">
              <span class="${ceColCls}">CE ${cePctSign}${cePct.toFixed(2)}%</span>
              <span class="text-dim" style="margin: 0 4px;">&middot;</span>
              <span class="${peColCls}">PE ${pePctSign}${pePct.toFixed(2)}%</span>
            </td>
            <td class="text-center">${sigBadge}</td>
          </tr>`;
        });

        tbodyMomentumJournal.innerHTML = html;

        if (summaryTotalMomentum) summaryTotalMomentum.textContent = logs.length;
        if (summaryBullishSignals) summaryBullishSignals.textContent = `${bullishCount} CE`;
        if (summaryBearishSignals) summaryBearishSignals.textContent = `${bearishCount} PE`;
        if (summaryConfirmedSignals) summaryConfirmedSignals.textContent = confirmedCount;
      }
    } catch (err) {
      console.warn('Error loading momentum journal:', err);
      if (tbodyMomentumJournal) {
        tbodyMomentumJournal.innerHTML = `<tr><td colspan="7" class="text-center text-red">Error loading momentum data: ${err.message}</td></tr>`;
      }
    }
  }

  // Event Listeners for Momentum Journal
  if (momentumJournalIndexTabs) {
    momentumJournalIndexTabs.addEventListener('click', e => {
      const btn = e.target.closest('.tab-btn');
      if (!btn) return;
      momentumJournalIndexTabs.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeMomentumJournalIndex = btn.dataset.filter || 'ALL';
      loadMomentumJournal();
    });
  }

  if (selectMomentumDate) {
    selectMomentumDate.addEventListener('change', () => loadMomentumJournal());
  }

  if (btnRefreshMomentum) {
    btnRefreshMomentum.addEventListener('click', () => loadMomentumJournal());
  }

  if (btnExportMomentumCsv) {
    btnExportMomentumCsv.addEventListener('click', () => {
      const selectedDate = selectMomentumDate ? selectMomentumDate.value : 'today';
      window.location.href = `/api/strategy/momentum/export.csv?date=${encodeURIComponent(selectedDate)}&index=${encodeURIComponent(activeMomentumJournalIndex)}`;
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

  // Momentum Controls
  const momTabs = document.getElementById('momentum-index-tabs');
  if (momTabs) {
    momTabs.addEventListener('click', e => {
      const btn = e.target.closest('.tab-btn');
      if (!btn) return;
      momTabs.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeMomentumFilter = btn.dataset.filter || 'ALL';
      renderMomentumFeed();
    });
  }

  const btnPauseMom = document.getElementById('btn-pause-momentum');
  if (btnPauseMom) {
    btnPauseMom.addEventListener('click', () => {
      isMomentumPaused = !isMomentumPaused;
      btnPauseMom.textContent = isMomentumPaused ? 'Resume' : 'Pause';
      btnPauseMom.classList.toggle('btn-primary', isMomentumPaused);
      if (!isMomentumPaused) renderMomentumFeed();
    });
  }

  const btnClearMom = document.getElementById('btn-clear-momentum');
  if (btnClearMom) {
    btnClearMom.addEventListener('click', () => {
      momentumHistory = [];
      renderMomentumFeed();
    });
  }

  // Stock Rank Filter Tabs
  const stockRankTabs = document.getElementById('stock-rank-filter-tabs');
  if (stockRankTabs) {
    stockRankTabs.addEventListener('click', e => {
      const btn = e.target.closest('.tab-btn');
      if (!btn) return;
      stockRankTabs.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeStockRankFilter = btn.dataset.filter || 'ALL';
      if (lastData) renderStockRankings(lastData.stock_rankings || [], lastData.top_bullish || [], lastData.top_bearish || []);
    });
  }

  // 09:16 Baseline Proof CSV buttons
  ['btn-export-baseline-proof', 'btn-download-proof-top', 'btn-download-baseline-proof'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('click', () => {
        window.location.href = '/api/strategy/baseline-proof.csv';
      });
    }
  });

  // Configurable Strategy Pills (ITM Depth, Whipsaw, Max Bullish/Bearish)
  document.querySelectorAll('.btn-cfg-pill').forEach(btn => {
    btn.addEventListener('click', async () => {
      const cfgKey = btn.dataset.cfg;
      const val = Number(btn.dataset.val);
      if (!cfgKey || isNaN(val)) return;
      const grp = btn.closest('.pill-group');
      if (grp) {
        grp.querySelectorAll('.btn-cfg-pill').forEach(b => {
          if (b.dataset.cfg === cfgKey) b.classList.remove('active');
        });
      }
      btn.classList.add('active');
      try {
        await fetch('/api/strategy/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ [cfgKey]: val })
        });
        showFeedback(`Updated ${cfgKey} = ${val}`);
        fetchDashboardData();
      } catch (err) {
        showFeedback(`Update error: ${err.message}`, true);
      }
    });
  });

  const btnSavePremBand = document.getElementById('btn-save-premium-band');
  if (btnSavePremBand) {
    btnSavePremBand.addEventListener('click', async () => {
      const minP = Number(document.getElementById('inp-min-premium')?.value || 50);
      const maxP = Number(document.getElementById('inp-max-premium')?.value || 2500);
      try {
        await fetch('/api/strategy/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ min_entry_premium: minP, max_entry_premium: maxP })
        });
        showFeedback(`Premium band set to ₹${minP} – ₹${maxP}`);
        fetchDashboardData();
      } catch (err) {
        showFeedback(`Save error: ${err.message}`, true);
      }
    });
  }

  // Initial Boot
  fetchDashboardData();
  loadTradeDates();
  loadTradesJournal();
  loadMomentumDates();
  loadMomentumJournal();
  setupSSE();
  // Continuous fallback polling every 2 seconds
  setInterval(() => {
    fetchDashboardData();
    if (paneTrades && paneTrades.style.display !== 'none') {
      loadTradesJournal();
    }
    if (paneLogs && paneLogs.style.display !== 'none') {
      loadMomentumJournal();
    }
  }, 2000);

})();
