"use strict";

const IST_OFFSET_SEC = 19800; // 5 hours 30 minutes in seconds (IST = UTC + 5:30)

const state = {
    selectedInstrument: "NSE_INDEX|Nifty 50",
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
    latestIndexPrices: {
        nifty: null,
        banknifty: null,
        sensex: null,
    },
    tickSnapshots: [],
    eventSource: null,
    activeView: "gchart",
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

    // Initialise GChart & Strategy live polling
    loadStrategyStatus();
    window.setInterval(() => {
        if (!document.hidden) {
            loadStrategyStatus();
        }
    }, 1500);
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
        // GChart Header & Ticker elements
        "gchartClock",
        "gchartSpot",
        "gchartExpiry",
        "btnToggleOptionChain",
        "btnTqResult",
        "btnTqTrades",
        "btnTqSettings",
        "tqPaneResult",
        "tqPaneTrades",
        "tqPaneSettings",
        "tqPosSummary",
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
        "btnRefreshChart",
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
        "stratModeText",
        "stratLots",
        "stratOrderBuffer",
        "stratMaxSpread",
        "stratMode",
        "btnSaveStratConfig",
        "btnStratArmNow",
        "btnStratReset",
        "btnStratExit",
        "stratConfigStatus",
        "stratLockedBody",
        "stratTradesBody",
        "stratSignalsBody",
    ];

    for (const id of ids) {
        elements[id] = document.getElementById(id);
    }
}

