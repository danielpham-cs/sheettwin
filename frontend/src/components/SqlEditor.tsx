"use client";

import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap, type CompletionContext } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { sql, SQLite } from "@codemirror/lang-sql";
import { bracketMatching, HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { linter, lintGutter, type Diagnostic } from "@codemirror/lint";
import { Compartment, EditorState, Prec } from "@codemirror/state";
import { EditorView, highlightActiveLine, keymap, lineNumbers } from "@codemirror/view";
import { tags } from "@lezer/highlight";
import { useEffect, useImperativeHandle, useRef, type Ref } from "react";

export type SqlSchema = Record<string, string[]>; // table or view -> its columns
export type SqlEditorHandle = { insert: (text: string) => void };

type Props = {
  value: string;
  onChange: (value: string) => void;
  onRun: () => void;
  /** Compile the query on the server: null if it is valid, else SQLite's error message. */
  check: (query: string) => Promise<string | null>;
  /** Called with the current error (null when the query is valid), to show it outside the editor too. */
  onError: (message: string | null) => void;
  schema: SqlSchema;
  didYouMean: (word: string) => string;
  ref?: Ref<SqlEditorHandle>;
};

// Keywords a typo is compared against; shorter ones (AS, ON, BY…) would match too many aliases.
const KEYWORDS = [
  "SELECT", "FROM", "WHERE", "GROUP", "ORDER", "LIMIT", "OFFSET", "HAVING", "JOIN", "LEFT", "INNER", "OUTER", "CROSS",
  "UNION", "DISTINCT", "WITH", "RECURSIVE", "USING", "LIKE", "BETWEEN", "CASE", "WHEN", "THEN", "ELSE", "NULL",
  "EXISTS", "VALUES", "COUNT", "DESC", "EXCEPT", "INTERSECT", "NATURAL", "COALESCE", "IFNULL", "CAST",
];

function editDistance(a: string, b: string) {
  const d = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) d[0][j] = j;
  for (let i = 1; i <= a.length; i++)
    for (let j = 1; j <= b.length; j++)
      d[i][j] = Math.min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
  return d[a.length][b.length];
}

/** The closest candidate within a typo's reach (1 edit for short words, 2 for longer), or null. */
function closest(word: string, candidates: string[]) {
  const w = word.toUpperCase();
  let best: string | null = null, bestD = w.length >= 6 ? 3 : 2;
  for (const c of candidates) {
    const d = editDistance(w, c.toUpperCase());
    if (d > 0 && d < bestD) { best = c; bestD = d; }
  }
  return best;
}

type Token = { text: string; from: number; to: number; word: boolean };

/** Tokens outside string literals and comments, with their offsets. */
function tokenize(src: string): Token[] {
  const out: Token[] = [];
  const re = /'(?:[^']|'')*'?|--[^\n]*|\/\*[\s\S]*?(?:\*\/|$)|"(?:[^"]|"")*"?|`[^`]*`?|\[[^\]]*\]?|[A-Za-z_][\w$]*|\d+(?:\.\d*)?|\S/g;
  for (let m; (m = re.exec(src)); ) {
    const t = m[0];
    if (t.startsWith("'") || t.startsWith("--") || t.startsWith("/*")) continue;
    const word = /^[A-Za-z_]/.test(t);
    const text = /^["`[]/.test(t) ? t.slice(1, -1) : t; // quoted identifier -> its name
    out.push({ text, from: m.index, to: m.index + t.length, word });
  }
  return out;
}

