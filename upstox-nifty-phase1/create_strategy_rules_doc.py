import os
import sys
from pathlib import Path
import docx
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn

def build_trading_rules_document(output_path: str):
    doc = Document()

    # Configure Margins (Normal 1 inch)
    sections = doc.sections
    for section in sections:
        section.top_margin = Inches(0.8)
        section.bottom_margin = Inches(0.8)
        section.left_margin = Inches(0.8)
        section.right_margin = Inches(0.8)

    # Styling Palettes
    NAVY = RGBColor(26, 54, 93)       # #1A365D - Main Headers
    BLUE = RGBColor(37, 99, 235)      # #2563EB - Subheaders
    DARK = RGBColor(31, 41, 55)       # #1F2937 - Body Text
    GRAY = RGBColor(107, 114, 128)    # #6B7280 - Captions / Meta
    GREEN = RGBColor(21, 128, 61)     # #15803D - Success / Bullish
    RED = RGBColor(185, 28, 28)       # #B91C1C - Danger / Bearish
    GOLD = RGBColor(180, 83, 9)       # #B45309 - Warning / Stars

    HEX_PRIMARY = "1A365D"
    HEX_HEADER_BG = "1E3A8A"
    HEX_ZEBRA = "F8FAFC"
    HEX_BORDER = "CBD5E1"

    # Base Styles
    normal_style = doc.styles['Normal']
    normal_style.font.name = 'Segoe UI'
    normal_style.font.size = Pt(10)
    normal_style.font.color.rgb = DARK
    normal_style.paragraph_format.line_spacing = 1.15
    normal_style.paragraph_format.space_after = Pt(4)

    def set_cell_background(cell, fill_hex: str):
        tcPr = cell._tc.get_or_add_tcPr()
        shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
        tcPr.append(shd)

    def set_cell_margins(cell, top=120, bottom=120, left=160, right=160):
        tcPr = cell._tc.get_or_add_tcPr()
        tcMar = parse_xml(f'<w:tcMar {nsdecls("w")}><w:top w:w="{top}" w:type="dxa"/><w:bottom w:w="{bottom}" w:type="dxa"/><w:left w:w="{left}" w:type="dxa"/><w:right w:w="{right}" w:type="dxa"/></w:tcMar>')
        tcPr.append(tcMar)

    def set_table_borders(table, color=HEX_BORDER, sz="4", val="single"):
        tblPr = table._tbl.tblPr
        borders = parse_xml(
            f'<w:tblBorders {nsdecls("w")}>'
            f'<w:top w:val="{val}" w:sz="{sz}" w:space="0" w:color="{color}"/>'
            f'<w:bottom w:val="{val}" w:sz="{sz}" w:space="0" w:color="{color}"/>'
            f'<w:left w:val="none"/>'
            f'<w:right w:val="none"/>'
            f'<w:insideH w:val="{val}" w:sz="{sz}" w:space="0" w:color="{color}"/>'
            f'<w:insideV w:val="none"/>'
            f'</w:tblBorders>'
        )
        tblPr.append(borders)

    def add_callout(text: str, title: str = "NOTE", kind: str = "note"):
        palette = {
            "note": {"bg": "EFF6FF", "border": "2563EB", "title_col": BLUE},
            "warning": {"bg": "FFFBEB", "border": "D97706", "title_col": GOLD},
            "caution": {"bg": "FEF2F2", "border": "DC2626", "title_col": RED},
            "tip": {"bg": "F0FDF4", "border": "16A34A", "title_col": GREEN},
        }
        cfg = palette.get(kind, palette["note"])
        tbl = doc.add_table(rows=1, cols=1)
        tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
        tbl.autofit = False
        cell = tbl.cell(0, 0)
        cell.width = Inches(6.8)
        set_cell_background(cell, cfg["bg"])
        set_cell_margins(cell, top=140, bottom=140, left=200, right=180)
        tcPr = cell._tc.get_or_add_tcPr()
        borders = parse_xml(
            f'<w:tcBorders {nsdecls("w")}>'
            f'<w:top w:val="none"/>'
            f'<w:left w:val="single" w:sz="36" w:space="0" w:color="{cfg["border"]}"/>'
            f'<w:bottom w:val="none"/>'
            f'<w:right w:val="none"/>'
            f'</w:tcBorders>'
        )
        tcPr.append(borders)
        p = cell.paragraphs[0]
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(2)
        r_t = p.add_run(f"[{title}] ")
        r_t.bold = True
        r_t.font.name = "Segoe UI"
        r_t.font.size = Pt(10)
        r_t.font.color.rgb = cfg["title_col"]
        r_txt = p.add_run(text)
        r_txt.font.name = "Segoe UI"
        r_txt.font.size = Pt(9.5)
        r_txt.font.color.rgb = DARK
        doc.add_paragraph().paragraph_format.space_after = Pt(4)

    def add_h1(text: str):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(18)
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        run.bold = True
        run.font.name = "Segoe UI"
        run.font.size = Pt(16)
        run.font.color.rgb = NAVY
        return p

    def add_h2(text: str):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(12)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        run.bold = True
        run.font.name = "Segoe UI"
        run.font.size = Pt(12.5)
        run.font.color.rgb = BLUE
        return p

    def add_h3(text: str):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(8)
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        run.bold = True
        run.font.name = "Segoe UI"
        run.font.size = Pt(10.5)
        run.font.color.rgb = DARK
        return p

    def add_bullet(bold_prefix: str, text: str):
        p = doc.add_paragraph(style='List Bullet')
        p.paragraph_format.space_before = Pt(1)
        p.paragraph_format.space_after = Pt(2)
        r_pre = p.add_run(bold_prefix)
        r_pre.bold = True
        r_pre.font.name = "Segoe UI"
        r_pre.font.size = Pt(9.5)
        r_pre.font.color.rgb = DARK
        r_body = p.add_run(f" {text}")
        r_body.font.name = "Segoe UI"
        r_body.font.size = Pt(9.5)
        r_body.font.color.rgb = DARK
        return p

    def style_table_headers(table, col_widths, headers):
        hdr_row = table.rows[0]
        tblPr = table._tbl.tblPr
        tblPr.append(parse_xml(f'<w:tblHeader {nsdecls("w")}/>'))
        for idx, text in enumerate(headers):
            cell = hdr_row.cells[idx]
            cell.width = col_widths[idx]
            set_cell_background(cell, HEX_HEADER_BG)
            set_cell_margins(cell, top=140, bottom=140, left=140, right=140)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            run = p.add_run(text)
            run.bold = True
            run.font.name = "Segoe UI"
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(255, 255, 255)

    def populate_table_rows(table, col_widths, data, align_cols=None):
        set_table_borders(table)
        for r_idx, row_data in enumerate(data):
            row = table.add_row()
            bg_col = HEX_ZEBRA if (r_idx % 2 == 1) else "FFFFFF"
            for c_idx, val in enumerate(row_data):
                cell = row.cells[c_idx]
                cell.width = col_widths[c_idx]
                set_cell_background(cell, bg_col)
                set_cell_margins(cell, top=100, bottom=100, left=130, right=130)
                cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
                p = cell.paragraphs[0]
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.space_after = Pt(0)
                align = WD_ALIGN_PARAGRAPH.LEFT
                if align_cols and c_idx in align_cols:
                    align = align_cols[c_idx]
                p.alignment = align
                run = p.add_run(str(val))
                run.font.name = "Segoe UI"
                run.font.size = Pt(8.5)
                run.font.color.rgb = DARK

    # =========================================================================
    # DOCUMENT HEADER / TITLE BLOCK
    # =========================================================================
    p_title = doc.add_paragraph()
    p_title.paragraph_format.space_before = Pt(10)
    p_title.paragraph_format.space_after = Pt(2)
    p_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p_title.add_run("OI BUILDUP TRADING SYSTEM")
    r.bold = True
    r.font.name = "Segoe UI"
    r.font.size = Pt(24)
    r.font.color.rgb = NAVY

    p_sub = doc.add_paragraph()
    p_sub.paragraph_format.space_before = Pt(0)
    p_sub.paragraph_format.space_after = Pt(8)
    p_sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_sub = p_sub.add_run("Automated Execution Rules, Entry & Exit Reference Manual")
    r_sub.font.name = "Segoe UI"
    r_sub.font.size = Pt(13)
    r_sub.font.color.rgb = BLUE

    p_meta = doc.add_paragraph()
    p_meta.paragraph_format.space_before = Pt(0)
    p_meta.paragraph_format.space_after = Pt(16)
    p_meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r_meta = p_meta.add_run("Author: Algo Trading Engineering Team  ·  Specification: Dr. Vijay Bhilwade  ·  Version 3.2 Production Release")
    r_meta.font.name = "Segoe UI"
    r_meta.font.size = Pt(9)
    r_meta.font.color.rgb = GRAY

    # Add divider line
    p_div = doc.add_paragraph()
    p_div.paragraph_format.space_after = Pt(12)
    p_div_border = parse_xml(f'<w:pBdr {nsdecls("w")}><w:bottom w:val="single" w:sz="12" w:space="1" w:color="{HEX_PRIMARY}"/></w:pBdr>')
    p_div._p.get_or_add_pPr().append(p_div_border)

    # Executive Summary Box
    add_callout(
        "This document specifies the complete, mathematically rigorous operational logic executed by the automated trading bot across Indices (NIFTY 50, BANK NIFTY, SENSEX) and NIFTY 50 Stocks. Every entry, sizing matrix, futures gate, time cutoff, and multi-factor exit is detailed in production fidelity. Follow the recommended 2-to-3 week paper incubation protocol before live capital deployment.",
        title="SYSTEM MANDATE & OPERATIONAL INTEGRITY",
        kind="note"
    )

    # =========================================================================
    # SECTION 1: ARCHITECTURAL OVERVIEW & DATA ENGINE
    # =========================================================================
    add_h1("1. System Architecture & Dual-Path Design")
    doc.add_paragraph(
        "The automated trading engine operates on a Dual-Path Execution Model designed to capture both the immediate opening impulse at 09:15:00 and sustained institutional accumulation starting at 09:18:00 when the exchange releases fresh Open Interest (OI) feeds."
    )
    add_bullet("Path 1 (09:15:00 First-Tick Entry):", "Executes immediately on market open for instruments identified on yesterday's 3:15 PM EOD Watchlist (ranked ★★★ or above). Requires spot gap alignment (<= 0.3%) and triggers on the very first positive tick above the stored reference LTP.")
    add_bullet("Path 2 (09:18:00 All-Day OI Buildup Matrix):", "Runs from the first fresh OI broadcast through 14:45:00. Evaluates the 16-condition option buildup matrix combined with a strict Futures Direction Gate and consecutive confirmation filters.")
    add_bullet("Slippage & Fill Reality:", "All paper and live entries are filled strictly at the live Ask price; all exits are executed strictly at the live Bid price. Fills are never simulated at previous close, midpoint, or LTP.")
    add_bullet("Multi-Index & Stock Isolation:", "Each instrument (NIFTY, BANK NIFTY, SENSEX, and each screened stock) runs an independent state machine with dedicated cache, risk limits, and trade counters.")

    # =========================================================================
    # SECTION 2: INSTRUMENTS & CONTRACT LOCKING
    # =========================================================================
    add_h1("2. Instruments & Contract Locking Mechanics")
    doc.add_paragraph(
        "The bot monitors three major equity indices and the liquid NIFTY 50 stock options universe. At 09:14:55, exactly 8 ITM/ATM option contracts (4 Calls and 4 Puts) are locked per instrument around the prevailing spot price."
    )

    inst_table = doc.add_table(rows=1, cols=5)
    inst_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    inst_widths = [Inches(1.5), Inches(1.2), Inches(1.0), Inches(1.4), Inches(1.7)]
    style_table_headers(inst_table, inst_widths, ["Instrument", "Exchange Key", "Lot Size", "Strike Step", "Futures Gate Filter"])
    inst_data = [
        ["NIFTY 50", "NSE_INDEX|Nifty 50", "25 (or 75)", "50 pts (ATM ± 1)", "Active (LB/SC for CE, SB/LU for PE)"],
        ["BANK NIFTY", "NSE_INDEX|Nifty Bank", "15 (or 30)", "100 pts (ATM ± 1)", "Active (LB/SC for CE, SB/LU for PE)"],
        ["SENSEX", "BSE_INDEX|SENSEX", "10 (or 20)", "100 pts (ATM ± 1)", "Disabled (Default OFF; ★★★ Only)"],
        ["NIFTY 50 Stocks", "NSE_EQ|<Symbol>", "Stock Specific", "Symbol Specific", "Active (LB/SC for CE, SB/LU for PE)"],
    ]
    populate_table_rows(inst_table, inst_widths, inst_data, align_cols={1: WD_ALIGN_PARAGRAPH.CENTER, 2: WD_ALIGN_PARAGRAPH.CENTER})
    doc.add_paragraph().paragraph_format.space_after = Pt(4)

    add_h3("Tightest-Spread ITM Strike Selection")
    doc.add_paragraph(
        "Rather than arbitrarily selecting an arbitrary strike, the bot evaluates the 4 locked In-The-Money (ITM) candidates and selects the contract possessing the tightest bid-ask spread %: Spread % = ((Ask - Bid) / Ask) * 100. This guarantees maximum liquidity, tightest fills, and lowest execution friction."
    )

    # =========================================================================
    # SECTION 3: DAILY TIMELINE & BASELINE SNAPSHOTS
    # =========================================================================
    add_h1("3. Daily Execution Timeline & Milestones")
    doc.add_paragraph("The bot operates on a strict, schedule-driven state machine synchronized to the Indian Standard Time (IST) exchange clock:")

    timeline_table = doc.add_table(rows=1, cols=4)
    timeline_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    time_widths = [Inches(1.2), Inches(1.3), Inches(1.4), Inches(2.9)]
    style_table_headers(timeline_table, time_widths, ["Time (IST)", "Phase / Event", "State Transition", "Action & Rules Enforced"])
    timeline_data = [
        ["09:00:00", "Pre-Market Open", "SETUP / PLANNING", "Freeze user settings; dynamic ATM contract planning begins around spot; verify prev close."],
        ["09:00 – 09:14", "Sanity Watchdog", "PLANNING", "Pre-open anomaly check: If LTP != Prev Close before 09:15, trigger PRE_OPEN_ANOMALY alert."],
        ["09:14:55", "Strike Lock", "LOCKED", "Lock 8 contracts (4 CE + 4 PE) for indices and stocks. In-memory hot cache initialized."],
        ["09:15:00", "Market Open", "ARMED", "Take hard Baseline Snapshot of Futures & Options (LTP, OI, Volume). Arm First-Tick strategy."],
        ["09:15 – 09:45", "Path 1 Window", "ARMED -> HOLDING", "First positive tick entry triggered. If neither SL nor Target hit by 09:45, square off."],
        ["09:18:00", "Path 2 Window", "ACTIVE EVAL", "Fresh OI stream arrives. Begin 16-condition matrix evaluation with 2-consecutive rule."],
        ["14:45:00", "No-Entry Cutoff", "NO_ENTRY", "Zero fresh entries permitted. Existing positions managed until exit or square-off."],
        ["15:10:00", "Square-off Notice", "WARNING", "System broadcast 5-minute warning alert before final automatic square-off."],
        ["15:15:00", "EOD Clock Square-off", "CLOSED", "Mandatory hard square-off of all open positions at live market bid. Reset daily states."],
        ["15:15 – 15:20", "EOD Screener", "SCREENING", "3:15 PM Ranking runs on all NIFTY 50 stocks & indices to build Tomorrow Watchlist."],
        ["15:20 – 15:25", "Optional BTST", "BTST_WINDOW", "Optional overnight BTST entry for ★★★+ ranked setups if enabled in Config."],
    ]
    populate_table_rows(timeline_table, time_widths, timeline_data, align_cols={0: WD_ALIGN_PARAGRAPH.CENTER, 2: WD_ALIGN_PARAGRAPH.CENTER})
    doc.add_paragraph().paragraph_format.space_after = Pt(4)

    # =========================================================================
    # SECTION 4: ENTRY MECHANISM 1 - 09:15 FIRST-TICK STRATEGY
    # =========================================================================
    add_h1("4. Entry Mechanism 1: 09:15 First-Tick Strategy (Path 1)")
    doc.add_paragraph(
        "Path 1 captures the opening impulse immediately at 09:15:00 without waiting for OI updates. It requires pre-qualification from yesterday's 3:15 PM EOD Watchlist."
    )
    add_bullet("Watchlist Gate:", "Only instruments ranked ★★★ or ★★★★ on yesterday's 3:15 PM Watchlist are permitted to enter at 09:15:00.")
    add_bullet("The 0.3% Gap Filter:", "If the index or stock opens gapped AGAINST yesterday's watchlist direction by more than 0.3%, the trade is aborted immediately. For Bullish watchlist, gap down cannot exceed -0.3%; for Bearish watchlist, gap up cannot exceed +0.3%.")
    add_bullet("Bullish Entry Trigger (BUY_CE):", "Spot opens at or above previous close. On the very first tick received after 09:15:00 where contract LTP > Reference LTP (Snapshot / Prev Close) on the tightest-spread ITM CE, enter BUY_CE at live Ask.")
    add_bullet("Bearish Entry Trigger (BUY_PE):", "Spot opens at or below previous close. On the very first tick received after 09:15:00 where contract LTP > Reference LTP on the tightest-spread ITM PE (or Stock spot ticks below open price), enter BUY_PE at live Ask.")
    add_bullet("Strict Daily Limit:", "Exactly 1 First-Tick entry is allowed per instrument per day. Subsequent entries must originate from Path 2.")
    add_bullet("09:45 Time Cutoff:", "If the 09:15 entry reaches neither the 25% Stop-Loss nor the 40% Target by 09:45:00, the bot forcefully exits the position at the live market bid.")

    # =========================================================================
    # SECTION 5: ENTRY MECHANISM 2 - 09:18 OI BUILDUP MATRIX
    # =========================================================================
    add_h1("5. Entry Mechanism 2: 09:18 OI Buildup Strategy (Path 2)")
    doc.add_paragraph(
        "Starting at 09:18:00, the exchange broadcasts fresh intraday Open Interest. Every incoming OI cycle triggers a complete re-evaluation of the ATM ±1 option chain against the 09:15:00 Baseline Snapshot."
    )

    add_h2("Buildup Classification Rules")
    doc.add_paragraph(
        "To eliminate false signals from market micro-noise, the bot applies a strict ±0.5% Price Deadband and a ±0.1% OI Threshold:"
    )
    add_bullet("Long Buildup (LB):", "Price Change >= +0.5% AND OI Change >= +0.1% (Bullish buyers accumulating contracts).")
    add_bullet("Short Buildup (SB):", "Price Change <= -0.5% AND OI Change >= +0.1% (Bearish writers adding short exposure).")
    add_bullet("Short Covering (SC):", "Price Change >= +0.5% AND OI Change <= -0.1% (Sellers forced to cover; explosive upside).")
    add_bullet("Long Unwinding (LU):", "Price Change <= -0.5% AND OI Change <= -0.1% (Bulls closing out long positions; downward drift).")
    add_bullet("FLAT / Deadband:", "Any price movement between -0.5% and +0.5% is treated as FLAT to prevent false triggers during sideways chop.")

    add_h2("The 16-Condition Option Chain Table")
    doc.add_paragraph(
        "The relative buildup on the Call Option (CE) vs Put Option (PE) generates one of 16 definitive market states:"
    )

    cond_table = doc.add_table(rows=1, cols=6)
    cond_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    cond_widths = [Inches(0.6), Inches(0.8), Inches(0.8), Inches(1.1), Inches(1.5), Inches(2.0)]
    style_table_headers(cond_table, cond_widths, ["No.", "CE Side", "PE Side", "Signal", "Rank / Lot Sizing", "Market Rationale"])
    cond_data = [
        ["1", "SC", "SB", "CE Buy", "★★★ (Full Lots)", "CE short covering + PE short writing (Strongest Bullish)"],
        ["2", "LB", "SB", "CE Buy", "★★ (Normal Lots)*", "CE aggressive buying + PE short writing (Bullish trend)"],
        ["3", "LB", "LU", "CE Buy", "★★ (Normal Lots)", "CE long buildup + PE long unwinding (Steady upward drift)"],
        ["4", "SC", "LU", "CE Buy", "★★ (Scalp / Half)", "CE short covering + PE unwinding (Quick momentum scalp)"],
        ["5", "SB", "SC", "PE Buy", "★★★ (Full Lots)", "CE short writing + PE short covering (Strongest Bearish)"],
        ["6", "SB", "LB", "PE Buy", "★★ (Normal Lots)*", "CE short writing + PE aggressive buying (Bearish trend)"],
        ["7", "LU", "LB", "PE Buy", "★★ (Normal Lots)", "CE long unwinding + PE long buildup (Steady downward drift)"],
        ["8", "LU", "SC", "PE Buy", "★★ (Scalp / Half)", "CE long unwinding + PE short covering (Quick momentum scalp)"],
        ["9", "LB", "LB", "No Trade", "Blocked", "Both premiums rising; IV spike or straddle buying"],
        ["10", "LB", "SC", "No Trade", "Blocked", "Both premiums rising; directional conflict"],
        ["11", "SC", "LB", "No Trade", "Blocked", "Both premiums rising; conflicting buyer flow"],
        ["12", "SC", "SC", "No Trade", "Blocked", "Both premiums rising; short panic on both sides"],
        ["13", "SB", "SB", "No Trade", "Blocked", "Writers aggressive on both sides; range-bound chop"],
        ["14", "SB", "LU", "No Trade", "Blocked", "Both premiums falling; directional decay"],
        ["15", "LU", "SB", "No Trade", "Blocked", "Both premiums falling; directional conflict"],
        ["16", "LU", "LU", "No Trade", "Blocked", "Both premiums collapsing; dead or illiquid market"],
    ]
    populate_table_rows(cond_table, cond_widths, cond_data, align_cols={0: WD_ALIGN_PARAGRAPH.CENTER, 1: WD_ALIGN_PARAGRAPH.CENTER, 2: WD_ALIGN_PARAGRAPH.CENTER, 3: WD_ALIGN_PARAGRAPH.CENTER})
    doc.add_paragraph().paragraph_format.space_after = Pt(4)

    add_callout(
        "*DYNAMIC UPGRADE RULE: Condition 2 upgrades to ★★★ Full Lots if PE OI added is >= 2x CE OI at ATM, volume spikes, and spot > VWAP. Mirror rule: Condition 6 upgrades to ★★★ Full Lots if CE OI added is >= 2x PE OI, volume spikes, and spot < VWAP.",
        title="INSTITUTIONAL VOLUME & OI UPGRADE FILTER",
        kind="tip"
    )

    add_h2("The 2 Consecutive OI Updates Requirement")
    doc.add_paragraph(
        "A single OI spike can be caused by temporary order placing or cancellations. To ensure institutional commitment, a valid signal must hold across 2 consecutive OI updates from the exchange before any order is sent to the market. If the signal flips or drops on the second update, the entry is canceled."
    )

    # =========================================================================
    # SECTION 6: THE FUTURES DIRECTION GATE & SENSEX SPECIAL RULES
    # =========================================================================
    add_h1("6. The Futures Direction Gate & SENSEX Protocol")
    doc.add_paragraph(
        "Option chains reveal where writers are active, but futures OI provides the purest read of institutional directional conviction without Greek distortions (theta decay and implied volatility spikes)."
    )

    add_h2("The Directional Gate Mechanism")
    doc.add_paragraph("Before executing any trade generated by the 16-condition matrix, the bot queries the underlying current-month futures contract:")
    add_bullet("CE Buy Validation:", "Futures MUST be in Long Buildup (LB) or Short Covering (SC). If Futures is in Short Buildup (SB) or Long Unwinding (LU), the trade is BLOCKED immediately with reason: Opposing Futures.")
    add_bullet("PE Buy Validation:", "Futures MUST be in Short Buildup (SB) or Long Unwinding (LU). If Futures is in Long Buildup (LB) or Short Covering (SC), the trade is BLOCKED immediately with reason: Opposing Futures.")

    add_h3("Signal Sizing Matrix (Option Rank + Futures Buildup)")
    sizing_table = doc.add_table(rows=1, cols=4)
    sizing_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    sz_widths = [Inches(1.5), Inches(1.5), Inches(1.5), Inches(2.3)]
    style_table_headers(sizing_table, sz_widths, ["Option Rank", "Futures Buildup", "Final Combined Rank", "Lot Allocation"])
    sz_data = [
        ["★★★", "LB / SB (Trend Confirm)", "★★★★", "Full Lots (lots_full)"],
        ["★★★", "SC / LU (Covering Drift)", "★★★", "Full Lots (lots_full)"],
        ["★★", "LB / SB (Trend Confirm)", "★★★", "Normal Lots (lots_normal)"],
        ["★★", "SC / LU (Covering Drift)", "★★", "Half Lots / Scalp (lots_scalp)"],
    ]
    populate_table_rows(sizing_table, sz_widths, sz_data, align_cols={0: WD_ALIGN_PARAGRAPH.CENTER, 1: WD_ALIGN_PARAGRAPH.CENTER, 2: WD_ALIGN_PARAGRAPH.CENTER})
    doc.add_paragraph().paragraph_format.space_after = Pt(4)

    add_h2("SENSEX Special Protocol")
    add_callout(
        "BSE SENSEX Futures order books exhibit wider bid-ask spreads and thinner liquidity than NSE Nifty futures. Therefore: (1) SENSEX Futures Gate is turned OFF by default in Config, and (2) Only ★★★ signals (Conditions 1 & 5) are permitted to enter. Lower ★★ signals are completely blocked for SENSEX.",
        title="SENSEX LIQUIDITY SAFEGUARD",
        kind="warning"
    )

    # =========================================================================
    # SECTION 7: POSITION SIZING & CONFIGURABLE LOTS
    # =========================================================================
    add_h1("7. Position Sizing & User Lot Configuration")
    doc.add_paragraph(
        "The bot enforces strict dynamic lot allocation tied directly to the Final Combined Rank. All lot quantities are user-configurable via the settings UI and API and are NEVER hardcoded:"
    )
    add_bullet("Rank ★★★★ (Maximum Confluence):", "Trades lots_full (Default: 1 lot; e.g., 25 qty Nifty, 15 qty Bank Nifty).")
    add_bullet("Rank ★★★ (High Confluence):", "Trades lots_full (Default: 1 lot).")
    add_bullet("Rank ★★ (Normal Confluence):", "Trades lots_normal (Default: 1 lot).")
    add_bullet("Rank ★★ (Scalp / Partial Confluence):", "Trades lots_scalp (Default: 1 lot).")
    add_bullet("Independent Controls:", "Indices and Stocks maintain completely independent lot multipliers and enabled toggles in Config.")
    add_bullet("Buyer / Seller Mode:", "When Buyer mode is enabled, the bot purchases Call/Put options. In Seller mode, it sells the corresponding options for theta decay credit.")

    # =========================================================================
    # SECTION 8: COMPREHENSIVE EXIT RULES & EXECUTION PROTOCOL
    # =========================================================================
    add_h1("8. Comprehensive Multi-Factor Exit Protocol")
    doc.add_paragraph(
        "The bot executes exits with zero hesitation. All exits are executed strictly at the Live Market Bid (never at entry price or midpoint). An open position is continuously monitored against 7 deterministic exit triggers:"
    )

    exit_table = doc.add_table(rows=1, cols=4)
    exit_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    exit_widths = [Inches(1.5), Inches(1.5), Inches(1.3), Inches(2.5)]
    style_table_headers(exit_table, exit_widths, ["Exit Trigger", "Mathematical Condition", "Exit Price", "Action & Engine Reason"])
    exit_data = [
        ["1. Stop-Loss (25%)", "Live Bid <= 0.75 * Entry Price", "Live Market Bid", "Hard stop triggered. Reason: STOP_LOSS_25PCT."],
        ["2. Target (40%)", "Live Bid >= 1.40 * Entry Price", "Live Market Bid", "Take-profit locked. Reason: TARGET_40PCT."],
        ["3. 09:45 Time Cutoff", "Clock Time >= 09:45:00", "Live Market Bid", "Applies to 09:15 Path 1 trades. Reason: TIME_CUTOFF_0945."],
        ["4. Reversal Signal", "Opposing 16-condition trigger", "Live Market Bid", "Option matrix flips direction. Reason: REVERSAL_SIGNAL."],
        ["5. Futures Flip", "Futures flips opposite to trade", "Live Market Bid", "Underlying futures reverses trend. Reason: FUTURES_FLIP."],
        ["6. 15:15 Clock EOD", "Clock Time >= 15:15:00", "Live Market Bid", "Mandatory end-of-day square-off. Reason: SQUAREOFF_1515."],
        ["7. Manual Exit", "User clicks 'Exit Position'", "Live Market Bid", "Interactive operator square-off. Reason: MANUAL_EXIT."],
    ]
    populate_table_rows(exit_table, exit_widths, exit_data, align_cols={1: WD_ALIGN_PARAGRAPH.CENTER, 2: WD_ALIGN_PARAGRAPH.CENTER})
    doc.add_paragraph().paragraph_format.space_after = Pt(4)

    add_callout(
        "CRITICAL EXECUTION RULE: Under no circumstances does the engine exit at the entry price, previous close, or theoretical mid-price. Every exit order is filled at the prevailing Live Bid price to capture real-world slippage and preserve accounting veracity.",
        title="LIVE BID FILL DISCIPLINE",
        kind="caution"
    )

    # =========================================================================
    # SECTION 9: RISK MANAGEMENT & OPERATIONAL SAFEGUARDS
    # =========================================================================
    add_h1("9. Risk Management Framework & Circuit Breakers")
    doc.add_paragraph(
        "Risk management is prioritized over profit generation. The bot features layered circuit breakers designed to prevent overtrading and catastrophic drawdown:"
    )
    add_bullet("Max 3 Trades Per Day:", "Raised from 1 to 3 trades per instrument per day. Once an index or stock reaches 3 completed trades, its state transitions to CLOSED and no further orders are accepted.")
    add_bullet("Daily Loss Limit (₹10,000 Default):", "The user specifies a maximum daily loss limit in rupees. If the aggregate net P&L (realized + unrealized) breaches this threshold, all active positions are liquidated immediately, and trading ceases for the day.")
    add_bullet("14:45:00 Hard No-Entry Cutoff:", "Strictly zero new trades are permitted after 14:45:00 IST to prevent holding high-theta intraday options into market close.")
    add_bullet("15:10:00 Square-Off Warning:", "A high-priority warning alert is broadcast across the system 5 minutes before the mandatory 15:15:00 clock square-off.")
    add_bullet("Stale Data Watchdog (> 60 Seconds):", "If the market data feed drops or fails to deliver ticks for > 60 seconds, a STALE_FEED alert is triggered, and new entries are blocked until feed health is restored.")
    add_bullet("Feed Disconnection & Recovery:", "If the WebSocket stream disconnects, orders are paused and contracts are re-verified upon reconnection.")

    # =========================================================================
    # SECTION 10: 3:15 PM EOD WATCHLIST & BTST ENGINE
    # =========================================================================
    add_h1("10. 3:15 PM EOD Watchlist & BTST Engine")
    doc.add_paragraph(
        "At 3:15 PM daily, the bot executes an automated screening algorithm across all NIFTY 50 stocks and major indices to construct the Tomorrow Watchlist."
    )

    add_h2("The 5 Screening Criteria")
    add_bullet("1. Futures Buildup Confluence:", "Compares current futures price/OI to previous day close. LB/SC classified as Bullish; SB/LU classified as Bearish.")
    add_bullet("2. Absolute OI Delta Threshold:", "Absolute OI change from 09:15 to 15:15 must be at least +3% for LB/SB, or negative for SC/LU.")
    add_bullet("3. Close Location Value (CLV):", "CLV = (LTP - Low) / (High - Low). Must be >= 0.85 for Bullish (closing near day high) or <= 0.15 for Bearish (closing near day low).")
    add_bullet("4. Last-Hour Momentum Confluence:", "Between 14:15 and 15:15, both futures price and OI must move in the same direction.")
    add_bullet("5. Relative Volume (RVOL):", "Futures volume in the final hour must be at least 1.5x the average final-hour volume over the last 20 sessions.")

    add_h2("Watchlist Output & Optional BTST Entry")
    doc.add_paragraph(
        "The screener ranks candidates into ★★★★, ★★★, and ★★ tiers and outputs the Top 5 Bullish and Top 5 Bearish stocks to selected_stocks.json. If the BTST toggle is enabled in Config, the bot enters an overnight position between 15:20 and 15:25 PM for candidates ranked ★★★ or higher."
    )

    # =========================================================================
    # SECTION 11: SIGNAL AUDIT LOGGING & LIVE UI JOURNAL
    # =========================================================================
    add_h1("11. Signal Auditing, Logging & UI Journal")
    doc.add_paragraph(
        "Complete observability is built into every tier of the trading engine:"
    )
    add_bullet("Signal Audit Log:", "Logs every single signal generated (both TAKEN and BLOCKED) along with its timestamp, underlying, rank, 16-condition number, futures buildup, and block reason.")
    add_bullet("Per-Day Live Journal View:", "The WPF Trade Journal defaults to today's date ('today'), displaying only live intraday trades, entry/exit prices, net P&L, and duration without clutter from historical sessions.")
    add_bullet("Historical Date Filter:", "A date selector dropdown and '📅 Today' quick button allow operators to inspect any historical day's trade ledger on demand.")
    add_bullet("Alerts Log Console:", "Real-time visual stream of all system events including stale feed warnings, pre-open checks, entry triggers, SL/Target hits, and risk breaches.")

    # =========================================================================
    # SECTION 12: MASTER COMPARISON MATRIX (INDICES VS STOCKS)
    # =========================================================================
    add_h1("12. Master Comparison Matrix: Indices vs Stocks")
    doc.add_paragraph("Summary reference comparing rule execution across all trading instruments:")

    comp_table = doc.add_table(rows=1, cols=4)
    comp_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    comp_widths = [Inches(1.8), Inches(1.6), Inches(1.6), Inches(1.8)]
    style_table_headers(comp_table, comp_widths, ["Operational Rule", "NIFTY & BANK NIFTY", "SENSEX", "NIFTY 50 STOCKS"])
    comp_data = [
        ["09:15 First-Tick Entry", "Yes (★★★+ Watchlist)", "Yes (★★★+ Watchlist)", "Yes (Top 5 Watchlist)"],
        ["0.3% Gap Filter", "Active (Skip if > 0.3%)", "Active (Skip if > 0.3%)", "Active (Skip if > 0.3%)"],
        ["09:18 OI Matrix (16 Cond)", "Active (Cond 1 to 8)", "Active (Cond 1 & 5 Only)", "Active (Cond 1 to 8)"],
        ["Futures Gate Filter", "Active (LB/SC or SB/LU)", "Disabled (Default OFF)", "Active (LB/SC or SB/LU)"],
        ["2-Consecutive OI Updates", "Required (2 in a row)", "Required (2 in a row)", "Required (2 in a row)"],
        ["Stop-Loss %", "25% of Entry Premium", "25% of Entry Premium", "25% of Entry Premium"],
        ["Target Profit %", "40% of Entry Premium", "40% of Entry Premium", "40% of Entry Premium"],
        ["09:45 Time Cutoff", "Active for 09:15 trades", "Active for 09:15 trades", "Active for 09:15 trades"],
        ["Futures Flip Exit", "Active at Live Bid", "Disabled", "Active at Live Bid"],
        ["Reversal Signal Exit", "Active at Live Bid", "Active at Live Bid", "Active at Live Bid"],
        ["Max Trades Per Day", "3 trades per index", "3 trades per index", "3 trades per stock"],
        ["Daily Loss Limit", "Shared Global Limit", "Shared Global Limit", "Shared Global Limit"],
        ["14:45 No-Entry Cutoff", "Enforced (0 entries)", "Enforced (0 entries)", "Enforced (0 entries)"],
        ["15:15 EOD Square-off", "Enforced at Live Bid", "Enforced at Live Bid", "Enforced at Live Bid"],
        ["3:15 PM Screener", "Scored for Watchlist", "Scored for Watchlist", "Ranks Top 5 Bull / Bear"],
    ]
    populate_table_rows(comp_table, comp_widths, comp_data, align_cols={1: WD_ALIGN_PARAGRAPH.CENTER, 2: WD_ALIGN_PARAGRAPH.CENTER, 3: WD_ALIGN_PARAGRAPH.CENTER})
    doc.add_paragraph().paragraph_format.space_after = Pt(8)

    # Final Verification Note
    add_callout(
        "INCUBATION MANDATE: Run the complete trading system in Paper Mode for a minimum of 2 to 3 weeks. Conduct a weekly review of the Part 7 signal and trade logs to verify win-rate, real-world slippage, and optimal lot distribution across ranks before transitioning to live broker execution.",
        title="INCUBATION & LIVE READINESS",
        kind="caution"
    )

    doc.save(output_path)
    print(f"Successfully generated document at: {output_path}")

if __name__ == "__main__":
    out_file = r"d:\Salauddin-projects\algo_trading\Complete_Trading_Bot_Execution_and_Rules_Manual.docx"
    build_trading_rules_document(out_file)