function bindEvents() {
    // Live GChart Clock
    function updateClock() {
        if (elements.gchartClock) {
            const now = new Date();
            const timeStr = now.toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour12: false });
            elements.gchartClock.textContent = timeStr;
        }
    }
    updateClock();
    window.setInterval(updateClock, 1000);

    // Toggle Option Chain View
    if (elements.btnToggleOptionChain) {
        elements.btnToggleOptionChain.addEventListener("click", () => {
            if (elements.viewOptionChain) {
                elements.viewOptionChain.hidden = !elements.viewOptionChain.hidden;
                if (!elements.viewOptionChain.hidden && !state.chainData) {
                    loadOptionChain(true);
                }
            }
        });
    }

    // Tick Query Subtabs
    const tqTabs = [
        { btn: elements.btnTqResult, pane: elements.tqPaneResult },
        { btn: elements.btnTqTrades, pane: elements.tqPaneTrades, onLoad: loadStrategyTrades },
        { btn: elements.btnTqSettings, pane: elements.tqPaneSettings },
    ];
    tqTabs.forEach(t => {
        if (t.btn && t.pane) {
            t.btn.addEventListener("click", () => {
                tqTabs.forEach(other => {
                    if (other.btn) other.btn.classList.toggle("active", other === t);
                    if (other.pane) other.pane.hidden = (other !== t);
                });
                if (t.onLoad) t.onLoad();
            });
        }
    });

    // Chart Timeframe Period Keypad
    document.querySelectorAll(".gchart-key-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".gchart-key-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            const tf = btn.getAttribute("data-timeframe");
            if (tf) {
                state.timeframe = tf;
                loadCandles();
            }
        });
    });

    // Chart Scale Reset
    if (elements.btnRefreshChart) {
        elements.btnRefreshChart.addEventListener("click", () => {
            if (state.tvChart) {
                state.tvChart.timeScale().resetTimeScale();
                state.tvChart.timeScale().fitContent();
            }
        });
    }

    // Strategy configuration save button
    if (elements.btnSaveStratConfig) {
        elements.btnSaveStratConfig.addEventListener("click", saveStrategyConfig);
    }
    if (elements.btnStratArmNow) {
        elements.btnStratArmNow.addEventListener("click", async () => {
            if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Arming bot now...";
            try {
                const res = await fetch("/api/strategy/arm-now", { method: "POST" });
                if (res.ok) {
                    await loadStrategyStatus();
                    if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "⚡ Bot ARMED!";
                }
            } catch (err) {
                console.error("Arm bot error", err);
            }
        });
    }
    if (elements.btnStratReset) {
        elements.btnStratReset.addEventListener("click", async () => {
            if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Resetting bot...";
            try {
                const res = await fetch("/api/strategy/reset", { method: "POST" });
                if (res.ok) {
                    await loadStrategyStatus();
                    if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Bot reset to PLANNING";
                }
            } catch (err) {
                console.error("Reset bot error", err);
            }
        });
    }
    if (elements.btnStratExit) {
        elements.btnStratExit.addEventListener("click", async () => {
            if (!confirm("Are you sure you want to manually exit any open position now?")) return;
            if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Exiting position...";
            try {
                const res = await fetch("/api/strategy/exit", { method: "POST" });
                if (res.ok) {
                    await loadStrategyStatus();
                    await loadStrategyTrades();
                    if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Position exited!";
                }
            } catch (err) {
                console.error("Manual exit error", err);
            }
        });
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
            if (elements.selectedInstrument) {
                elements.selectedInstrument.textContent =
                    state.selectedInstrument || "Waiting for data";
            }

            // Reset active candle, map, and chart series data to prevent price scale squish/collision
            state.activeCandle = null;
            state.lastTickTimeSec = 0;
            state.candles.clear();
            if (state.areaSeries) state.areaSeries.setData([]);
            if (state.candleSeries) state.candleSeries.setData([]);
            if (state.volumeSeries) state.volumeSeries.setData([]);
            if (state.tvChart) {
                state.tvChart.priceScale("right").applyOptions({ autoScale: true });
            }

            await loadCandles();
            refreshVisibleTick();

            if (state.tvChart) {
                state.tvChart.timeScale().resetTimeScale();
                state.tvChart.timeScale().fitContent();
            }
        });
    }

    // Refresh & Reset Chart button
    if (elements.btnRefreshChart) {
        elements.btnRefreshChart.addEventListener("click", async () => {
            const icon = elements.btnRefreshChart.querySelector("svg");
            if (icon) {
                icon.style.transition = "transform 0.5s ease";
                icon.style.transform = "rotate(360deg)";
                setTimeout(() => { icon.style.transform = ""; }, 500);
            }

            state.activeCandle = null;
            state.lastTickTimeSec = 0;
            if (state.tvChart) {
                state.tvChart.priceScale("right").applyOptions({ autoScale: true });
            }

            await loadCandles();
            refreshVisibleTick();

            if (state.tvChart) {
                state.tvChart.timeScale().resetTimeScale();
                state.tvChart.timeScale().fitContent();
            }
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
        height: container.clientHeight || 520,
        layout: {
            background: { type: "solid", color: "#000000" },
            textColor: "#cbd5e1",
            fontSize: 12,
            fontFamily: "'Consolas', monospace",
        },
        localization: {
            locale: "en-IN",
            dateFormat: "dd MMM yyyy",
        },
        grid: {
            vertLines: { color: "#0d1522" },
            horzLines: { color: "#0d1522" },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: {
                color: "rgba(0, 255, 102, 0.4)",
                width: 1,
                style: LightweightCharts.LineStyle.Dashed,
            },
            horzLine: {
                color: "rgba(0, 255, 102, 0.4)",
                width: 1,
                style: LightweightCharts.LineStyle.Dashed,
            },
        },
        rightPriceScale: {
            borderColor: "#1e293b",
            autoScale: true,
            scaleMargins: {
                top: 0.08,
                bottom: 0.16,
            },
        },
        timeScale: {
            borderColor: "#1e293b",
            timeVisible: true,
            secondsVisible: true,
        },
    });

    state.volumeSeries = state.tvChart.addHistogramSeries({
        color: "rgba(0, 255, 102, 0.25)",
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
        upColor: "#00ff66",
        downColor: "#ff2a4b",
        borderUpColor: "#00ff66",
        borderDownColor: "#ff2a4b",
        wickUpColor: "#00ff66",
        wickDownColor: "#ff2a4b",
    });

    // Auto-fit on resize
    if (window.ResizeObserver) {
        const ro = new ResizeObserver(() => {
            if (state.tvChart && container.clientWidth > 0) {
                state.tvChart.applyOptions({
                    width: container.clientWidth,
                    height: container.clientHeight || 520,
                });
            }
        });
        ro.observe(container);
    }

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

    state.activeCandle = null;
    state.lastTickTimeSec = 0;

    if (state.tvChart) {
        state.tvChart.applyOptions({
            timeScale: {
                secondsVisible: tf === "tick",
            },
        });
        state.tvChart.priceScale("right").applyOptions({ autoScale: true });
    }

    if (tf === "tick") {
        if (state.candleSeries) {
            state.candleSeries.applyOptions({ visible: false });
            state.candleSeries.setData([]);
        }
        if (state.volumeSeries) {
            state.volumeSeries.applyOptions({ visible: false });
            state.volumeSeries.setData([]);
        }
        if (state.areaSeries) {
            state.areaSeries.applyOptions({ visible: true });
        }
    } else {
        if (state.areaSeries) {
            state.areaSeries.applyOptions({ visible: false });
            state.areaSeries.setData([]);
        }
        if (state.candleSeries) {
            state.candleSeries.applyOptions({ visible: true });
        }
        if (state.volumeSeries) {
            state.volumeSeries.applyOptions({ visible: true });
        }
    }

    await loadCandles();
    if (state.tvChart) {
        state.tvChart.timeScale().resetTimeScale();
        state.tvChart.timeScale().fitContent();
    }
}

