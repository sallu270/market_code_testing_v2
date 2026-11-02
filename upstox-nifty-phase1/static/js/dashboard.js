"use strict";

const state = {
    selectedInstrument: "",
    chart: null,
    tvChart: null,
    candleSeries: null,
    areaSeries: null,
    volumeSeries: null,
    timeframe: "1m",
    activeCandle: null,
    lastTickTimeSec: 0,
    candles: new Map(),
    ticks: [],
    eventSource: null,
    activeView: "optionChain",
    optionExpiries: [],
    selectedExpiry: "",
    selectedOptionIndex: "NSE_INDEX|Nifty 50",
    strikeRange: "15",
    chainAutoRefresh: true,
    chainData: null,
    chainTimer: null,
};

const elements = {};

document.addEventListener("DOMContentLoaded", async () => {
    mapElements();
    initialiseChart();
    bindEvents();
    await loadInitialData();
    connectEventStream();
    window.setInterval(refreshHealth, 10000);

    // Initialise Option Chain with 1-second live polling + real-time WebSocket tick integration
    await loadOptionExpiries();
    state.chainTimer = window.setInterval(() => {
        if (!document.hidden && state.activeView === "optionChain") {
            loadOptionChain();
        }
    }, 1000);

    // Initialise Strategy tab polling
    loadStrategyStatus();
    window.setInterval(() => {
        if (!document.hidden && state.activeView === "strategy") {
            loadStrategyStatus();
            loadStrategyTrades();
            loadStrategySignals();
        }
    }, 2000);
});

function mapElements() {
    const ids = [
        "statusDot",
        "connectionState",
        "connectionMessage",
        "selectedInstrument",
        "feedMode",
        "lastPrice",
        "lastPriceTime",
        "bidAsk",
        "spread",
        "storedCounts",
        "storedCandles",
        "instrumentSelect",
        "chartEmpty",
        "candleState",
        "openValue",
        "highValue",
        "lowValue",
        "closeValue",
        "volumeValue",
        "tickCount",
        "browserStream",
        "lastEvent",
        "feedLatency",
        "upstoxStatus",
        "ticksBody",
        "priceChart",
        // Option chain elements
        "tabOptionChain",
        "tabStrategy",
        "tabLiveFeed",
        "viewOptionChain",
        "viewStrategy",
        "viewLiveFeed",
        "chainSpotPrice",
        "chainSpotTime",
        "chainAtmStrike",
        "chainAtmType",
        "chainPcr",
        "chainPcrSentiment",
        "chainTotalOi",
        "chainOiSplit",
        "optionIndexSelect",
        "optionExpirySelect",
        "strikeRangeSelect",
        "chainAutoRefresh",
        "btnRefreshChain",
        "chainStatusMsg",
        "optionChainBody",
        // Strategy elements
        "stratCurrentPosition",
        "stratPositionDetail",
        "stratTotalTrades",
        "stratWinLossRatio",
        "stratNetPnl",
        "stratPnlPoints",
        "stratStatusBadge",
        "stratQtyThreshold",
        "stratValThreshold",
        "stratCondDayHigh",
        "stratCondPrevClose",
        "stratCondUptick",
        "btnSaveStratConfig",
        "stratConfigStatus",
        "stratTradesBody",
        "stratSignalsBody",
    ];

    for (const id of ids) {
        elements[id] = document.getElementById(id);
    }
}

function bindEvents() {
    // Tab switching
    if (elements.tabOptionChain) {
        elements.tabOptionChain.addEventListener("click", () => switchView("optionChain"));
    }
    if (elements.tabStrategy) {
        elements.tabStrategy.addEventListener("click", () => switchView("strategy"));
    }
    if (elements.tabLiveFeed) {
        elements.tabLiveFeed.addEventListener("click", () => switchView("liveFeed"));
    }

    // Strategy configuration save button
    if (elements.btnSaveStratConfig) {
        elements.btnSaveStratConfig.addEventListener("click", saveStrategyConfig);
    }

    // Option chain controls
    if (elements.optionIndexSelect) {
        elements.optionIndexSelect.addEventListener("change", async (e) => {
            state.selectedOptionIndex = e.target.value;
            state.selectedExpiry = "";
            state.chainData = null;
            await loadOptionExpiries();
        });
    }

    if (elements.optionExpirySelect) {
        elements.optionExpirySelect.addEventListener("change", (e) => {
            state.selectedExpiry = e.target.value;
            loadOptionChain(true);
        });
    }

    if (elements.strikeRangeSelect) {
        elements.strikeRangeSelect.addEventListener("change", (e) => {
            state.strikeRange = e.target.value;
            if (state.chainData) {
                renderOptionChain(state.chainData);
            }
        });
    }

    if (elements.chainAutoRefresh) {
        elements.chainAutoRefresh.addEventListener("change", (e) => {
            state.chainAutoRefresh = e.target.checked;
        });
    }

    if (elements.btnRefreshChain) {
        elements.btnRefreshChain.addEventListener("click", () => {
            loadOptionChain(true);
        });
    }

    // Instrument watchlist selection
    if (elements.instrumentSelect) {
        elements.instrumentSelect.addEventListener("change", async (event) => {
            state.selectedInstrument = event.target.value;
            elements.selectedInstrument.textContent =
                state.selectedInstrument || "Waiting for data";
            await loadCandles();
            refreshVisibleTick();
        });
    }

    // TradingView Timeframe buttons
    document.querySelectorAll(".tf-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            const tf = btn.getAttribute("data-timeframe");
            if (tf && tf !== state.timeframe) {
                switchTimeframe(tf);
            }
        });
    });
}

function switchView(viewName) {
    state.activeView = viewName;
    const tabs = [
        { name: "optionChain", btn: elements.tabOptionChain, view: elements.viewOptionChain },
        { name: "strategy", btn: elements.tabStrategy, view: elements.viewStrategy },
        { name: "liveFeed", btn: elements.tabLiveFeed, view: elements.viewLiveFeed },
    ];

    for (const t of tabs) {
        if (!t.btn || !t.view) continue;
        const isActive = t.name === viewName;
        t.btn.classList.toggle("tab-button--active", isActive);
        t.btn.setAttribute("aria-selected", isActive ? "true" : "false");
        t.view.hidden = !isActive;
    }

    if (viewName === "optionChain" && !state.chainData) {
        loadOptionChain(true);
    } else if (viewName === "strategy") {
        loadStrategyStatus();
        loadStrategyTrades();
        loadStrategySignals();
    } else if (viewName === "liveFeed") {
        if (state.tvChart) {
            const container = document.getElementById("tvChartContainer");
            if (container && container.clientWidth > 0) {
                state.tvChart.applyOptions({
                    width: container.clientWidth,
                    height: container.clientHeight || 440,
                });
                state.tvChart.timeScale().fitContent();
            }
        }
    }
}

