// Landing page copy. `en` is typed as `typeof zh`, so both languages must have every key.
// Issue examples are real findings from the bundled sample workbook (`sheetir sample`).

const zh = {
  nav: { features: "功能", how: "運作方式", issues: "偵測問題", open: "開啟 App" },
  hero: {
    badge: "SheetIR v0.1 · 概念驗證",
    title1: "讓每一張 Excel 試算表",
    title2: "看得懂、查得到、信得過",
    sub: "SheetTwin 把企業試算表解析成可查詢的結構化模型，自動找出公式錯誤，並用對話、依賴圖和圖表解釋每一個數字是怎麼來的。",
    start: "開始使用",
    sample: "用範例試試看",
    chips: ["本機分析", "資料不外流", "不修改原檔", "確定性分析，結果可重現"],
  },
  why: {
    kicker: "為什麼需要它",
    title: "試算表撐起了報價、預算和決策，卻很少被檢查",
    items: [
      { title: "公式悄悄被改壞", body: "整欄往下拉的公式，中間有一格不一樣，結果就錯了，而且沒有人會發現。" },
      { title: "數字蓋掉了公式", body: "有人把算好的值直接打成常數，之後輸入改了，它也不會跟著變。" },
      { title: "常數藏在公式裡", body: "加成率、匯率寫死在幾十個公式中，要調整只能一格一格找。" },
      { title: "沒人知道怎麼算的", body: "人員交接之後，最終的報價是從哪些輸入算出來的，只能靠猜。" },
    ],
  },
  features: {
    kicker: "功能",
    title: "上傳一份活頁簿，馬上看懂它",
    items: {
      chat: {
        title: "用對話問活頁簿",
        body: "直接問「有哪些問題」、「參數有哪些」，或輸入儲存格位址，立刻得到它的公式、語意名稱和計算來源。",
        points: ["點擊回覆裡的儲存格就能繼續追溯", "中英文都能問、都能答"],
      },
      graph: {
        title: "依賴圖：每個數字從哪裡來",
        body: "把整份活頁簿的資料流畫成一張圖。整欄相同的公式合成一個節點，幾百個儲存格也能一眼看懂。",
        points: ["滑鼠移上去，整條計算鏈會亮起來", "有問題的節點直接標出來"],
      },
      charts: {
        title: "圖表：問題與資料一目了然",
        body: "儲存格角色、問題嚴重度和類型的分布，以及偵測到的表格資料，都能直接畫成圓餅圖和長條圖。",
        points: ["依文字欄位分組加總", "色盲友善、經過驗證的配色"],
      },
      exports: {
        title: "轉換成可再利用的格式",
        body: "解析結果可以下載成 SQLite 資料庫、JSON 語意模型和 CSV，交給後續的程式、分析或稽核使用。",
        points: ["直接在介面預覽 CSV 和 JSON", "SQLite 可以用 SQL 查詢整個模型"],
      },
    },
  },
  how: {
    kicker: "運作方式",
    title: "從試算表到可解釋的模型",
    steps: [
      { title: "上傳", body: "拖曳 .xlsx 檔案，原始檔案不會被修改。" },
      { title: "解析成四層結構", body: "由下而上建立模型，每一層都從下一層推導出來。" },
      { title: "稽核與自我檢查", body: "偵測問題，並用獨立的方法重新推導，確認解析結果正確。" },
      { title: "對話與視覺化", body: "用聊天、依賴圖、圖表理解它，或匯出給其他工具。" },
    ],
    layers: [
      { id: "L0", name: "原始資料", body: "儲存格、公式、Excel 快取值" },
      { id: "L1", name: "公式與參照", body: "解析參照，轉成與位置無關的 R1C1" },
      { id: "L2", name: "結構", body: "公式群組、表格、儲存格角色" },
      { id: "L3", name: "語意", body: "參數、欄位、輸出的名稱" },
    ],
    checks: ["每個公式都歸入一個群組", "用獨立實作重算每個群組", "沒有循環參照"],
  },
  issues: {
    kicker: "偵測問題",
    title: "它在範例報價單裡找到了這些",
    sub: "以下都是 SheetTwin 在內建範例報價單（4 個工作表、20 項產品）中實際偵測到的問題。",
    items: [
      { kind: "inconsistent_formula", severity: "high", title: "同一欄的公式不一致", cell: "Quote!I13", body: "這一列的加成是 1.12，同一欄其他 19 格都是 1.15。" },
      { kind: "hardcoded_in_formula_column", severity: "high", title: "公式被數字蓋掉", cell: "Quote!G17", body: "人工成本被直接打成 3250，其他列都是「工時 × 工資率」。" },
      { kind: "hardcoded_constant", severity: "medium", title: "寫死的常數", cell: "Quote!I2:I21", body: "加成率 1.15 寫死在 19 個公式裡，沒有放在參數表。" },
      { kind: "orphan", severity: "low", title: "沒有被使用的數值", cell: "Quote!E17", body: "因為 G17 被蓋掉，這一列的工時已經不影響任何結果。" },
      { kind: "volatile", severity: "medium", title: "每次開啟結果都不同", cell: "Report!B1", body: "=TODAY() 會讓報表在不同日期打開時得到不同的值。" },
      { kind: "dynamic_ref", severity: "medium", title: "無法追蹤的動態參照", cell: "Report!B6", body: "INDIRECT 的依賴關係只有在執行時才知道。" },
    ],
    more: "還能偵測：#REF! 錯誤、外部連結、循環參照、陣列公式、找不到的名稱等。",
  },
  cta: {
    title: "看看你的試算表裡藏了什麼",
    sub: "上傳一份活頁簿，幾秒內就能看到它的結構和問題。",
  },
  footer: { legacy: "結構瀏覽器", note: "SheetTwin · SheetIR 概念驗證 · 所有分析都在本機進行" },
  shotAlt: {
    chat: "聊天介面：列出範例報價單偵測到的問題",
    graph: "依賴圖：範例報價單的資料流總覽",
    charts: "圖表：儲存格角色與問題分布",
    exports: "轉換檔案面板：SQLite、JSON、CSV",
  },
};