/** Turn SQLite's message into a diagnostic on the token it is about, with a suggestion when one is close. */
export function locate(src: string, message: string, schema: SqlSchema, didYouMean: (w: string) => string): Diagnostic {
  const tokens = tokenize(src);
  const known = new Set([...Object.keys(schema), ...Object.values(schema).flat()].map((s) => s.toLowerCase()));
  const keywords = new Set(KEYWORDS.map((k) => k.toLowerCase()));
  const at = (t: Token, msg: string): Diagnostic => ({ from: t.from, to: t.to, severity: "error", message: msg });

  // A misspelled keyword: SQLite often blames the token after it ("FROM cells limt 10" -> near "10",
  // since limt reads as a table alias), so the typo is the more useful place to point at.
  const typo = (upto: number) => {
    for (const t of tokens) {
      if (t.from > upto) break;
      const w = t.text.toLowerCase();
      if (!t.word || keywords.has(w) || known.has(w)) continue;
      const k = closest(t.text, KEYWORDS);
      if (k) return at(t, `${message} — ${didYouMean(k)}`);
    }
    return null;
  };

  if (/^only SELECT/.test(message)) {
    const first = tokens[0];
    if (!first) return { from: 0, to: 0, severity: "error", message };
    const k = closest(first.text, ["SELECT", "WITH"]);
    return at(first, k ? `${message} — ${didYouMean(k)}` : message);
  }

  const near = message.match(/^near "(.*)": syntax error/);
  if (near) {
    const hit = tokens.find((t) => t.text === near[1]) ?? tokens.find((t) => t.text.toLowerCase() === near[1].toLowerCase());
    return typo(hit?.from ?? src.length) ?? (hit ? at(hit, message) : { from: src.length, to: src.length, severity: "error", message });
  }
  if (/^incomplete input/.test(message)) {
    const last = tokens.at(-1);
    return typo(src.length) ?? (last ? at(last, message) : { from: 0, to: 0, severity: "error", message });
  }

  const missing = message.match(/^no such (column|table|function): (.+)$/);
  if (missing) {
    const name = missing[2].split(".").pop()!;
    const hit = tokens.find((t) => t.text.toLowerCase() === name.toLowerCase());
    const pool = missing[1] === "table" ? Object.keys(schema) : missing[1] === "column" ? [...new Set(Object.values(schema).flat())] : [];
    const s = closest(name, pool);
    const msg = s ? `${message} — ${didYouMean(s)}` : message;
    if (hit) return at(hit, msg);
  }

  const line = src.indexOf("\n");
  return { from: 0, to: line < 0 ? src.length : line, severity: "error", message };
}

/** Column names anywhere (the SQL language completes them only after "table."), each with the tables that have it. */
function columnCompletions(schema: SqlSchema) {
  const tables = new Map<string, string[]>();
  for (const [table, cols] of Object.entries(schema)) for (const c of cols) tables.set(c, [...(tables.get(c) ?? []), table]);
  const options = [...tables].map(([label, ts]) => ({
    label, type: "property", boost: 1, detail: ts.length > 2 ? `${ts.slice(0, 2).join(", ")}, …` : ts.join(", "),
  }));
  return (ctx: CompletionContext) => {
    const word = ctx.matchBefore(/\w+/);
    if (!word && !ctx.explicit) return null;
    const from = word?.from ?? ctx.pos;
    if (ctx.state.sliceDoc(from - 1, from) === ".") return null; // qualified: left to the SQL language
    return { from, options, validFor: /^\w*$/ };
  };
}

/** The SQL language for a schema, with column completion added. */
const sqlFor = (schema: SqlSchema) => [
  sql({ dialect: SQLite, schema, upperCaseKeywords: true }),
  SQLite.language.data.of({ autocomplete: columnCompletions(schema) }),
];