function initialiseChart() {
    const container = document.getElementById("tvChartContainer");
    if (!container || !window.LightweightCharts) {
        if (elements.chartEmpty) {
            elements.chartEmpty.hidden = false;
        }
        return;
    }

    state.tvChart = LightweightCharts.createChart(container, {
        width: container.clientWidth || 800,
        height: container.clientHeight || 440,
        layout: {
            background: { type: "solid", color: "#081321" },
            textColor: "#8fa3ba",
            fontSize: 12,
            fontFamily: "'Inter', system-ui, sans-serif",
        },
        grid: {
            vertLines: { color: "rgba(148, 163, 184, 0.06)" },
            horzLines: { color: "rgba(148, 163, 184, 0.06)" },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: {
                color: "rgba(83, 224, 193, 0.35)",
                width: 1,
                style: LightweightCharts.LineStyle.Dashed,
            },
            horzLine: {
                color: "rgba(83, 224, 193, 0.35)",
                width: 1,
                style: LightweightCharts.LineStyle.Dashed,
            },
        },
        rightPriceScale: {
            borderColor: "rgba(148, 163, 184, 0.15)",
            scaleMargins: {
                top: 0.08,
                bottom: 0.16,
            },
        },
        timeScale: {
            borderColor: "rgba(148, 163, 184, 0.15)",
            timeVisible: true,
            secondsVisible: state.timeframe === "tick",
        },
    });

    state.volumeSeries = state.tvChart.addHistogramSeries({
        color: "rgba(83, 224, 193, 0.25)",
        priceFormat: {
            type: "volume",
        },
        priceScaleId: "",
    });
    state.volumeSeries.priceScale().applyOptions({
        scaleMargins: {
            top: 0.82,
            bottom: 0,
        },
    });

    state.candleSeries = state.tvChart.addCandlestickSeries({
        upColor: "#089981",
        downColor: "#f23645",
        borderUpColor: "#089981",
        borderDownColor: "#f23645",
        wickUpColor: "#089981",
        wickDownColor: "#f23645",
    });

    state.areaSeries = state.tvChart.addAreaSeries({
        topColor: "rgba(8, 153, 129, 0.4)",
        bottomColor: "rgba(8, 153, 129, 0.02)",
        lineColor: "#089981",
        lineWidth: 2,
        priceLineVisible: true,
        lastValueVisible: true,
    });
    state.areaSeries.applyOptions({ visible: false });

    // Handle container resize
    if (window.ResizeObserver) {
        const resizeObserver = new ResizeObserver((entries) => {
            if (!entries || !entries.length || !state.tvChart) return;
            const entry = entries[0];
            const { width, height } = entry.contentRect;
            if (width > 0 && height > 0) {
                state.tvChart.applyOptions({ width, height });
            }
        });
        resizeObserver.observe(container);
    }
}

async function switchTimeframe(tf) {
    state.timeframe = tf;

    document.querySelectorAll(".tf-btn").forEach((btn) => {
        btn.classList.toggle("tf-btn--active", btn.getAttribute("data-timeframe") === tf);
    });

    if (state.tvChart) {
        state.tvChart.applyOptions({
            timeScale: {
                secondsVisible: tf === "tick",
            },
        });
    }

    if (tf === "tick") {
        if (state.candleSeries) state.candleSeries.applyOptions({ visible: false });
        if (state.volumeSeries) state.volumeSeries.applyOptions({ visible: false });
        if (state.areaSeries) state.areaSeries.applyOptions({ visible: true });
    } else {
        if (state.areaSeries) state.areaSeries.applyOptions({ visible: false });
        if (state.candleSeries) state.candleSeries.applyOptions({ visible: true });
        if (state.volumeSeries) state.volumeSeries.applyOptions({ visible: true });
    }

    await loadCandles();
}

async function loadInitialData() {
    try {
        const [instrumentsResult, ticksResult] = await Promise.all([
            fetchJson("/api/instruments"),
            fetchJson("/api/ticks?limit=40"),
        ]);

        populateInstrumentSelect(instrumentsResult.instruments || []);
        state.ticks = ticksResult.ticks || [];
        renderTicks();
        refreshVisibleTick();
        await loadCandles();
        await refreshHealth();
    } catch (error) {
        setBrowserStream("Initial load failed", true);
        setStatus("error", error.message);
    }
}

async function loadCandles() {
    if (!state.selectedInstrument) {
        if (elements.chartEmpty) elements.chartEmpty.hidden = false;
        renderCurrentCandle();
        return;
    }

    if (state.timeframe === "tick") {
        try {
            const query = new URLSearchParams({
                instrument_key: state.selectedInstrument,
                limit: "300",
            });
            const res = await fetchJson(`/api/ticks?${query.toString()}`);
            const ticks = res.ticks || [];

            // Sort chronological ascending
            const sortedTicks = [...ticks].sort((a, b) => {
                const ta = Number(a.exchange_timestamp_ms || a.received_timestamp_ms || 0);
                const tb = Number(b.exchange_timestamp_ms || b.received_timestamp_ms || 0);
                return ta - tb;
            });

            const cleanPoints = [];
            let lastSec = 0;

            for (const t of sortedTicks) {
                const tsMs = Number(t.exchange_timestamp_ms || t.received_timestamp_ms);
                const ltp = toNumber(t.ltp);
                if (ltp === null) continue;

                let sec = Math.floor(tsMs / 1000);
                if (sec <= lastSec) {
                    sec = lastSec + 1;
                }
                cleanPoints.push({ time: sec, value: ltp });
                lastSec = sec;
            }

            state.lastTickTimeSec = lastSec;

            if (state.areaSeries) {
                state.areaSeries.setData(cleanPoints);
            }
            if (elements.chartEmpty) elements.chartEmpty.hidden = cleanPoints.length > 0;
            if (state.tvChart && cleanPoints.length > 0) {
                state.tvChart.timeScale().fitContent();
            }
        } catch (err) {
            console.error("Failed to load tick data for chart", err);
        }
        return;
    }

    // 1m or 5m Candlestick mode
    const query = new URLSearchParams({
        instrument_key: state.selectedInstrument,
        timeframe: state.timeframe,
        limit: "150",
    });

    try {
        const result = await fetchJson(`/api/candles?${query.toString()}`);
        state.candles.clear();

        const candleList = [];
        const volumeList = [];
        let lastTimeSec = 0;

        for (const candle of result.candles || []) {
            state.candles.set(Number(candle.candle_start_ms), candle);
            const timeSec = Math.floor(Number(candle.candle_start_ms) / 1000);
            if (timeSec <= lastTimeSec) continue;
            lastTimeSec = timeSec;

            const open = toNumber(candle.open);
            const high = toNumber(candle.high);
            const low = toNumber(candle.low);
            const close = toNumber(candle.close);
            const vol = toNumber(candle.volume) || 0;

            candleList.push({ time: timeSec, open, high, low, close });
            volumeList.push({
                time: timeSec,
                value: vol,
                color: close >= open ? "rgba(8, 153, 129, 0.4)" : "rgba(242, 54, 69, 0.4)",
            });
        }

        if (state.candleSeries) {
            state.candleSeries.setData(candleList);
        }
        if (state.volumeSeries) {
            state.volumeSeries.setData(volumeList);
        }

        if (elements.chartEmpty) elements.chartEmpty.hidden = candleList.length > 0;
        if (state.tvChart && candleList.length > 0) {
            state.tvChart.timeScale().fitContent();
        }

        renderCurrentCandle();
    } catch (error) {
        setStatus("error", `Candle load failed: ${error.message}`);
    }
}

