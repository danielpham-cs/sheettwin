# SheetTwin chat UI

Next.js front end for SheetIR: chat with a parsed workbook, upload new workbooks and browse the files
converted from them. All data comes from the FastAPI backend.

| Command | What it does |
|---|---|
| `npm run build` | Static export to `out/`, then copied to `../sheetir/web/static/app`; `sheetir serve` serves the landing page at `/` and the app at `/app/` |
| `npm run screenshots` | Re-shoot `public/screenshots/` for the landing page (needs a running backend and an installed Edge/Chrome) |
| `npm run dev` | Dev server on http://localhost:3000 with hot reload; `/api/*` is proxied to the backend |

Both need the backend running (`uv run sheetir serve` from `sheettwin/`). Set `SHEETIR_API` if it is not on
`http://127.0.0.1:8765`. The UI runs entirely in the browser, so there is no `npm start`.

| Part | File |
|---|---|
| Landing page (`/`) and its copy | `src/components/Landing.tsx`, `src/lib/landing-i18n.ts` |
| Left rail: 新增資料 / 轉換檔案, workbook list | `src/components/Sidebar.tsx` |
| Upload dialog (drag and drop, sample workbook) | `src/components/UploadDialog.tsx` |
| Converted files panel with CSV/JSON preview and downloads | `src/components/ExportsPanel.tsx` |
| Chat (Markdown replies; clicking a cell id traces it) | `src/components/Chat.tsx` |
| State, chat history (localStorage, per workbook) | `src/components/App.tsx` |
| API client | `src/lib/api.ts` |

The chat backend (`sheetir/web/chat.py`) is rule-based for now: summary, issues, parameters, tables,
outputs, checks and cell lookups. It is the place to plug in the LLM Q&A stage later.
