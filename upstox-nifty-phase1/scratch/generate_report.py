import docx
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

def create_report(filename):
    doc = docx.Document()

    # Set 1-inch margins
    sections = doc.sections
    for section in sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)

    # Helper function to add headings in simple black text
    def add_custom_heading(text, level):
        h = doc.add_heading(text, level=level)
        h.paragraph_format.space_before = Pt(14)
        h.paragraph_format.space_after = Pt(4)
        h.paragraph_format.keep_with_next = True
        for run in h.runs:
            run.font.name = "Arial"
            run.font.color.rgb = RGBColor(0, 0, 0)
        return h

    # Helper function for regular paragraphs
    def add_para(text="", bold_prefix=None, space_after=4):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(space_after)
        p.paragraph_format.line_spacing = 1.15
        if bold_prefix:
            run_b = p.add_run(bold_prefix)
            run_b.bold = True
            run_b.font.name = "Arial"
            run_b.font.size = Pt(10.5)
            run_b.font.color.rgb = RGBColor(0, 0, 0)
        if text:
            run_t = p.add_run(text)
            run_t.font.name = "Arial"
            run_t.font.size = Pt(10.5)
            run_t.font.color.rgb = RGBColor(0, 0, 0)
        return p

    # Helper for bullet points
    def add_bullet(bold_prefix, text):
        p = doc.add_paragraph(style='List Bullet')
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.line_spacing = 1.15
        if bold_prefix:
            run_b = p.add_run(bold_prefix)
            run_b.bold = True
            run_b.font.name = "Arial"
            run_b.font.size = Pt(10.5)
            run_b.font.color.rgb = RGBColor(0, 0, 0)
        if text:
            run_t = p.add_run(text)
            run_t.font.name = "Arial"
            run_t.font.size = Pt(10.5)
            run_t.font.color.rgb = RGBColor(0, 0, 0)
        return p

    # Title
    title_p = doc.add_paragraph()
    title_p.paragraph_format.space_after = Pt(2)
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run_title = title_p.add_run("ALGO TRADING EXECUTION REPORT")
    run_title.bold = True
    run_title.font.name = "Arial"
    run_title.font.size = Pt(16)
    run_title.font.color.rgb = RGBColor(0, 0, 0)

    sub_p = doc.add_paragraph()
    sub_p.paragraph_format.space_after = Pt(18)
    sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run_sub = sub_p.add_run("Today's Session Analysis & Trades Breakdown (12 November 2026)")
    run_sub.font.name = "Arial"
    run_sub.font.size = Pt(11)
    run_sub.font.color.rgb = RGBColor(80, 80, 80)

    # 1. Executive Summary
    add_custom_heading("1. Executive Summary", 1)
    add_para("This report outlines the complete algorithmic trading activity for the session of 12 November 2026. It details the core Open Interest (OI) buildup strategy, the automated morning trade execution, the subsequent trades triggered using the Arm Now feature across multiple indices, and the exact exit procedures applied.")
    add_bullet("Trading Date: ", "12 November 2026")
    add_bullet("Instruments Traded: ", "NIFTY 50 Options, BANK NIFTY Options, SENSEX Options")
    add_bullet("Total Trades Executed: ", "5 Trades (1 Automated Morning Trade + 4 Arm Now Trades)")
    add_bullet("Winning Trades: ", "2 Trades")
    add_bullet("Loss Trades: ", "3 Trades (controlled via strict risk/exit cuts)")
    add_bullet("Overall Session Net Profit: ", "+Rs 1,639.50 Net Profit")

    # 2. How the Strategy Decides and Executes Trades
    add_custom_heading("2. Strategy Mechanics: How Trades Are Decided and Executed", 1)
    add_para("The algorithm executes option buying strategies based on real-time Open Interest (OI) build-up combined with Price Action across 8 In-The-Money (ITM) option contracts.")
    add_para("The timeline and decision logic operate in five structured phases:")

    add_bullet("Phase 1 - 09:14:55 IST (Strike Selection & Lock): ", "The bot calculates the nearest spot price and identifies the 8 closest In-The-Money contracts (4 CE strikes below spot and 4 PE strikes above spot). These 8 contracts are locked into memory.")
    add_bullet("Phase 2 - 09:15:00 IST (Baseline Snapshot): ", "At the market opening bell, the bot records the exact baseline Last Traded Price (LTP) and Open Interest (OI) for all 8 locked contracts. No trading is permitted during this baseline capture.")
    add_bullet("Phase 3 - 09:18:00 IST (Armed & Evaluation Window): ", "From 09:18:00 onwards, the bot compares incoming live market ticks against the 09:15 baseline snapshot to compute Delta OI and Delta Price for both Call and Put sides.")
    add_bullet("Phase 4 - Buildup Classification (The 16-Rule Matrix): ", "The strategy evaluates the nature of institutional positioning:")
    add_para("   - Long Buildup (LB): Price UP and OI UP (Aggressive buying conviction)", space_after=2)
    add_para("   - Short Buildup (SB): Price DOWN and OI UP (Aggressive selling conviction)", space_after=2)
    add_para("   - Short Covering (SC): Price UP and OI DOWN (Sellers forced to cover)", space_after=2)
    add_para("   - Long Unwinding (LU): Price DOWN and OI DOWN (Buyers taking exits)", space_after=4)
    add_para("When Call side shows Long Buildup and Put side shows Short Buildup (e.g. Rule #1 and Rule #2), a CE Buy order is triggered. Conversely, when Call side shows Short Buildup and Put side shows Long Buildup (e.g. Rule #5, Rule #6, Rule #7), a PE Buy order is triggered. If prices or OI are flat, the bot stays in WAIT mode.")
    add_bullet("Phase 5 - Liquidity and Spread Filter: ", "Before placing the order, the bot verifies that the Bid-Ask spread of the contract is 3% or lower to prevent slippage. The order is then placed at the live market Ask price.")

    # 3. Detailed Trade Logs
    add_custom_heading("3. Detailed Log of Today's Executed Trades", 1)

    # Trade 1
    add_custom_heading("Trade 1: Automated Morning Opening Trade (NIFTY 50)", 2)
    add_bullet("Trade ID: ", "OIBU-1112091800-21")
    add_bullet("Underlying Index: ", "NSE NIFTY 50 (Spot: 25,242.85)")
    add_bullet("Option Contract: ", "NIFTY 25150 CE (17-Nov-2026 Expiry)")
    add_bullet("Quantity / Lots: ", "130 Quantity (2 Lots)")
    add_bullet("Trigger Rule: ", "Rule #2 [2-Star] (CE: Long Buildup, PE: Short Buildup -> CE Buy)")
    add_bullet("Entry Time: ", "09:18:00.583 IST (Executed right at the opening bell)")
    add_bullet("Entry Price: ", "Rs 261.25")
    add_bullet("Exit Time: ", "09:20:12.091 IST")
    add_bullet("Exit Price: ", "Rs 282.00")
    add_bullet("How Exited: ", "Target hit (+20.75 points gain). Executed at live market bid price.")
    add_bullet("Trade P&L: ", "+Rs 2,697.50 Net Profit")

    # Section explaining Bank Nifty & Sensex morning behavior
    add_custom_heading("Technical Note: Why Only NIFTY Traded at 09:18 AM", 2)
    add_para("During the 09:18 AM market open, NIFTY entered its trade immediately, but BANK NIFTY and SENSEX did not enter. The reasons were:")
    add_bullet("1. Scanner Scope: ", "The morning pre-market strike-locking script was configured to lock 8 strikes for NIFTY 50 only. Bank Nifty and Sensex contracts were in the database, but their live evaluation cycle had not been decoupled.")
    add_bullet("2. Global Daily Trade Cap: ", "The bot operated under a global rule: max_trades_per_day = 1. The moment NIFTY hit its target and closed at 09:20 AM, the bot marked the whole day as completed (State: DONE), blocking any subsequent entries across all indices.")
    add_para("Resolution: During today's session, the architecture was upgraded to '1 Trade Per Index'. Nifty, Bank Nifty, and Sensex were separated into independent state machines, and 'Arm Now' buttons were introduced to allow targeted re-entry.")

    # Trade 2
    add_custom_heading("Trade 2: Arm Now Entry (BSE SENSEX)", 2)
    add_bullet("Trade ID: ", "OIBU-1112102146-22")
    add_bullet("Underlying Index: ", "BSE SENSEX (Spot: 82,248.89)")
    add_bullet("Option Contract: ", "SENSEX 82200 PE (12-Nov-2026 Expiry)")
    add_bullet("Quantity / Lots: ", "20 Quantity (2 Lots)")
    add_bullet("Trigger Rule: ", "Rule #7 [2-Star] (CE: Long Unwinding, PE: Long Buildup -> PE Buy)")
    add_bullet("How Triggered: ", "User triggered 'Arm Now' for Sensex. Bot locked 8 ITM Sensex strikes, evaluated bearish institutional buildup, and executed PE Buy.")
    add_bullet("Entry Time: ", "10:21:46.307 IST")
    add_bullet("Entry Price: ", "Rs 194.00")
    add_bullet("Exit Time: ", "10:36:24.736 IST")
    add_bullet("Exit Price: ", "Rs 193.50")
    add_bullet("How Exited: ", "Square-off executed via 'Exit All Positions' button.")
    add_bullet("Trade P&L: ", "-0.50 points / -Rs 10.00 (Minimal frictional loss)")

    # Trade 3
    add_custom_heading("Trade 3: Arm Now Entry (NIFTY 50)", 2)
    add_bullet("Trade ID: ", "OIBU-1112102533-23")
    add_bullet("Underlying Index: ", "NSE NIFTY 50 (Spot: 25,348.30)")
    add_bullet("Option Contract: ", "NIFTY 25500 PE (17-Nov-2026 Expiry)")
    add_bullet("Quantity / Lots: ", "130 Quantity (2 Lots)")
    add_bullet("Trigger Rule: ", "Rule #6 [2-Star] (CE: Short Buildup, PE: Long Buildup -> PE Buy)")
    add_bullet("How Triggered: ", "User re-armed NIFTY 50. Bot detected fresh Put buying and Call selling buildup and placed a Put entry.")
    add_bullet("Entry Time: ", "10:25:33.880 IST")
    add_bullet("Entry Price: ", "Rs 279.70")
    add_bullet("Exit Time: ", "10:26:27.776 IST")
    add_bullet("Exit Price: ", "Rs 285.10")
    add_bullet("How Exited: ", "Quick scalp target reached (+5.40 points gain).")
    add_bullet("Trade P&L: ", "+Rs 702.00 Net Profit")

    # Trade 4
    add_custom_heading("Trade 4: Arm Now Entry (BANK NIFTY)", 2)
    add_bullet("Trade ID: ", "OIBU-1112102628-24")
    add_bullet("Underlying Index: ", "NSE BANK NIFTY (Spot: 61,328.15)")
    add_bullet("Option Contract: ", "BANKNIFTY 61100 CE (23-Nov-2026 Expiry)")
    add_bullet("Quantity / Lots: ", "30 Quantity (2 Lots)")
    add_bullet("Trigger Rule: ", "Rule #3 [2-Star] (CE: Long Buildup, PE: Long Unwinding -> CE Buy)")
    add_bullet("How Triggered: ", "User clicked 'Arm Now' for Bank Nifty. Bot scanned 8 ITM strikes, observed Put buyers unwinding and Call buying, and executed CE Buy.")
    add_bullet("Entry Time: ", "10:26:28.327 IST")
    add_bullet("Entry Price: ", "Rs 825.85")
    add_bullet("Exit Time: ", "10:36:23.803 IST")
    add_bullet("Exit Price: ", "Rs 797.85")
    add_bullet("How Exited: ", "Controlled square-off executed via 'Exit All Positions' button.")
    add_bullet("Trade P&L: ", "-28.00 points / -Rs 840.00")

    # Trade 5
    add_custom_heading("Trade 5: Arm Now Entry (NIFTY 50)", 2)
    add_bullet("Trade ID: ", "OIBU-1112102939-25")
    add_bullet("Underlying Index: ", "NSE NIFTY 50 (Spot: 25,327.40)")
    add_bullet("Option Contract: ", "NIFTY 25350 PE (17-Nov-2026 Expiry)")
    add_bullet("Quantity / Lots: ", "130 Quantity (2 Lots)")
    add_bullet("Trigger Rule: ", "Rule #6 [2-Star] (CE: Short Buildup, PE: Long Buildup -> PE Buy)")
    add_bullet("How Triggered: ", "User re-armed NIFTY 50 for a downward momentum continuation setup.")
    add_bullet("Entry Time: ", "10:29:39.716 IST")
    add_bullet("Entry Price: ", "Rs 205.15")
    add_bullet("Exit Time: ", "10:32:25.140 IST")
    add_bullet("Exit Price: ", "Rs 198.15")
    add_bullet("How Exited: ", "Risk management exit to prevent adverse drawdown.")
    add_bullet("Trade P&L: ", "-7.00 points / -Rs 910.00")

    # 4. Exit Mechanisms Explained
    add_custom_heading("4. Exit Mechanisms: How Trades Are Closed", 1)
    add_para("The algorithm manages exits through four primary methods:")
    add_bullet("1. Target Profit Exit (+20 Points): ", "When the option price reaches +20 points above entry, the trade is automatically squared off at the market bid price (as seen in Trade 1).")
    add_bullet("2. Stop Loss Protection (-30 Points): ", "If market direction reverses against the position, a strict stop-loss automatically cuts the trade at -30 points to protect capital.")
    add_bullet("3. Immediate Multi-Index Square-Off ('Exit All Positions'): ", "The user can click 'Exit All Positions' in the terminal. The confirmation prompt has been removed, allowing a zero-delay instantaneous exit across all open positions at live market bid prices.")
    add_bullet("4. Clock Square-Off (15:15:00 IST): ", "Any open trades remaining at 15:15:00 are automatically squared off to prevent overnight gap risk.")

    # 5. Summary Table
    add_custom_heading("5. Session Performance Summary", 1)
    add_bullet("Trade 1 (Nifty 25150 CE): ", "+20.75 pts | +Rs 2,697.50 Profit")
    add_bullet("Trade 2 (Sensex 82200 PE): ", "-0.50 pts | -Rs 10.00")
    add_bullet("Trade 3 (Nifty 25500 PE): ", "+5.40 pts | +Rs 702.00 Profit")
    add_bullet("Trade 4 (Bank Nifty 61100 CE): ", "-28.00 pts | -Rs 840.00")
    add_bullet("Trade 5 (Nifty 25350 PE): ", "-7.00 pts | -Rs 910.00")
    add_para("--------------------------------------------------------------------------------", space_after=2)
    add_bullet("TOTAL NET SESSION RESULT: ", "+Rs 1,639.50 Net Profit")

    doc.save(filename)
    print(f"Report saved to {filename}")

if __name__ == "__main__":
    create_report(r"D:\Salauddin-projects\algo_trading\Today_Trades_Execution_Report_12Nov2026.docx")