async function loadInitialData() {
    try {
        const [instrumentsResult, ticksResult] = await Promise.all([
            fetchJson("/api/instruments"),
            fetchJson("/api/ticks?limit=150"),
        ]);

        populateInstrumentSelect(instrumentsResult.instruments || []);
        state.ticks = ticksResult.ticks || [];
        rebuildTickSnapshots(state.ticks);
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

                let sec = Math.floor(tsMs / 1000) + IST_OFFSET_SEC;
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
                state.tvChart.priceScale("right").applyOptions({ autoScale: true });
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
            const timeSec = Math.floor(Number(candle.candle_start_ms) / 1000) + IST_OFFSET_SEC;
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
            state.tvChart.priceScale("right").applyOptions({ autoScale: true });
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

    if (event.type === "strategy_update") {
        if (state.activeView === "strategy") {
            loadStrategyStatus();
        }
        return;
    }

    if (event.type === "strategy_trade") {
        if (state.activeView === "strategy") {
            loadStrategyStatus();
            loadStrategyTrades();
        }
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
    state.ticks = state.ticks.slice(0, 100);

    updateTickSnapshot(tick);
    renderTicks();

    // LIVE MILLISECOND OPTION CHAIN UPDATES
    updateChainSpotFromTick(tick);
    updateChainRowFromTick(tick);

    if (instrumentKey === state.selectedInstrument) {
        renderLatestTick(tick);

        const ltp = toNumber(tick.ltp);
        if (ltp !== null && state.tvChart) {
            if (state.timeframe === "tick" && state.areaSeries) {
                const nowSec = Math.floor(Date.now() / 1000) + IST_OFFSET_SEC;
                const tSec = Math.max(nowSec, (state.lastTickTimeSec || 0) + 1);
                state.lastTickTimeSec = tSec;
                state.areaSeries.update({ time: tSec, value: ltp });
                if (elements.chartEmpty) elements.chartEmpty.hidden = true;
            } else if (state.candleSeries && (state.timeframe === "1m" || state.timeframe === "5m")) {
                const bucketDurationSec = state.timeframe === "5m" ? 300 : 60;
                const tsMs = Number(tick.exchange_timestamp_ms || Date.now());
                const rawBucketSec = Math.floor(tsMs / (bucketDurationSec * 1000)) * bucketDurationSec;
                const chartBucketSec = rawBucketSec + IST_OFFSET_SEC;

                let candle = state.activeCandle;
                if (!candle || candle.time !== chartBucketSec || candle.instrumentKey !== instrumentKey) {
                    const existing = state.candles.get(rawBucketSec * 1000);
                    if (existing) {
                        candle = {
                            instrumentKey: instrumentKey,
                            time: chartBucketSec,
                            open: toNumber(existing.open),
                            high: Math.max(toNumber(existing.high), ltp),
                            low: Math.min(toNumber(existing.low), ltp),
                            close: ltp,
                        };
                    } else {
                        candle = {
                            instrumentKey: instrumentKey,
                            time: chartBucketSec,
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
                state.candleSeries.update({
                    time: candle.time,
                    open: candle.open,
                    high: candle.high,
                    low: candle.low,
                    close: candle.close,
                });
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
        const timeSec = Math.floor(Number(candle.candle_start_ms) / 1000) + IST_OFFSET_SEC;
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

function updateTickSnapshot(tick) {
    const inst = String(tick.instrument_key || "");
    const ltp = toNumber(tick.ltp);
    if (ltp === null) return;

    if (inst.includes("Nifty 50")) {
        state.latestIndexPrices.nifty = ltp;
    } else if (inst.includes("Bank")) {
        state.latestIndexPrices.banknifty = ltp;
    } else if (inst.includes("SENSEX") || inst.includes("Sensex")) {
        state.latestIndexPrices.sensex = ltp;
    }

    const tsMs = Number(tick.exchange_timestamp_ms || tick.received_timestamp_ms || Date.now());
    const sec = Math.floor(tsMs / 1000);

    if (!state.tickSnapshots.length || state.tickSnapshots[0].sec !== sec) {
        state.tickSnapshots.unshift({
            sec: sec,
            timeMs: tsMs,
            nifty: state.latestIndexPrices.nifty,
            banknifty: state.latestIndexPrices.banknifty,
            sensex: state.latestIndexPrices.sensex,
        });
        if (state.tickSnapshots.length > 50) {
            state.tickSnapshots.pop();
        }
    } else {
        const current = state.tickSnapshots[0];
        current.timeMs = tsMs;
        if (inst.includes("Nifty 50")) current.nifty = ltp;
        else if (inst.includes("Bank")) current.banknifty = ltp;
        else if (inst.includes("SENSEX") || inst.includes("Sensex")) current.sensex = ltp;
    }
}

function rebuildTickSnapshots(rawTicksDesc) {
    state.tickSnapshots = [];
    const ticksAsc = [...rawTicksDesc].reverse();
    for (const tick of ticksAsc) {
        updateTickSnapshot(tick);
    }
}

function renderTicks() {
    if (!state.tickSnapshots || !state.tickSnapshots.length) {
        elements.ticksBody.innerHTML = `
            <tr>
                <td colspan="4" class="table-empty">
                    No market ticks received yet.
                </td>
            </tr>
        `;
        return;
    }

    elements.ticksBody.innerHTML = state.tickSnapshots.map((snap) => `
        <tr>
            <td style="font-variant-numeric: tabular-nums; color: var(--muted); font-size: 0.8rem;">
                ${escapeHtml(formatTime(snap.timeMs))}
            </td>
            <td style="font-variant-numeric: tabular-nums; font-weight: 600; color: #53e0c1; font-size: 0.85rem;">
                ${snap.nifty !== null && snap.nifty !== undefined ? '₹' + escapeHtml(formatNumber(snap.nifty)) : '—'}
            </td>
            <td style="font-variant-numeric: tabular-nums; font-weight: 600; color: #60a5fa; font-size: 0.85rem;">
                ${snap.banknifty !== null && snap.banknifty !== undefined ? '₹' + escapeHtml(formatNumber(snap.banknifty)) : '—'}
            </td>
            <td style="font-variant-numeric: tabular-nums; font-weight: 600; color: #f59e0b; font-size: 0.85rem;">
                ${snap.sensex !== null && snap.sensex !== undefined ? '₹' + escapeHtml(formatNumber(snap.sensex)) : '—'}
            </td>
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
   OPENING GAP ENTRY BOT DATA FETCHER & RENDERER (NIFTY 50)
   ========================================================================== */

async function loadStrategyStatus() {
    try {
        const res = await fetchJson("/api/strategy/status");
        if (!res) return;

        // 1. Bot State Badge & Mode
        const stateStr = res.state || "SETUP";
        if (elements.stratStatusBadge) {
            elements.stratStatusBadge.textContent = stateStr;
            elements.stratStatusBadge.className = 'gchart-state-badge state-' + stateStr.toLowerCase();
        }
        if (elements.gchartSpot && res.spot_price) {
            elements.gchartSpot.textContent = `NC=${formatNumber(res.spot_price, 2)}`;
        }
        if (elements.gchartExpiry && res.active_expiry) {
            elements.gchartExpiry.textContent = `EXP: ${res.active_expiry}`;
        }

        // 2. Active Position (Only 1 active position for NIFTY 50)
        const pos = res.current_position;
        if (elements.stratCurrentPosition) {
            if (pos && pos.status === "OPEN") {
                const isCe = (pos.side || "").includes("CE");
                const pts = toNumber(pos.pnl_points) || 0;
                const net = toNumber(pos.net_pnl) || 0;
                elements.stratCurrentPosition.innerHTML = `
                    <span style="color: ${isCe ? '#00ff66' : '#ff3344'}; font-weight: 700;">
                        ${escapeHtml(pos.side)}: ${escapeHtml(pos.symbol)}
                    </span>
                `;
                if (elements.stratPositionDetail) {
                    elements.stratPositionDetail.textContent = `@ ${formatNumber(pos.entry_price, 2)} (${pos.entry_time_ist}) | Cur: @ ${formatNumber(pos.current_price || pos.entry_price, 2)}`;
                }
                if (elements.stratNetPnl) {
                    elements.stratNetPnl.textContent = `${net >= 0 ? '+' : ''}₹${formatNumber(net, 2)}`;
                    elements.stratNetPnl.className = net >= 0 ? "diff-positive" : "diff-negative";
                }
                if (elements.stratPnlPoints) {
                    elements.stratPnlPoints.textContent = `(${pts >= 0 ? '+' : ''}${formatNumber(pts, 2)} pts)`;
                }
                if (elements.tqPosSummary) {
                    elements.tqPosSummary.innerHTML = `<strong style="color: #53e0c1;">ACTIVE:</strong> ${escapeHtml(pos.symbol)} @ ${formatNumber(pos.entry_price, 2)} | P&L: ₹${formatNumber(net, 2)}`;
                }
            } else {
                elements.stratCurrentPosition.textContent = "FLAT (NO POSITION)";
                elements.stratCurrentPosition.style.color = "#94a3b8";
                if (elements.stratPositionDetail) {
                    elements.stratPositionDetail.textContent = 
                        stateStr === "HOLDING" ? "Managing position" :
                        stateStr === "ARMED" ? "Armed! Watching 1st positive tick at 09:15" :
                        stateStr === "LOCKED" ? "8 ITM candidates locked with Ref LTP at 09:14:55" :
                        stateStr === "CLOSED" ? "Daily trade completed for today" :
                        stateStr === "NO_TRADE" ? "Entry window expired. No trade today." :
                        "Watching 8 ITM candidates at 09:15:00";
                }
                if (elements.stratNetPnl) {
                    const todayPnl = Number(res.today_net_pnl_inr || 0);
                    elements.stratNetPnl.textContent = `${todayPnl >= 0 ? '+' : ''}₹${formatNumber(todayPnl, 2)}`;
                    elements.stratNetPnl.className = todayPnl >= 0 ? "diff-positive" : "diff-negative";
                }
                if (elements.stratPnlPoints) {
                    const todayPts = Number(res.today_pnl_points || 0);
                    elements.stratPnlPoints.textContent = `(${todayPts >= 0 ? '+' : ''}${formatNumber(todayPts, 2)} pts)`;
                }
                if (elements.tqPosSummary) {
                    elements.tqPosSummary.textContent = "Watching 8 ITM Candidates at 09:15:00";
                }
            }
        }

        // 3. Today & Lifetime Performance
        const todayTrades = res.today_trades || 0;
        if (elements.stratTotalTrades) {
            elements.stratTotalTrades.textContent = `${todayTrades} / 1 Traded`;
        }

        // 4. Config Inputs
        const cfg = res.config || {};
        if (elements.stratLots && document.activeElement !== elements.stratLots) {
            elements.stratLots.value = cfg.lots || 1;
        }
        if (elements.stratOrderBuffer && document.activeElement !== elements.stratOrderBuffer) {
            elements.stratOrderBuffer.value = cfg.order_buffer || 0.50;
        }
        if (elements.stratMaxSpread && document.activeElement !== elements.stratMaxSpread) {
            elements.stratMaxSpread.value = cfg.max_spread_pct || 3.0;
        }
        if (elements.stratMode && document.activeElement !== elements.stratMode) {
            elements.stratMode.value = cfg.mode || "paper";
        }

        // 5. 8 Locked ITM Candidates Table (GChart Table format matching user screenshot)
        const lockedList = res.locked_contracts || [];
        if (elements.stratLockedBody) {
            if (!lockedList.length) {
                elements.stratLockedBody.innerHTML = `
                    <tr>
                        <td colspan="6" style="text-align: center; color: var(--muted); padding: 30px;">
                            Awaiting spot tick to plan 8 ITM candidates (4 CE below spot, 4 PE above spot)...
                        </td>
                    </tr>
                `;
            } else {
                elements.stratLockedBody.innerHTML = lockedList.map(c => {
                    const ltp = toNumber(c.ltp);
                    const ref = toNumber(c.ref_ltp);
                    const diff = (ltp !== null && ref !== null) ? (ltp - ref) : null;
                    const diffClass = (diff !== null && diff > 0.001) ? "diff-positive" :
                                      (diff !== null && diff < -0.001) ? "diff-negative" : "diff-zero";
                    const diffText = (diff !== null) ? ((diff > 0 ? "+" : "") + formatNumber(diff, 2)) : "—";
                    const isWinner = res.last_decision && res.last_decision.chosen_strike === c.strike && res.last_decision.side === c.side;
                    const timeStr = formatTime(c.last_trade_ms || Date.now());

                    return `
                        <tr class="${isWinner ? 'winner-row' : ''}">
                            <td style="color: #94a3b8; font-size: 0.8em;">${escapeHtml(timeStr)}</td>
                            <td>
                                <span style="font-weight: 700; color: #fff;">${escapeHtml(c.symbol || '')}</span>
                                <span style="color: #f59e0b; font-size: 0.8em; margin-left: 4px;">[${ref !== null ? formatNumber(ref, 1) : '—'}]</span>
                            </td>
                            <td style="font-weight: 700; color: #fff;">${ltp !== null ? formatNumber(ltp, 2) : '—'}</td>
                            <td class="${diffClass}">${diffText}</td>
                            <td style="color: #94a3b8;">${c.bid_qty || c.ask_qty || 65}</td>
                            <td style="color: #38bdf8; font-weight: 600;">${c.spread_pct != null ? formatNumber(c.spread_pct, 2) + '%' : '—'}</td>
                        </tr>
                    `;
                }).join("");
            }
        }

    } catch (err) {
        console.error("Opening Gap Strategy status failed", err);
    }
}

async function loadStrategyTrades() {
    try {
        const res = await fetchJson("/api/strategy/trades?limit=50");
        const trades = res.trades || [];
        if (!elements.stratTradesBody) return;

        if (!trades.length) {
            elements.stratTradesBody.innerHTML = `
                <tr>
                    <td colspan="7" style="text-align: center; color: var(--muted); padding: 25px;">
                        No trades executed today. Opening gap entry triggers on first positive tick at 09:15.
                    </td>
                </tr>
            `;
            return;
        }

        elements.stratTradesBody.innerHTML = trades.map(t => {
            const isCe = (t.side || "").includes("CE");
            const pnl = toNumber(t.net_pnl);
            const isOpen = t.status === "OPEN";

            return `
                <tr>
                    <td style="font-weight: 600; color: #cbd5e1;">${escapeHtml(t.trade_id)}</td>
                    <td style="text-align: center;">
                        <span class="badge ${isCe ? 'badge--atm' : 'badge--closed'}">${escapeHtml(t.side)}</span>
                    </td>
                    <td style="font-weight: 700; color: #fff;">${escapeHtml(t.symbol)}</td>
                    <td style="font-weight: 700;">${formatNumber(t.entry_price, 2)}</td>
                    <td style="font-weight: 700;">${t.exit_price ? formatNumber(t.exit_price, 2) : '—'}</td>
                    <td class="${pnl !== null && pnl >= 0 ? 'diff-positive' : 'diff-negative'}">
                        <strong>${pnl !== null ? `${pnl >= 0 ? '+' : ''}₹${formatNumber(pnl, 2)}` : '—'}</strong>
                    </td>
                    <td style="text-align: center;">
                        <span class="badge ${isOpen ? 'badge--atm' : 'badge--closed'}">${isOpen ? 'OPEN' : 'CLOSED'}</span>
                    </td>
                </tr>
            `;
        }).join("");
    } catch (err) {
        console.error("Failed to load strategy trades", err);
    }
}

async function saveStrategyConfig() {
    const body = {
        lots: parseInt(elements.stratLots?.value || 1, 10),
        order_buffer: parseFloat(elements.stratOrderBuffer?.value || 0.50),
        max_spread_pct: parseFloat(elements.stratMaxSpread?.value || 3.0),
        mode: elements.stratMode?.value || "paper",
    };

    if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Saving rules...";

    try {
        const res = await fetch("/api/strategy/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (res.ok) {
            if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Parameters saved!";
            await loadStrategyStatus();
            setTimeout(() => {
                if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Opening gap engine ready";
            }, 2500);
        }
    } catch (err) {
        if (elements.stratConfigStatus) elements.stratConfigStatus.textContent = "Failed to save";
    }
}
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