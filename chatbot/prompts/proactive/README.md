# Proactive prompts

`threshold_monitor/` owns both `rewrite_threshold_alert.txt` and
`daily_report_coach.txt`. They are loaded by
`chatbot.proactive.threshold_monitor`. STEPS notifications remain deterministic
and require no prompt. The daily-report system/user boundary is preserved;
these flows do not invoke the D2 Advisor or reactive graph.
