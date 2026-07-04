import monitor

# We manually simulate finding the actual Audited Q4 Results filing for Diffusion Engineers Limited
results_ann = {
    "title": "Diffusion Engineers Limited",
    "description": "Audited Financial Results of the Company for the quarter ended on 31st March, 2026",
    "link": "https://nsearchives.nseindia.com/corporate/DIFFUSION_16052026202904_Outcome16052026.pdf",
    "guid": "TEST_RUN_DIFFUSION_Q4"
}

# We also attach the Investor Presentation as a supplementary document
supplementary_anns = [
    {
        "title": "Diffusion Engineers Limited",
        "description": "Investor Presentation for the quarter ended on 31st March, 2026",
        "link": "https://nsearchives.nseindia.com/corporate/DIFFUSION_17052026161809_Investorpresentation17052026.pdf",
        "guid": "TEST_RUN_DIFFUSION_PRES"
    }
]

print("Starting manual test run for Diffusion Engineers...")
print("Downloading actual PDFs from NSE Archives, executing Gemini analysis, compiling report, and sending to Telegram...")

success = monitor.process_announcements_group(
    company="Diffusion Engineers Limited",
    results_ann=results_ann,
    supplementary_anns=supplementary_anns
)

if success:
    print("SUCCESS: Test alert and PDF report successfully posted to Telegram!")
else:
    print("FAILED: Check the logs above for details.")