function connectEventStream() {
    if (state.eventSource) {
        state.eventSource.close();
    }

    setBrowserStream("Connecting", false);
    state.eventSource = new EventSource("/api/stream");

    state.eventSource.onopen = () => {
        setBrowserStream("Connected", false);
    };

    state.eventSource.onerror = () => {
        setBrowserStream("Reconnecting", true);
    };

    state.eventSource.onmessage = (event) => {
        elements.lastEvent.textContent = formatTime(Date.now());

        try {
            const message = JSON.parse(event.data);
            handleEvent(message);
        } catch (error) {
            console.error("Invalid server event", error);
        }
    };
}

function handleEvent(event) {
    const payload = event.payload || {};

    if (event.type === "status") {
        setStatus(payload.state || "waiting", payload.message || "");
        return;
    }

    if (event.type === "tick") {
        handleTick(payload);
        return;
    }

    if (event.type === "candle") {
        handleCandle(payload);
    }
}

function handleTick(tick) {
    const instrumentKey = String(tick.instrument_key || "");

    if (!state.selectedInstrument && instrumentKey) {
        addInstrument(instrumentKey);
        state.selectedInstrument = instrumentKey;
        elements.instrumentSelect.value = instrumentKey;
    } else {
        addInstrument(instrumentKey);
    }

    state.ticks.unshift(tick);
    state.ticks = state.ticks.slice(0, 40);

    renderTicks();

    // LIVE MILLISECOND OPTION CHAIN UPDATES
    updateChainSpotFromTick(tick);
    updateChainRowFromTick(tick);

    if (instrumentKey === state.selectedInstrument) {
        renderLatestTick(tick);

        const ltp = toNumber(tick.ltp);
        if (ltp !== null && state.tvChart) {
            if (state.timeframe === "tick" && state.areaSeries) {
                const nowSec = Math.floor(Date.now() / 1000);
                const tSec = Math.max(nowSec, (state.lastTickTimeSec || 0) + 1);
                state.lastTickTimeSec = tSec;
                state.areaSeries.update({ time: tSec, value: ltp });
                if (elements.chartEmpty) elements.chartEmpty.hidden = true;
            } else if (state.candleSeries && (state.timeframe === "1m" || state.timeframe === "5m")) {
                const bucketDurationSec = state.timeframe === "5m" ? 300 : 60;
                const tsMs = Number(tick.exchange_timestamp_ms || Date.now());
                const bucketSec = Math.floor(tsMs / (bucketDurationSec * 1000)) * bucketDurationSec;

                let candle = state.activeCandle;
                if (!candle || candle.time !== bucketSec) {
                    const existing = state.candles.get(bucketSec * 1000);
                    if (existing) {
                        candle = {
                            time: bucketSec,
                            open: toNumber(existing.open),
                            high: Math.max(toNumber(existing.high), ltp),
                            low: Math.min(toNumber(existing.low), ltp),
                            close: ltp,
                        };
                    } else {
                        candle = {
                            time: bucketSec,
                            open: ltp,
                            high: ltp,
                            low: ltp,
                            close: ltp,
                        };
                    }
                } else {
                    candle.high = Math.max(candle.high, ltp);
                    candle.low = Math.min(candle.low, ltp);
                    candle.close = ltp;
                }
                state.activeCandle = candle;
                state.candleSeries.update(candle);
                if (elements.chartEmpty) elements.chartEmpty.hidden = true;
            }
        }
    }
}

function handleCandle(candle) {
    if (String(candle.instrument_key) !== state.selectedInstrument) {
        return;
    }

    state.candles.set(Number(candle.candle_start_ms), candle);

    const orderedKeys = [...state.candles.keys()].sort((a, b) => a - b);
    while (orderedKeys.length > 200) {
        const oldest = orderedKeys.shift();
        state.candles.delete(oldest);
    }

    if (state.candleSeries && state.timeframe === "1m") {
        const timeSec = Math.floor(Number(candle.candle_start_ms) / 1000);
        const open = toNumber(candle.open);
        const high = toNumber(candle.high);
        const low = toNumber(candle.low);
        const close = toNumber(candle.close);
        const vol = toNumber(candle.volume) || 0;

        const cData = { time: timeSec, open, high, low, close };
        state.candleSeries.update(cData);
        if (state.volumeSeries) {
            state.volumeSeries.update({
                time: timeSec,
                value: vol,
                color: close >= open ? "rgba(8, 153, 129, 0.4)" : "rgba(242, 54, 69, 0.4)",
            });
        }
        if (elements.chartEmpty) elements.chartEmpty.hidden = true;
    }

    renderCurrentCandle();
}

function populateInstrumentSelect(instruments) {
    const existing = new Set(
        [...elements.instrumentSelect.options].map((option) => option.value)
    );

    for (const instrument of instruments) {
        if (!existing.has(instrument)) {
            const option = document.createElement("option");
            option.value = instrument;
            option.textContent = instrument;
            elements.instrumentSelect.appendChild(option);
            existing.add(instrument);
        }
    }

    if (!state.selectedInstrument && elements.instrumentSelect.options.length) {
        state.selectedInstrument = elements.instrumentSelect.options[0].value;
        elements.instrumentSelect.value = state.selectedInstrument;
    }

    elements.selectedInstrument.textContent =
        state.selectedInstrument || "Waiting for data";
}