const theme = EditorView.theme({
  "&": { height: "100%", backgroundColor: "transparent", color: "#f4f4f5", fontSize: "13px" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--font-mono, ui-monospace, monospace)", lineHeight: "1.65" },
  ".cm-content": { padding: "12px 0", caretColor: "#c4b5fd" },
  ".cm-cursor": { borderLeftColor: "#c4b5fd" },
  ".cm-gutters": { backgroundColor: "transparent", border: "none", color: "#52525b" },
  ".cm-activeLine": { backgroundColor: "rgba(255,255,255,0.025)" },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "#a1a1aa" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection": { backgroundColor: "rgba(139,92,246,0.3) !important" },
  ".cm-matchingBracket": { backgroundColor: "rgba(139,92,246,0.25)", outline: "none" },
  ".cm-tooltip": { backgroundColor: "#1b1b22", border: "1px solid rgba(255,255,255,0.1)", borderRadius: "8px", overflow: "hidden" },
  ".cm-tooltip-autocomplete > ul": { fontFamily: "var(--font-mono, ui-monospace, monospace)", fontSize: "12.5px", maxHeight: "16em" },
  ".cm-tooltip-autocomplete > ul > li": { padding: "2px 10px 2px 6px", color: "#d4d4d8" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { backgroundColor: "rgba(139,92,246,0.3)", color: "#fff" },
  ".cm-completionDetail": { color: "#71717a", fontStyle: "normal", marginLeft: "8px" },
  ".cm-completionMatchedText": { textDecoration: "none", color: "#c4b5fd" },
  ".cm-diagnostic": { padding: "4px 10px", fontSize: "12px" },
  ".cm-diagnostic-error": { borderLeft: "3px solid #fb7185", color: "#fecdd3" },
}, { dark: true });

const highlight = HighlightStyle.define([
  { tag: tags.keyword, color: "#c4b5fd", fontWeight: "500" },
  { tag: [tags.string, tags.special(tags.string)], color: "#6ee7b7" },
  { tag: tags.number, color: "#fcd34d" },
  { tag: tags.bool, color: "#fcd34d" },
  { tag: tags.null, color: "#fda4af" },
  { tag: [tags.lineComment, tags.blockComment], color: "#71717a", fontStyle: "italic" },
  { tag: [tags.operator, tags.punctuation, tags.paren], color: "#a1a1aa" },
  { tag: tags.typeName, color: "#67e8f9" },
]);

/** A SQL editor with schema-aware completion and SQLite's own errors marked as you type. */
export function SqlEditor({ value, onChange, onRun, check, onError, schema, didYouMean, ref }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const language = useRef(new Compartment());
  // Latest props for the editor's callbacks, which are created once.
  const props = useRef({ onChange, onRun, check, onError, schema, didYouMean });
  useEffect(() => { props.current = { onChange, onRun, check, onError, schema, didYouMean }; });

  useEffect(() => {
    const lint = linter(async (v) => {
      const text = v.state.doc.toString();
      const p = props.current;
      if (!text.trim()) { p.onError(null); return []; }
      const error = await p.check(text).catch(() => null);
      if (v.state.doc.toString() !== text) return []; // edited meanwhile: the next run will report
      p.onError(error);
      return error ? [locate(text, error, p.schema, p.didYouMean)] : [];
    }, { delay: 450 });

    view.current = new EditorView({
      parent: host.current!,
      state: EditorState.create({
        doc: value,
        extensions: [
          lineNumbers(), highlightActiveLine(), history(), bracketMatching(), closeBrackets(),
          autocompletion({ icons: false }),
          language.current.of(sqlFor(props.current.schema)),
          syntaxHighlighting(highlight), theme, lint, lintGutter(),
          Prec.highest(keymap.of([{ key: "Mod-Enter", run: () => { props.current.onRun(); return true; } }])),
          keymap.of([...closeBracketsKeymap, ...completionKeymap, ...defaultKeymap, ...historyKeymap, indentWithTab]),
          EditorView.updateListener.of((u) => { if (u.docChanged) props.current.onChange(u.state.doc.toString()); }),
        ],
      }),
    });
    return () => { view.current?.destroy(); view.current = null; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- created once; later values arrive through the effects below
  }, []);

  // Text set from outside (an example query): replace the document unless it already matches.
  useEffect(() => {
    const v = view.current;
    if (v && v.state.doc.toString() !== value) v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value } });
  }, [value]);

  useEffect(() => {
    view.current?.dispatch({ effects: language.current.reconfigure(sqlFor(schema)) });
  }, [schema]);

  useImperativeHandle(ref, () => ({
    insert(text: string) {
      const v = view.current;
      if (!v) return;
      v.dispatch(v.state.replaceSelection(text));
      v.focus();
    },
  }), []);

  return <div ref={host} className="h-44 resize-y overflow-hidden" />;
}