const en: typeof zh = {
  nav: { features: "Features", how: "How it works", issues: "What it finds", open: "Open app" },
  hero: {
    badge: "SheetIR v0.1 · proof of concept",
    title1: "Make every spreadsheet",
    title2: "understandable, traceable, trustworthy",
    sub: "SheetTwin turns business spreadsheets into a queryable, structured model, finds formula errors automatically, and explains where every number comes from through chat, a dependency graph and charts.",
    start: "Get started",
    sample: "Try the sample",
    chips: ["Runs locally", "No data leaves your machine", "Original file untouched", "Deterministic, reproducible"],
  },
  why: {
    kicker: "Why it matters",
    title: "Spreadsheets drive quotes, budgets and decisions, yet are rarely checked",
    items: [
      { title: "Formulas break silently", body: "One cell in a filled-down column differs from the rest, the result is wrong, and nobody notices." },
      { title: "Numbers overwrite formulas", body: "Someone types a computed value in as a constant; when the inputs change, it doesn't." },
      { title: "Constants hide in formulas", body: "Markups and exchange rates are hard-coded in dozens of formulas and have to be found one by one." },
      { title: "Nobody knows how it works", body: "After a handover, how the final quote is calculated is anyone's guess." },
    ],
  },
  features: {
    kicker: "Features",
    title: "Upload a workbook, understand it right away",
    items: {
      chat: {
        title: "Ask your workbook",
        body: "Ask “what issues are there?” or “list the parameters”, or type a cell address to get its formula, semantic name and where it comes from.",
        points: ["Click a cell in a reply to keep tracing", "Ask and answer in English or Chinese"],
      },
      graph: {
        title: "Dependency graph: where every number comes from",
        body: "The whole workbook's data flow as one picture. A filled-down column becomes a single node, so hundreds of cells stay readable.",
        points: ["Hover a node to light up its calculation chain", "Nodes with issues are flagged"],
      },
      charts: {
        title: "Charts: issues and data at a glance",
        body: "Cell roles, issue severity and kinds, and the data in detected tables, as pie and bar charts.",
        points: ["Sum a column by any text column", "Colorblind-safe, validated palette"],
      },
      exports: {
        title: "Converted into reusable formats",
        body: "Download the parsed model as a SQLite database, a JSON semantic model and CSVs for downstream code, analysis or audits.",
        points: ["Preview CSV and JSON in the app", "Query the whole model with SQL"],
      },
    },
  },
  how: {
    kicker: "How it works",
    title: "From spreadsheet to explainable model",
    steps: [
      { title: "Upload", body: "Drop in an .xlsx file. The original is never modified." },
      { title: "Parse into four layers", body: "The model is built bottom-up; each layer is derived from the one below." },
      { title: "Audit and self-check", body: "Issues are detected, and an independent method re-derives the result to confirm it." },
      { title: "Chat and visualize", body: "Understand it through chat, the graph and charts, or export it to other tools." },
    ],
    layers: [
      { id: "L0", name: "Raw", body: "Cells, formulas, Excel's cached values" },
      { id: "L1", name: "Formulas", body: "Resolved references, position-independent R1C1" },
      { id: "L2", name: "Structure", body: "Formula groups, tables, cell roles" },
      { id: "L3", name: "Semantics", body: "Names for parameters, fields and outputs" },
    ],
    checks: ["Every formula belongs to a group", "Each group re-derived by an independent implementation", "No circular references"],
  },
  issues: {
    kicker: "What it finds",
    title: "What it found in the sample quotation",
    sub: "These are real findings from the bundled sample quotation workbook (4 sheets, 20 products).",
    items: [
      { kind: "inconsistent_formula", severity: "high", title: "Inconsistent formula in a column", cell: "Quote!I13", body: "This row's markup is 1.12; the other 19 cells in the column use 1.15." },
      { kind: "hardcoded_in_formula_column", severity: "high", title: "A number overwrote a formula", cell: "Quote!G17", body: "Labor cost was typed in as 3250; every other row computes hours × labor rate." },
      { kind: "hardcoded_constant", severity: "medium", title: "Hard-coded constant", cell: "Quote!I2:I21", body: "The 1.15 markup is hard-coded in 19 formulas instead of living in the parameters." },
      { kind: "orphan", severity: "low", title: "Value nothing uses", cell: "Quote!E17", body: "Because G17 was overwritten, this row's labor hours no longer affect any result." },
      { kind: "volatile", severity: "medium", title: "Different on every open", cell: "Report!B1", body: "=TODAY() makes the report give different values on different days." },
      { kind: "dynamic_ref", severity: "medium", title: "Untraceable dynamic reference", cell: "Report!B6", body: "INDIRECT's dependencies are only known at run time." },
    ],
    more: "Also detects: #REF! errors, external links, circular references, array formulas, unresolved names and more.",
  },
  cta: {
    title: "See what's hiding in your spreadsheets",
    sub: "Upload a workbook and see its structure and issues in seconds.",
  },
  footer: { legacy: "Structure browser", note: "SheetTwin · SheetIR proof of concept · all analysis runs locally" },
  shotAlt: {
    chat: "Chat: the issues found in the sample quotation",
    graph: "Dependency graph: overview of the sample quotation's data flow",
    charts: "Charts: cell roles and issue breakdown",
    exports: "Converted files panel: SQLite, JSON, CSV",
  },
};

export const LANDING = { zh, en };
export type LandingCopy = typeof zh;