function addInstrument(instrument) {
    if (!instrument) {
        return;
    }

    const exists = [...elements.instrumentSelect.options]
        .some((option) => option.value === instrument);

    if (!exists) {
        const option = document.createElement("option");
        option.value = instrument;
        option.textContent = instrument;
        elements.instrumentSelect.appendChild(option);
    }
}

function renderLatestTick(tick) {
    elements.selectedInstrument.textContent = tick.instrument_key || "—";
    elements.lastPrice.textContent = formatNumber(tick.ltp);
    elements.lastPriceTime.textContent =
        `Exchange time: ${formatTime(tick.exchange_timestamp_ms)}`;

    elements.bidAsk.textContent =
        `${formatNumber(tick.bid_price)} / ${formatNumber(tick.ask_price)}`;

    const bid = toNumber(tick.bid_price);
    const ask = toNumber(tick.ask_price);
    const spread = bid !== null && ask !== null ? ask - bid : null;
    elements.spread.textContent = `Spread: ${formatNumber(spread)}`;

    // Live Feed Latency Calculation (Exchange generation -> Localhost Browser)
    const exTime = Number(tick.exchange_timestamp_ms);
    if (exTime > 0 && elements.feedLatency) {
        const lagMs = Math.max(0, Date.now() - exTime);
        const lagSec = (lagMs / 1000).toFixed(2);
        elements.feedLatency.textContent = `${lagMs} ms (${lagSec}s)`;
        elements.feedLatency.style.color = lagMs < 750 ? "var(--primary)" : (lagMs < 1400 ? "var(--warning)" : "var(--danger)");
    }
}

function refreshVisibleTick() {
    const matchingTick = state.ticks.find(
        (tick) => String(tick.instrument_key) === state.selectedInstrument
    );

    if (matchingTick) {
        renderLatestTick(matchingTick);
    } else {
        elements.selectedInstrument.textContent =
            state.selectedInstrument || "Waiting for data";
        elements.lastPrice.textContent = "—";
        elements.lastPriceTime.textContent = "No tick received";
        elements.bidAsk.textContent = "— / —";
        elements.spread.textContent = "Spread: —";
    }
}

function renderCurrentCandle() {
    const candles = orderedCandles();
    const candle = candles[candles.length - 1];

    if (!candle) {
        elements.candleState.textContent = "Waiting";
        elements.candleState.classList.remove("badge--closed");

        for (const id of [
            "openValue",
            "highValue",
            "lowValue",
            "closeValue",
            "volumeValue",
            "tickCount",
        ]) {
            elements[id].textContent = "—";
        }
        return;
    }

    elements.candleState.textContent =
        Number(candle.is_closed) === 1 || candle.is_closed === true
            ? "Closed"
            : "Building";

    elements.candleState.classList.toggle(
        "badge--closed",
        Number(candle.is_closed) === 1 || candle.is_closed === true
    );

    elements.openValue.textContent = formatNumber(candle.open);
    elements.highValue.textContent = formatNumber(candle.high);
    elements.lowValue.textContent = formatNumber(candle.low);
    elements.closeValue.textContent = formatNumber(candle.close);
    elements.volumeValue.textContent = formatNumber(candle.volume, 0);
    elements.tickCount.textContent = String(candle.tick_count ?? "—");
}

function renderChart() {
    // Handled by TradingView Lightweight Charts
}

function orderedCandles() {
    return [...state.candles.values()].sort(
        (a, b) => Number(a.candle_start_ms) - Number(b.candle_start_ms)
    );
}

function renderTicks() {
    if (!state.ticks.length) {
        elements.ticksBody.innerHTML = `
            <tr>
                <td colspan="7" class="table-empty">
                    No market ticks received yet.
                </td>
            </tr>
        `;
        return;
    }

    elements.ticksBody.innerHTML = state.ticks.map((tick) => `
        <tr>
            <td>${escapeHtml(formatTime(tick.exchange_timestamp_ms))}</td>
            <td title="${escapeHtml(String(tick.instrument_key || ""))}">
                ${escapeHtml(shortInstrument(tick.instrument_key))}
            </td>
            <td>${escapeHtml(formatNumber(tick.ltp))}</td>
            <td>${escapeHtml(formatNumber(tick.bid_price))}</td>
            <td>${escapeHtml(formatNumber(tick.ask_price))}</td>
            <td>${escapeHtml(formatNumber(tick.volume, 0))}</td>
            <td>${escapeHtml(formatNumber(tick.open_interest, 0))}</td>
        </tr>
    `).join("");
}

async function refreshHealth() {
    try {
        const result = await fetchJson("/api/health");
        const counts = result.counts || {};

        elements.storedCounts.textContent =
            `${formatInteger(counts.ticks || 0)} ticks`;
        elements.storedCandles.textContent =
            `${formatInteger(counts.candles || 0)} one-minute candles`;
        elements.feedMode.textContent = `Feed mode: ${result.feed_mode || "—"}`;

        if (result.status) {
            setStatus(result.status.state, result.status.message);
        }
    } catch (error) {
        setBrowserStream("Health request failed", true);
    }
}

function setStatus(stateName, message) {
    const normalised = String(stateName || "waiting").toLowerCase();
    const isConnected = normalised === "connected";
    const isError = [
        "error",
        "message_error",
        "configuration_required",
        "disconnected",
    ].includes(normalised);

    elements.connectionState.textContent = humanise(normalised);
    elements.connectionMessage.textContent = message || "No status message";
    elements.upstoxStatus.textContent = humanise(normalised);

    elements.statusDot.className = "connection__dot";

    if (isConnected) {
        elements.statusDot.classList.add("connection__dot--connected");
    } else if (isError) {
        elements.statusDot.classList.add("connection__dot--error");
    } else {
        elements.statusDot.classList.add("connection__dot--waiting");
    }
}

function setBrowserStream(message, isError) {
    elements.browserStream.textContent = message;
    elements.browserStream.style.color = isError ? "#ff9aac" : "";
}

async function fetchJson(url) {
    const response = await fetch(url, {
        headers: {
            "Accept": "application/json",
        },
    });

    if (!response.ok) {
        throw new Error(`${response.status} ${response.statusText}`);
    }

    return response.json();
}

function formatTime(timestamp) {
    const number = Number(timestamp);
    if (!Number.isFinite(number)) {
        return "—";
    }

    return new Intl.DateTimeFormat("en-IN", {
        timeZone: "Asia/Kolkata",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
    }).format(new Date(number));
}

function formatNumber(value, maximumFractionDigits = 2) {
    const number = toNumber(value);
    if (number === null) {
        return "—";
    }

    return new Intl.NumberFormat("en-IN", {
        minimumFractionDigits: 0,
        maximumFractionDigits,
    }).format(number);
}

