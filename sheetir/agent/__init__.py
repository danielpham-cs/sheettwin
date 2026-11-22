"""Query agent (docs/excel-agent-system-design.md §7): a loop in which the language model picks one tool per step
(search_catalog, describe_table, run_sql, make_chart, final_answer) as a JSON action, SheetIR runs it, and the answer is
checked before it is returned: every number in it must come from a query result."""
