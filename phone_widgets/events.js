// ============================================================
// EARNINGS + DIVIDENDS WIDGET — Large
// Left column:  Earnings this week (ticker, date, detail)
// Right column: Dividends this week (ticker, pay date, amount)
//               + Ex-dividend dates section below
// Hits Railway /widget/data endpoint
// ============================================================

const API_URL = "https://portracker-v2-production.up.railway.app/widget/data";

// ── Colors ───────────────────────────────────────────────────
const BG_COLOR        = new Color("#0d0d0d");
const WHITE           = new Color("#ffffff");
const LABEL_COLOR     = new Color("#aaaaaa");
const ACCENT_EARNINGS = new Color("#4fc3f7");  // sky blue
const ACCENT_DIVS     = new Color("#ce93d8");  // soft purple
const ACCENT_EXDIV    = new Color("#ffcc80");  // amber
const DIVIDER_COLOR   = new Color("#2a2a2a");
const TIMESTAMP_COLOR = new Color("#555555");
const GREEN           = new Color("#00e676");

// ── Fetch data ────────────────────────────────────────────────
async function fetchData() {
  try {
    const req = new Request(API_URL);
    req.timeoutInterval = 15;
    return await req.loadJSON();
  } catch (e) {
    return null;
  }
}

// ── Week label ────────────────────────────────────────────────
function getWeekLabel() {
  const now = new Date();
  const ct  = new Date(now.toLocaleString("en-US", { timeZone: "America/Chicago" }));
  const day = ct.getDay();
  const diffToMon = day === 0 ? -6 : 1 - day;
  const mon = new Date(ct);
  mon.setDate(ct.getDate() + diffToMon);
  const sun = new Date(mon);
  sun.setDate(mon.getDate() + 6);
  const fmt = (d) => d.toLocaleDateString("en-US", {
    month: "short", day: "numeric", timeZone: "America/Chicago"
  });
  return `${fmt(mon)} – ${fmt(sun)}`;
}

// ── Build a column ────────────────────────────────────────────
function buildColumn(parent, title, accentColor, items, renderRow) {
  const col = parent.addStack();
  col.layoutVertically();
  col.spacing = 0;

  const hdr = col.addText(title);
  hdr.font = Font.boldSystemFont(11);
  hdr.textColor = accentColor;
  hdr.lineLimit = 1;

  col.addSpacer(6);

  if (!items || items.length === 0) {
    const none = col.addText("None this week");
    none.font = Font.italicSystemFont(9);
    none.textColor = LABEL_COLOR;
    return;
  }

  for (const item of items) {
    renderRow(col, item);
    col.addSpacer(6);
  }
}

// ── Build widget ──────────────────────────────────────────────
async function buildWidget() {
  const data = await fetchData();

  const widget = new ListWidget();
  widget.backgroundColor = BG_COLOR;
  widget.setPadding(14, 14, 12, 14);

  // ── Title row ────────────────────────────────────────────
  const titleRow = widget.addStack();
  titleRow.layoutHorizontally();
  titleRow.centerAlignContent();

  const title = titleRow.addText("THIS WEEK");
  title.font = Font.boldSystemFont(12);
  title.textColor = WHITE;

  titleRow.addSpacer();

  const weekLabel = titleRow.addText(getWeekLabel());
  weekLabel.font = Font.systemFont(9);
  weekLabel.textColor = LABEL_COLOR;

  widget.addSpacer(10);

  if (!data) {
    const err = widget.addText("Unable to load data");
    err.font = Font.systemFont(10);
    err.textColor = LABEL_COLOR;
    return widget;
  }

  const events  = data.events || {};
  const earn    = events.earn  || [];
  const div     = events.div   || [];
  const exdiv   = events.exdiv || [];

  // ── Top section: Earnings + Dividends side by side ────────
  const body = widget.addStack();
  body.layoutHorizontally();
  body.spacing = 12;

  // LEFT: Earnings
  buildColumn(body, "📈  EARNINGS", ACCENT_EARNINGS, earn, (col, item) => {
    const ticker = col.addText(item.symbol);
    ticker.font = Font.boldSystemFont(12);
    ticker.textColor = WHITE;
    ticker.lineLimit = 1;

    const date = col.addText(item.date);
    date.font = Font.systemFont(9);
    date.textColor = LABEL_COLOR;
    date.lineLimit = 1;

    const detail = col.addText(item.detail);
    detail.font = Font.mediumSystemFont(9);
    detail.textColor = ACCENT_EARNINGS;
    detail.lineLimit = 1;
  });

  // Vertical divider
  const div1 = body.addStack();
  div1.backgroundColor = DIVIDER_COLOR;
  div1.size = new Size(1, 0);

  // RIGHT: Dividends (payment date + amount)
  buildColumn(body, "💰  DIVIDENDS", ACCENT_DIVS, div, (col, item) => {
    const ticker = col.addText(item.symbol);
    ticker.font = Font.boldSystemFont(12);
    ticker.textColor = WHITE;
    ticker.lineLimit = 1;

    const date = col.addText(item.date);
    date.font = Font.systemFont(9);
    date.textColor = LABEL_COLOR;
    date.lineLimit = 1;

    const detail = col.addText(item.detail);
    detail.font = Font.mediumSystemFont(9);
    detail.textColor = GREEN;
    detail.lineLimit = 1;
  });

  widget.addSpacer(10);

  // ── Horizontal divider ────────────────────────────────────
  const hdivStack = widget.addStack();
  hdivStack.backgroundColor = DIVIDER_COLOR;
  hdivStack.size = new Size(0, 1);

  widget.addSpacer(8);

  // ── Bottom section: Ex-dividend dates ────────────────────
  const exdivHeader = widget.addText("📅  EX-DIVIDEND DATES");
  exdivHeader.font = Font.boldSystemFont(10);
  exdivHeader.textColor = ACCENT_EXDIV;

  widget.addSpacer(6);

  if (exdiv.length === 0) {
    const none = widget.addText("No ex-dividend dates this week");
    none.font = Font.italicSystemFont(9);
    none.textColor = LABEL_COLOR;
  } else {
    for (const item of exdiv) {
      const row = widget.addStack();
      row.layoutHorizontally();
      row.centerAlignContent();
      row.spacing = 6;

      const ticker = row.addText(item.symbol);
      ticker.font = Font.boldSystemFont(11);
      ticker.textColor = WHITE;
      ticker.lineLimit = 1;

      const date = row.addText(item.date);
      date.font = Font.systemFont(9);
      date.textColor = LABEL_COLOR;
      date.lineLimit = 1;

      row.addSpacer();

      const detail = row.addText(item.detail);
      detail.font = Font.mediumSystemFont(9);
      detail.textColor = ACCENT_EXDIV;
      detail.lineLimit = 1;

      widget.addSpacer(4);
    }
  }

  widget.addSpacer();

  // ── Timestamp ─────────────────────────────────────────────
  const ts = widget.addText(data.updated || "—");
  ts.font = Font.systemFont(8);
  ts.textColor = TIMESTAMP_COLOR;

  return widget;
}

// ── Entry point ───────────────────────────────────────────────
const widget = await buildWidget();

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  widget.presentLarge();
}

Script.complete();