function formatInteger(value) {
    return new Intl.NumberFormat("en-IN", {
        maximumFractionDigits: 0,
    }).format(Number(value) || 0);
}

function toNumber(value) {
    if (value === null || value === undefined || value === "") {
        return null;
    }

    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function shortInstrument(value) {
    const text = String(value || "");
    return text.length > 30 ? `${text.slice(0, 27)}…` : text;
}

function humanise(value) {
    return String(value || "")
        .replaceAll("_", " ")
        .replace(/\b\w/g, (character) => character.toUpperCase());
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}

/* ==========================================================================
   OPTION CHAIN DATA FETCHER & RENDERER
   ========================================================================== */

async function loadOptionExpiries() {
    try {
        const idx = state.selectedOptionIndex || "NSE_INDEX|Nifty 50";
        const response = await fetchJson(`/api/option-chain/expiries?instrument_key=${encodeURIComponent(idx)}`);
        const expiries = response.expiries || [];
        state.optionExpiries = expiries;

        if (elements.optionExpirySelect) {
            if (!expiries.length) {
                elements.optionExpirySelect.innerHTML = `<option value="">No active expiries found</option>`;
                return;
            }

            elements.optionExpirySelect.innerHTML = expiries.map((exp, index) => {
                const label = formatExpiryLabel(exp, index === 0);
                return `<option value="${escapeHtml(exp)}" ${index === 0 ? "selected" : ""}>${escapeHtml(label)}</option>`;
            }).join("");

            state.selectedExpiry = expiries[0];
            await loadOptionChain(true);
        }
    } catch (err) {
        if (elements.chainStatusMsg) {
            elements.chainStatusMsg.textContent = "Failed to load expiries";
        }
    }
}

function formatExpiryLabel(dateStr, isNearest) {
    try {
        const [year, month, day] = dateStr.split("-");
        const date = new Date(year, month - 1, day);
        const formatted = new Intl.DateTimeFormat("en-IN", {
            day: "2-digit",
            month: "short",
            year: "numeric"
        }).format(date);
        return isNearest ? `${formatted} (Current Weekly)` : formatted;
    } catch {
        return dateStr;
    }
}

async function loadOptionChain(force = false) {
    if (!state.selectedExpiry) return;

    if (elements.chainStatusMsg) {
        elements.chainStatusMsg.textContent = "Updating...";
    }

    try {
        const idx = state.selectedOptionIndex || "NSE_INDEX|Nifty 50";
        const url = `/api/option-chain?instrument_key=${encodeURIComponent(idx)}&expiry_date=${encodeURIComponent(state.selectedExpiry)}`;
        const result = await fetchJson(url);

        if (result.error) {
            if (elements.chainStatusMsg) {
                elements.chainStatusMsg.textContent = "Error: " + result.error;
            }
            return;
        }

        state.chainData = result;
        renderOptionChain(result);

        if (elements.chainStatusMsg) {
            elements.chainStatusMsg.textContent = `Updated at ${formatTime(Date.now())}`;
        }
    } catch (err) {
        if (elements.chainStatusMsg) {
            elements.chainStatusMsg.textContent = "Failed to fetch option chain";
        }
    }
}

function renderOptionChain(data) {
    if (!data || !Array.isArray(data.strikes) || !data.strikes.length) {
        elements.optionChainBody.innerHTML = `
            <tr>
                <td colspan="15" class="table-empty">
                    No strikes available for this expiry.
                </td>
            </tr>
        `;
        return;
    }

    const spotPrice = toNumber(data.spot_price);
    const strikes = data.strikes;

    // 1. Find ATM strike (closest to spotPrice)
    let atmStrike = null;
    let minDiff = Infinity;
    if (spotPrice !== null) {
        for (const item of strikes) {
            const strike = toNumber(item.strike_price);
            if (strike !== null) {
                const diff = Math.abs(strike - spotPrice);
                if (diff < minDiff) {
                    minDiff = diff;
                    atmStrike = strike;
                }
            }
        }
    }

    // 2. Filter strikes based on state.strikeRange
    let visibleStrikes = [...strikes].sort((a, b) => toNumber(a.strike_price) - toNumber(b.strike_price));
    if (state.strikeRange !== "all" && atmStrike !== null) {
        const count = parseInt(state.strikeRange, 10) || 15;
        const atmIndex = visibleStrikes.findIndex(s => toNumber(s.strike_price) === atmStrike);
        if (atmIndex !== -1) {
            const start = Math.max(0, atmIndex - count);
            const end = Math.min(visibleStrikes.length, atmIndex + count + 1);
            visibleStrikes = visibleStrikes.slice(start, end);
        }
    }

    // 3. Compute totals and max OI for bars
    let totalCallOi = 0;
    let totalPutOi = 0;
    let maxCallOi = 1;
    let maxPutOi = 1;

    for (const item of strikes) {
        const callData = item.call_options?.market_data || {};
        const putData = item.put_options?.market_data || {};
        const callOi = toNumber(callData.oi) || 0;
        const putOi = toNumber(putData.oi) || 0;
        totalCallOi += callOi;
        totalPutOi += putOi;
    }

    for (const item of visibleStrikes) {
        const callData = item.call_options?.market_data || {};
        const putData = item.put_options?.market_data || {};
        const callOi = toNumber(callData.oi) || 0;
        const putOi = toNumber(putData.oi) || 0;
        if (callOi > maxCallOi) maxCallOi = callOi;
        if (putOi > maxPutOi) maxPutOi = putOi;
    }

    // 4. Update Header Metrics
    if (elements.chainSpotPrice) {
        elements.chainSpotPrice.textContent = spotPrice !== null ? formatNumber(spotPrice, 2) : "—";
    }
    if (elements.chainSpotTime) {
        elements.chainSpotTime.textContent = "Live at " + formatTime(Date.now());
    }
    if (elements.chainAtmStrike) {
        elements.chainAtmStrike.textContent = atmStrike !== null ? formatNumber(atmStrike, 0) : "—";
    }

    const pcr = totalCallOi > 0 ? (totalPutOi / totalCallOi) : null;
    if (elements.chainPcr) {
        elements.chainPcr.textContent = pcr !== null ? pcr.toFixed(2) : "—";
    }
    if (elements.chainPcrSentiment && pcr !== null) {
        if (pcr > 1.2) {
            elements.chainPcrSentiment.textContent = "Bullish Sentiment (>1.2)";
            elements.chainPcrSentiment.className = "text--up";
        } else if (pcr < 0.8) {
            elements.chainPcrSentiment.textContent = "Bearish Sentiment (<0.8)";
            elements.chainPcrSentiment.className = "text--down";
        } else {
            elements.chainPcrSentiment.textContent = "Neutral Range (0.8 - 1.2)";
            elements.chainPcrSentiment.className = "text--muted";
        }
    }

    if (elements.chainTotalOi) {
        elements.chainTotalOi.textContent = formatInteger(totalCallOi + totalPutOi);
    }
    if (elements.chainOiSplit) {
        elements.chainOiSplit.textContent = `CE: ${formatInteger(totalCallOi)} | PE: ${formatInteger(totalPutOi)}`;
    }

    // 5. Build Table Rows
    const rowsHtml = visibleStrikes.map(item => {
        const strike = toNumber(item.strike_price);
        const isAtm = strike === atmStrike;
        const isCallItm = spotPrice !== null && strike < spotPrice;
        const isPutItm = spotPrice !== null && strike > spotPrice;

        const call = item.call_options || {};
        const callMkt = call.market_data || {};
        const callGreeks = call.option_greeks || {};

        const put = item.put_options || {};
        const putMkt = put.market_data || {};
        const putGreeks = put.option_greeks || {};

        // Call metrics
        const callOi = toNumber(callMkt.oi);
        const callPrevOi = toNumber(callMkt.prev_oi);
        const callOiChg = (callOi !== null && callPrevOi !== null) ? (callOi - callPrevOi) : null;
        const callVol = toNumber(callMkt.volume);
        const callIv = toNumber(callGreeks.iv);
        const callLtp = toNumber(callMkt.ltp);
        const callClose = toNumber(callMkt.close_price);
        const callChg = (callLtp !== null && callClose !== null && callClose > 0) ? (callLtp - callClose) : null;
        const callChgPct = (callChg !== null && callClose > 0) ? ((callChg / callClose) * 100) : null;
        const callBid = toNumber(callMkt.bid_price);
        const callAsk = toNumber(callMkt.ask_price);

        // Put metrics
        const putOi = toNumber(putMkt.oi);
        const putPrevOi = toNumber(putMkt.prev_oi);
        const putOiChg = (putOi !== null && putPrevOi !== null) ? (putOi - putPrevOi) : null;
        const putVol = toNumber(putMkt.volume);
        const putIv = toNumber(putGreeks.iv);
        const putLtp = toNumber(putMkt.ltp);
        const putClose = toNumber(putMkt.close_price);
        const putChg = (putLtp !== null && putClose !== null && putClose > 0) ? (putLtp - putClose) : null;
        const putChgPct = (putChg !== null && putClose > 0) ? ((putChg / putClose) * 100) : null;
        const putBid = toNumber(putMkt.bid_price);
        const putAsk = toNumber(putMkt.ask_price);

        const callOiBarWidth = callOi ? Math.min(100, Math.round((callOi / maxCallOi) * 100)) : 0;
        const putOiBarWidth = putOi ? Math.min(100, Math.round((putOi / maxPutOi) * 100)) : 0;

        const rowClass = isAtm ? "row--atm" : "";
        const callCellClass = isCallItm ? "cell--itm-call" : "";
        const putCellClass = isPutItm ? "cell--itm-put" : "";

        return `
            <tr class="${rowClass}" data-call-key="${escapeHtml(call.instrument_key || '')}" data-put-key="${escapeHtml(put.instrument_key || '')}">
                <!-- CALLS (CE) -->
                <td class="${callCellClass} oi-cell ce-oi">
                    <div class="oi-bar oi-bar--call" style="width: ${callOiBarWidth}%"></div>
                    <span class="val-text">${callOi !== null ? formatInteger(callOi) : "—"}</span>
                </td>
                <td class="${callCellClass} ${renderChgClass(callOiChg)}">
                    ${renderChgVal(callOiChg, true)}
                </td>
                <td class="${callCellClass}">${callVol !== null ? formatInteger(callVol) : "—"}</td>
                <td class="${callCellClass}">${callIv !== null && callIv > 0 ? callIv.toFixed(1) + "%" : "—"}</td>
                <td class="${callCellClass}"><strong class="ce-ltp">${callLtp !== null ? formatNumber(callLtp, 2) : "—"}</strong></td>
                <td class="${callCellClass} ${renderChgClass(callChg)}">
                    ${renderChgVal(callChg)} ${callChgPct !== null ? `(${callChgPct >= 0 ? '+' : ''}${callChgPct.toFixed(1)}%)` : ''}
                </td>
                <td class="${callCellClass} ce-bidask">
                    ${(callBid !== null && callAsk !== null) ? `${formatNumber(callBid, 1)} / ${formatNumber(callAsk, 1)}` : "— / —"}
                </td>

                <!-- STRIKE -->
                <td class="td--strike">
                    <span>${formatNumber(strike, 0)}</span>
                    ${isAtm ? '<span class="badge--atm-strike">ATM</span>' : ''}
                </td>

                <!-- PUTS (PE) -->
                <td class="${putCellClass} pe-bidask">
                    ${(putBid !== null && putAsk !== null) ? `${formatNumber(putBid, 1)} / ${formatNumber(putAsk, 1)}` : "— / —"}
                </td>
                <td class="${putCellClass} ${renderChgClass(putChg)}">
                    ${renderChgVal(putChg)} ${putChgPct !== null ? `(${putChgPct >= 0 ? '+' : ''}${putChgPct.toFixed(1)}%)` : ''}
                </td>
                <td class="${putCellClass}"><strong class="pe-ltp">${putLtp !== null ? formatNumber(putLtp, 2) : "—"}</strong></td>
                <td class="${putCellClass}">${putIv !== null && putIv > 0 ? putIv.toFixed(1) + "%" : "—"}</td>
                <td class="${putCellClass}">${putVol !== null ? formatInteger(putVol) : "—"}</td>
                <td class="${putCellClass} ${renderChgClass(putOiChg)}">
                    ${renderChgVal(putOiChg, true)}
                </td>
                <td class="${putCellClass} oi-cell pe-oi">
                    <div class="oi-bar oi-bar--put" style="width: ${putOiBarWidth}%"></div>
                    <span class="val-text">${putOi !== null ? formatInteger(putOi) : "—"}</span>
                </td>
            </tr>
        `;
    }).join("");

    elements.optionChainBody.innerHTML = rowsHtml;
}

function renderChgClass(val) {
    if (val === null || val === undefined) return "text--muted";
    if (val > 0) return "text--up";
    if (val < 0) return "text--down";
    return "text--muted";
}

function renderChgVal(val, isInteger = false) {
    if (val === null || val === undefined) return "—";
    const prefix = val > 0 ? "+" : "";
    return prefix + (isInteger ? formatInteger(val) : formatNumber(val, 2));
}

/* ==========================================================================
   REAL-TIME MILLISECOND TICK DISPATCHERS
   ========================================================================== */

function updateChainSpotFromTick(tick) {
    if (!elements.chainSpotPrice) return;
    const newPrice = toNumber(tick.ltp);
    if (newPrice === null) return;

    const oldText = elements.chainSpotPrice.textContent.replace(/,/g, "");
    const oldPrice = parseFloat(oldText);

    elements.chainSpotPrice.textContent = formatNumber(newPrice, 2);
    elements.chainSpotTime.textContent = `Live at ${formatTime(tick.exchange_timestamp_ms || Date.now())}`;

    if (!isNaN(oldPrice) && oldPrice !== newPrice) {
        flashElement(elements.chainSpotPrice, newPrice > oldPrice ? "flash-up" : "flash-down");
    }
}

function updateChainRowFromTick(tick) {
    const key = tick.instrument_key;
    if (!key) return;

    const callRow = document.querySelector(`[data-call-key="${key}"]`);
    if (callRow) {
        updateSingleOptionCell(callRow, tick, true);
    }

    const putRow = document.querySelector(`[data-put-key="${key}"]`);
    if (putRow) {
        updateSingleOptionCell(putRow, tick, false);
    }
}

function updateSingleOptionCell(rowEl, tick, isCall) {
    const ltpEl = rowEl.querySelector(isCall ? ".ce-ltp" : ".pe-ltp");
    const bidAskEl = rowEl.querySelector(isCall ? ".ce-bidask" : ".pe-bidask");
    const oiEl = rowEl.querySelector(isCall ? ".ce-oi .val-text" : ".pe-oi .val-text");

    if (ltpEl && tick.ltp !== undefined && tick.ltp !== null) {
        const oldLtp = parseFloat(ltpEl.textContent.replace(/,/g, ""));
        const newLtp = parseFloat(tick.ltp);
        ltpEl.textContent = formatNumber(newLtp, 2);
        if (!isNaN(oldLtp) && oldLtp !== newLtp) {
            flashElement(ltpEl, newLtp > oldLtp ? "flash-up" : "flash-down");
        }
    }

    if (bidAskEl && tick.bid_price !== undefined && tick.ask_price !== undefined) {
        bidAskEl.textContent = `${formatNumber(tick.bid_price, 1)} / ${formatNumber(tick.ask_price, 1)}`;
    }

    if (oiEl && tick.open_interest !== undefined && tick.open_interest !== null) {
        oiEl.textContent = formatInteger(tick.open_interest);
    }
}

function flashElement(el, flashClass) {
    el.classList.remove("flash-up", "flash-down");
    void el.offsetWidth;
    el.classList.add(flashClass);
    setTimeout(() => {
        el.classList.remove(flashClass);
    }, 750);
}

/* ==========================================================================
   STRATEGY PAPER TRADES DATA FETCHER & RENDERER
   ========================================================================== */

async function loadStrategyStatus() {
    try {
        const res = await fetchJson("/api/strategy/status");
        if (!res) return;

        // Position
        const pos = res.current_position;
        if (elements.stratCurrentPosition) {
            if (pos && pos.status === "OPEN") {
                const isCe = pos.side === "BUY_CE";
                elements.stratCurrentPosition.innerHTML = `
                    <span class="${isCe ? 'text--up' : 'text--down'}">
                        ${escapeHtml(pos.side)}: ${escapeHtml(pos.symbol)}
                    </span>
                `;
                elements.stratPositionDetail.textContent = `Entry @ ${formatNumber(pos.entry_price, 2)} (${pos.entry_time_ist})`;
            } else {
                elements.stratCurrentPosition.textContent = "FLAT (NO POSITION)";
                elements.stratPositionDetail.textContent = "Scanning 1% ITM contracts";
            }
        }

        // Summary (Distinguish Today vs Lifetime)
        const todayTrades = res.today_trades !== undefined ? res.today_trades : (res.total_trades || 0);
        const lifetimeTrades = res.lifetime_trades !== undefined ? res.lifetime_trades : todayTrades;
        const todayWins = res.today_win_trades || 0;
        const todayLosses = res.today_loss_trades || 0;
        const todayPnl = Number(res.today_net_pnl_inr !== undefined ? res.today_net_pnl_inr : (res.net_pnl_inr || 0));
        const todayPts = Number(res.today_pnl_points !== undefined ? res.today_pnl_points : (res.pnl_points_total || 0));
        const lifetimePnl = Number(res.lifetime_net_pnl_inr !== undefined ? res.lifetime_net_pnl_inr : todayPnl);
        const lot = res.config?.lot_size || 65;

        if (elements.stratTotalTrades) {
            elements.stratTotalTrades.textContent = `${todayTrades} Trade${todayTrades === 1 ? '' : 's'} Today`;
        }
        if (elements.stratWinLossRatio) {
            elements.stratWinLossRatio.textContent = `${todayWins} Wins / ${todayLosses} Losses (Overall: ${lifetimeTrades} trades)`;
        }
        if (elements.stratNetPnl) {
            elements.stratNetPnl.textContent = `${todayPnl >= 0 ? '+' : ''}₹${formatNumber(todayPnl, 2)}`;
            elements.stratNetPnl.className = todayPnl >= 0 ? "text--up" : "text--down";
        }
        if (elements.stratPnlPoints) {
            elements.stratPnlPoints.textContent = `${todayPts >= 0 ? '+' : ''}${formatNumber(todayPts, 2)} Pts Today (Overall: ${lifetimePnl >= 0 ? '+' : ''}₹${formatNumber(lifetimePnl, 2)})`;
            elements.stratPnlPoints.className = todayPts >= 0 ? "text--up" : "text--down";
        }

        // Config inputs
        const cfg = res.config || {};
        if (elements.stratQtyThreshold && document.activeElement !== elements.stratQtyThreshold) {
            elements.stratQtyThreshold.value = cfg.qty_threshold || 50;
        }
        if (elements.stratValThreshold && document.activeElement !== elements.stratValThreshold) {
            elements.stratValThreshold.value = cfg.value_threshold_lakhs || 5;
        }
        if (elements.stratCondDayHigh) elements.stratCondDayHigh.checked = !!cfg.use_day_high_condition;
        if (elements.stratCondPrevClose) elements.stratCondPrevClose.checked = !!cfg.use_prev_close_condition;
        if (elements.stratCondUptick) elements.stratCondUptick.checked = !!cfg.use_uptick_condition;
    } catch (err) {
        console.error("Strategy status failed", err);
    }
}

async function loadStrategyTrades() {
    try {
        const res = await fetchJson("/api/strategy/trades?limit=100");
        const trades = res.trades || [];
        if (!elements.stratTradesBody) return;

        if (!trades.length) {
            elements.stratTradesBody.innerHTML = `
                <tr>
                    <td colspan="16" class="table-empty">
                        No paper trades recorded yet. Scanning 1% ITM contracts between 09:15 and 15:15...
                    </td>
                </tr>
            `;
            return;
        }

        elements.stratTradesBody.innerHTML = trades.map(t => {
            const isCe = t.side === "BUY_CE";
            const pnl = toNumber(t.net_pnl);
            const pnlPts = toNumber(t.pnl_points);
            const isOpen = t.status === "OPEN";
            const tradeDate = t.trade_date || (t.entry_timestamp_ms ? new Date(t.entry_timestamp_ms).toISOString().slice(0, 10) : '—');

            return `
                <tr>
                    <td style="text-align: left; font-weight: 600;">${escapeHtml(t.trade_id)}</td>
                    <td style="text-align: center; color: var(--muted); font-size: 0.85em;">${escapeHtml(tradeDate)}</td>
                    <td style="text-align: center;">
                        <span class="badge ${isCe ? 'badge--atm' : 'badge--closed'}">${escapeHtml(t.side)}</span>
                    </td>
                    <td style="text-align: left; font-weight: 700; color: #fff;">${escapeHtml(t.symbol)}</td>
                    <td style="text-align: right;">${formatNumber(t.strike, 0)}</td>
                    <td style="text-align: right;">${t.lot_size}</td>
                    <td style="text-align: center;">${escapeHtml(t.entry_time_ist || '—')}</td>
                    <td style="text-align: right;"><strong>${formatNumber(t.entry_price, 2)}</strong></td>
                    <td style="text-align: center;">${escapeHtml(t.exit_time_ist || '—')}</td>
                    <td style="text-align: right;"><strong>${t.exit_price ? formatNumber(t.exit_price, 2) : '—'}</strong></td>
                    <td style="text-align: left; color: var(--muted);">${escapeHtml(t.exit_reason || '—')}</td>
                    <td style="text-align: right;" class="${renderChgClass(pnlPts)}">${pnlPts !== null ? renderChgVal(pnlPts) : '—'}</td>
                    <td style="text-align: right;" class="${renderChgClass(pnl)}">
                        <strong>${pnl !== null ? `${pnl >= 0 ? '+' : ''}₹${formatNumber(pnl, 2)}` : '—'}</strong>
                    </td>
                    <td style="text-align: center;">
                        <span class="badge ${isOpen ? 'badge--atm' : 'badge--closed'}">${isOpen ? 'OPEN' : 'CLOSED'}</span>
                    </td>
                    <td style="text-align: right; color: var(--muted);">${t.spot_price ? formatNumber(t.spot_price, 2) : '—'}</td>
                    <td style="text-align: right; color: var(--muted);">${t.atm_strike ? formatNumber(t.atm_strike, 0) : '—'}</td>
                </tr>
            `;
        }).join("");
    } catch (err) {
        console.error("Failed to load strategy trades", err);
    }
}

async function loadStrategySignals() {
    try {
        const res = await fetchJson("/api/strategy/signals?limit=50");
        const signals = res.signals || [];
        if (!elements.stratSignalsBody) return;

        if (!signals.length) {
            elements.stratSignalsBody.innerHTML = `
                <tr>
                    <td colspan="13" class="table-empty">
                        No buy pressure signals triggered yet.
                    </td>
                </tr>
            `;
            return;
        }

        elements.stratSignalsBody.innerHTML = signals.map(s => {
            const isCe = s.side === "CE";
            const bidAsk = (s.bid_price || s.ask_price)
                ? `${formatNumber(s.bid_price, 2)} / ${formatNumber(s.ask_price, 2)}`
                : "— / —";
            const spreadPct = s.spread_pct != null ? `${formatNumber(s.spread_pct, 2)}%` : "—";
            const oiChg = s.oi_change != null ? (s.oi_change >= 0 ? "+" : "") + formatInteger(s.oi_change) : "—";
            const confColor = s.confidence === "HIGH" ? "var(--primary)" : s.confidence === "MEDIUM" ? "#f59e0b" : "var(--muted)";
            return `
                <tr>
                    <td style="text-align: left;">${escapeHtml(s.timestamp_ist)}</td>
                    <td style="text-align: center;">
                        <span class="badge ${isCe ? 'badge--atm' : 'badge--closed'}">${escapeHtml(s.side)}</span>
                    </td>
                    <td style="text-align: right;">${formatNumber(s.strike, 0)}</td>
                    <td style="text-align: right;"><strong>${formatNumber(s.ltp, 2)}</strong></td>
                    <td style="text-align: right;">+${formatInteger(s.qty_delta)}</td>
                    <td style="text-align: right;">₹${formatNumber(s.val_lakhs, 2)}L</td>
                    <td style="text-align: right;">${formatNumber(s.day_high, 2)}</td>
                    <td style="text-align: right;">${formatNumber(s.prev_close, 2)}</td>
                    <td style="text-align: right; font-size: 0.8em;">${bidAsk}</td>
                    <td style="text-align: right;">${spreadPct}</td>
                    <td style="text-align: right;">${oiChg}</td>
                    <td style="text-align: center; font-weight: 700; color: ${confColor};">${escapeHtml(s.confidence || '—')}</td>
                    <td style="text-align: center; font-weight: 700; color: ${s.action_taken && (s.action_taken.includes('ENTER') || s.action_taken.includes('REVERS')) ? 'var(--primary)' : 'var(--muted)'};">
                        ${escapeHtml(s.action_taken)}
                    </td>
                </tr>
            `;
        }).join("");
    } catch (err) {
        console.error("Failed to load strategy signals", err);
    }
}

async function saveStrategyConfig() {
    const body = {
        qty_threshold: parseFloat(elements.stratQtyThreshold?.value || 50),
        value_threshold_lakhs: parseFloat(elements.stratValThreshold?.value || 5),
        use_day_high_condition: elements.stratCondDayHigh?.checked,
        use_prev_close_condition: elements.stratCondPrevClose?.checked,
        use_uptick_condition: elements.stratCondUptick?.checked,
    };

    if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Saving...";

    try {
        const res = await fetch("/api/strategy/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (res.ok) {
            if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Parameters saved!";
            setTimeout(() => {
                if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Paper engine running";
            }, 2500);
        }
    } catch (err) {
        if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Failed to save";
    }
}