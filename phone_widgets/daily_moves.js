// ============================================================
// DAILY MOVES WIDGET — Small
// Hits Railway /widget/data endpoint
// Shows daily % and $ gain per portfolio
// Dark background, green/red coloring
// ============================================================

const API_URL = "https://portracker-v2-production.up.railway.app/widget/data";

// ── Colors ───────────────────────────────────────────────────
const BG_COLOR        = new Color("#0d0d0d");
const WHITE           = new Color("#ffffff");
const LABEL_COLOR     = new Color("#aaaaaa");
const GREEN           = new Color("#00e676");
const RED             = new Color("#ff1744");
const NEUTRAL         = new Color("#ffffff");
const TIMESTAMP_COLOR = new Color("#555555");

// ── Portfolio display config ──────────────────────────────────
const PORTFOLIO_LABELS = {
  "individual": "Individual",
  "roth_ira":   "Roth IRA",
  "liquid_fund": "Liquid",
};

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

// ── Build widget ──────────────────────────────────────────────
async function buildWidget() {
  const data = await fetchData();

  const widget = new ListWidget();
  widget.backgroundColor = BG_COLOR;
  widget.setPadding(12, 14, 10, 14);

  // ── Header ────────────────────────────────────────────────
  const headerStack = widget.addStack();
  headerStack.layoutHorizontally();
  headerStack.centerAlignContent();

  const titleText = headerStack.addText("DAILY MOVES");
  titleText.font = Font.boldSystemFont(11);
  titleText.textColor = WHITE;

  headerStack.addSpacer();

  const marketIcon = headerStack.addText(
    data?.market_open ? "☀" : "☾"
  );
  marketIcon.font = Font.systemFont(11);

  widget.addSpacer(6);

  if (!data) {
    const errText = widget.addText("Unable to load data");
    errText.font = Font.systemFont(10);
    errText.textColor = LABEL_COLOR;
    return widget;
  }

  // ── Portfolio rows ─────────────────────────────────────────
  const portfolios = data.portfolios || {};
  const slugs = Object.keys(PORTFOLIO_LABELS);

  for (const slug of slugs) {
    const pf    = portfolios[slug];
    const label = PORTFOLIO_LABELS[slug];

    const row = widget.addStack();
    row.layoutHorizontally();
    row.centerAlignContent();

    const nameText = row.addText(label);
    nameText.font = Font.mediumSystemFont(10);
    nameText.textColor = LABEL_COLOR;
    nameText.lineLimit = 1;
    nameText.minimumScaleFactor = 0.7;

    row.addSpacer();

    const rightStack = row.addStack();
    rightStack.layoutVertically();
    rightStack.centerAlignContent();

    if (!pf || pf.daily_pct === null) {
      const naText = rightStack.addText("N/A");
      naText.font = Font.boldSystemFont(12);
      naText.textColor = NEUTRAL;
    } else {
      const pct      = pf.daily_pct;
      const gain     = pf.daily_gain;
      const color    = pct > 0 ? GREEN : pct < 0 ? RED : NEUTRAL;
      const sign     = pct >= 0 ? "+" : "";
      const gainSign = gain >= 0 ? "+" : "-";

      const pctText = rightStack.addText(`${sign}${pct.toFixed(2)}%`);
      pctText.font = Font.boldSystemFont(13);
      pctText.textColor = color;
      pctText.lineLimit = 1;

      const gainText = rightStack.addText(`${gainSign}$${Math.abs(gain).toFixed(0)}`);
      gainText.font = Font.systemFont(9);
      gainText.textColor = color;
      gainText.lineLimit = 1;
    }

    widget.addSpacer(4);
  }

  widget.addSpacer();

  // ── Timestamp ──────────────────────────────────────────────
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
  widget.presentSmall();
}

Script.complete();