"""Clean stage (docs/excel-agent-system-design.md §6.4): a TableSpec and the IR's cells become one typed table, ready
for DuckDB: header levels flattened into SQL identifiers, merged cells opened, total, subtotal and note rows removed,
values typed from their number formats and contents, units found, and column sums checked against the total row."""
