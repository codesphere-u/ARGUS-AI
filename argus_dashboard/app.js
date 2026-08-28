// ARGUS AI: Autonomous Multi-Agent Trading Desk Orchestration & Interactive UI

// Sample Data Archive
const SAMPLE_PASSPORTS = [
  {
    id: "PASSPORT-20260828-NVDA01",
    ticker: "NVDA",
    class: "US_EQUITY",
    direction: "LONG",
    price: 148.50,
    status: "VETOED",
    confidence: 0.72,
    score: -45.0,
    vetoReason: "Devil's Advocate VETO: Overbought RSI (76.2) + High VIX (26.4) creates extreme liquidity flush risk.",
    riskReward: 1.8,
    entry: 148.50,
    sl: 142.20,
    tp: 159.80,
    shares: 0,
    riskUsd: 0,
    execution: "VETOED_BEFORE_DISPATCH",
    timestamp: "2026-08-28T15:12:00Z",
    postReflection: "Asset dropped -4.8% in the next session. Devil's Advocate trap identification fully vindicated, saving $1,000 USD risk allocation."
  },
  {
    id: "PASSPORT-20260828-BTC02",
    ticker: "BTC/USD",
    class: "CRYPTO",
    direction: "LONG",
    price: 88420.00,
    status: "APPROVED",
    confidence: 0.88,
    score: 68.0,
    vetoReason: "None. Devil's Advocate passed with low severity rating (22% trap risk).",
    riskReward: 2.4,
    entry: 88420.00,
    sl: 86100.00,
    tp: 93980.00,
    shares: 0.431,
    riskUsd: 1000.00,
    execution: "EXECUTED_ON_ALPACA",
    timestamp: "2026-08-28T14:40:00Z",
    postReflection: "Trade reached Take-Profit target ($93,980.00) for a net gain of +$2,396.00 USD."
  },
  {
    id: "PASSPORT-20260828-TSLA03",
    ticker: "TSLA",
    class: "US_EQUITY",
    direction: "LONG",
    price: 214.30,
    status: "DOWNSIZED",
    confidence: 0.65,
    score: 42.0,
    vetoReason: "Approved with caution. 50% downsize applied due to Devil's Advocate flagging choppy 50-EMA consolidation.",
    riskReward: 2.1,
    entry: 214.30,
    sl: 206.50,
    tp: 230.60,
    shares: 64.1,
    riskUsd: 500.00,
    execution: "EXECUTED_ON_ALPACA",
    timestamp: "2026-08-28T13:25:00Z",
    postReflection: "Downsized risk protected portfolio during mid-day chop before gradual upward drift."
  },
  {
    id: "PASSPORT-20260828-AMD04",
    ticker: "AMD",
    class: "US_EQUITY",
    direction: "LONG",
    price: 152.80,
    status: "VETOED",
    confidence: 0.80,
    score: -60.0,
    vetoReason: "Devil's Advocate VETO: Extreme Trap (RSI 81.5 + VIX 29.0). False breakout into institutional supply zone.",
    riskReward: 1.5,
    entry: 152.80,
    sl: 146.00,
    tp: 163.00,
    shares: 0,
    riskUsd: 0,
    execution: "VETOED_BEFORE_DISPATCH",
    timestamp: "2026-08-28T11:05:00Z",
    postReflection: "AMD experienced an immediate -6.2% reversal at market open. Total capital preservation achieved."
  }
];

// Benchmark Scenarios
const BENCHMARK_SCENARIOS = [
  {
    title: "Scenario 1: The RSI 82 Breakout Trap (NVDA)",
    blindBot: {
      action: "BUY ORDER PLACED (100% Sizing)",
      steps: [
        "1. Quant signal detected EMA breakout at $148.50.",
        "2. No adversarial check or self-dissent mechanism.",
        "3. Blindly bought top of expansion range.",
        "4. Price flushed -4.8% directly into 142.20 stop loss."
      ],
      result: "RESULT: -$1,000.00 STOP-OUT LOSS",
      resultClass: "loss"
    },
    argusBot: {
      action: "PROPOSAL VETOED BY DEVIL'S ADVOCATE",
      steps: [
        "1. Quant & News proposed LONG on momentum thesis.",
        "2. Devil's Advocate flagged RSI 76.2 + VIX 26.4 trap risk (78%).",
        "3. Adversarial VETO triggered; trade immediately discarded.",
        "4. Capital preserved; 0 orders routed to Alpaca."
      ],
      result: "RESULT: +$1,000.00 CAPITAL PRESERVED (0 LOSS)",
      resultClass: "saved"
    }
  },
  {
    title: "Scenario 2: The Pre-FOMC Volatility Flush (SPY)",
    blindBot: {
      action: "HEAVY LEVERAGED LONG (100% Sizing)",
      steps: [
        "1. Technical indicator flashed moving average bounce.",
        "2. Ignored upcoming rate announcement volatility spike.",
        "3. Immediate 30-pip market gap triggered stop slippage.",
        "4. Suffered maximum single-trade portfolio drawdown."
      ],
      result: "RESULT: -$1,250.00 LOSS (SLIPPAGE)",
      resultClass: "loss"
    },
    argusBot: {
      action: "DOWNSIZED BY 50% + WIDENED ATR BRACKET",
      steps: [
        "1. Macro Agent flagged impending central bank catalyst.",
        "2. Devil's Advocate scored 55% event dislocation risk.",
        "3. Risk Commander reduced position to 0.5% risk budget.",
        "4. ATR stop absorbed the intraday volatility wick safely."
      ],
      result: "RESULT: +$840.00 PROFIT (CONTROLLED RISK)",
      resultClass: "saved"
    }
  },
  {
    title: "Scenario 3: Low-Liquidity Crypto Whipsaw (ETH)",
    blindBot: {
      action: "BUY ON SHORT-TERM SPIKE",
      steps: [
        "1. Chased sudden volume pump on 5-minute chart.",
        "2. Ignored overall bearish macro crypto regime.",
        "3. Trapped at local top during liquidity sweep.",
        "4. Account suffered consecutive drawdown hit."
      ],
      result: "RESULT: -$890.00 LOSS",
      resultClass: "loss"
    },
    argusBot: {
      action: "VETOED (VIX REGIME & LOW CONFIDENCE)",
      steps: [
        "1. News Agent detected unverified rumor source.",
        "2. Devil's Advocate identified liquidity grab pattern.",
        "3. Disqualification risk scored 82% -> VETO issued.",
        "4. Passport archived to audit log; desk remained safe."
      ],
      result: "RESULT: PROTECTED FROM WHIPSAW (0 LOSS)",
      resultClass: "saved"
    }
  }
];

// State Management
let isKillSwitchActive = false;
let currentPassports = [...SAMPLE_PASSPORTS];

// DOM Elements
document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  initTickerPreview();
  initDeliberation();
  initPassportArchive();
  initBenchmarkArena(0);
  initKillSwitch();
});

// Tab Navigation
function initTabs() {
  const tabBtns = document.querySelectorAll(".tab-btn");
  const tabContents = document.querySelectorAll(".tab-content");

  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      tabBtns.forEach(b => b.classList.remove("active"));
      tabContents.forEach(c => c.classList.remove("active"));

      btn.classList.add("active");
      const targetId = `tab-${btn.dataset.tab}`;
      const targetContent = document.getElementById(targetId);
      if (targetContent) {
        targetContent.classList.add("active");
      }
    });
  });
}

// Ticker Select & Preview Update
function initTickerPreview() {
  const select = document.getElementById("tickerSelect");
  const previewTicker = document.getElementById("previewTicker");
  const previewClass = document.getElementById("previewClass");
  const previewPrice = document.getElementById("previewPrice");
  const previewRSI = document.getElementById("previewRSI");
  const previewATR = document.getElementById("previewATR");
  const previewVIX = document.getElementById("previewVIX");

  select.addEventListener("change", (e) => {
    const selectedOption = select.options[select.selectedIndex];
    const ticker = selectedOption.value;
    const price = selectedOption.dataset.price;
    const rsi = selectedOption.dataset.rsi;
    const vix = selectedOption.dataset.vix;

    previewTicker.textContent = ticker;
    previewClass.textContent = ticker.includes("/") ? "CRYPTO" : "US EQUITY";
    previewPrice.textContent = `$${parseFloat(price).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
    previewRSI.textContent = rsi;
    previewVIX.textContent = vix;

    // RSI color cue
    if (parseFloat(rsi) > 70) {
      previewRSI.className = "val text-red";
    } else if (parseFloat(rsi) < 35) {
      previewRSI.className = "val text-green";
    } else {
      previewRSI.className = "val text-cyan";
    }
  });
}

// Committee Deliberation Simulation
function initDeliberation() {
  const conveneBtn = document.getElementById("conveneCommitteeBtn");
  const select = document.getElementById("tickerSelect");
  const container = document.getElementById("agentCardsContainer");
  const statusBadge = document.getElementById("deliberationStatusBadge");
  const consensusFooter = document.getElementById("consensusFooter");

  conveneBtn.addEventListener("click", () => {
    if (isKillSwitchActive) {
      alert("🚨 Cannot convene committee: Emergency Kill Switch is currently ACTIVE. Reset the desk first.");
      return;
    }

    const selectedOption = select.options[select.selectedIndex];
    const ticker = selectedOption.value;
    const trend = selectedOption.dataset.trend;
    const price = parseFloat(selectedOption.dataset.price);
    const rsi = parseFloat(selectedOption.dataset.rsi);
    const vix = parseFloat(selectedOption.dataset.vix);

    runCommitteeDeliberation(ticker, trend, price, rsi, vix);
  });
}

function runCommitteeDeliberation(ticker, trend, price, rsi, vix) {
  const container = document.getElementById("agentCardsContainer");
  const statusBadge = document.getElementById("deliberationStatusBadge");
  const consensusFooter = document.getElementById("consensusFooter");
  const banner = document.getElementById("consensusBanner");
  const icon = document.getElementById("consensusIcon");
  const title = document.getElementById("consensusVerdictTitle");
  const summary = document.getElementById("consensusSummaryText");

  container.innerHTML = "";
  consensusFooter.classList.add("hidden");
  statusBadge.textContent = "DELIBERATING...";
  statusBadge.className = "badge badge-pulse";

  // Determine Agent Votes based on Scenario
  let quantVote, newsVote, macroVote, daReview, consensusStatus;

  if (trend === "EXTREME_TRAP" || (trend === "BULLISH_OVERBOUGHT" && rsi > 75)) {
    // Veto Case
    quantVote = {
      name: "Quant Specialist",
      role: "Technicals, Indicators & Momentum",
      vote: "BUY",
      voteClass: "vote-buy",
      score: "+45.0",
      confidence: "78%",
      thesis: `Strong upward continuation pattern. Price above 20 & 50 EMAs. Volume spike (+38%).`,
      bullets: ["Breakout above resistance band", "RSI is elevated at " + rsi + " (momentum push)"]
    };
    newsVote = {
      name: "News & Sentiment Agent",
      role: "Catalysts, Filings & Market Sentiment",
      vote: "BUY",
      voteClass: "vote-buy",
      score: "+50.0",
      confidence: "82%",
      thesis: `Positive headline coverage regarding next-gen enterprise shipments and analyst upgrades.`,
      bullets: ["3 positive analyst notes", "Institutional call option accumulation detected"]
    };
    macroVote = {
      name: "Macro & Regime Agent",
      role: "Sector Rotation & Market Beta",
      vote: "HOLD",
      voteClass: "vote-hold",
      score: "0.0",
      confidence: "50%",
      thesis: `Broader index beta is elevated with VIX at ${vix}. Caution recommended for high-beta longs.`,
      bullets: ["Tech sector relative strength positive", "Macro liquidity tightening"]
    };
    optionsVote = {
      name: "Options Flow Agent",
      role: "Derivatives, Gamma Exposure & Put/Call Skew",
      vote: "VETO (SKEW)",
      voteClass: "vote-veto",
      score: "-40.0",
      confidence: "80%",
      thesis: `Heavy out-of-the-money put buying and negative dealer gamma pin overhead.`,
      bullets: ["Put/Call ratio skewed to 1.35", "Dealer hedging creates heavy resistance band"]
    };
    daReview = {
      name: "Devil's Advocate (Adversarial Dissent)",
      role: "Trap Detection, Stress-Testing & VETO Power",
      vote: "VETO (TRAP)",
      voteClass: "vote-veto",
      score: "TRAP RISK: 84%",
      confidence: "94%",
      thesis: `CRITICAL DISSENT: Long entry rejected! Severe Overbought Momentum Trap at RSI ${rsi}. False breakout vulnerability into heavy overhead dealer gamma resistance.`,
      bullets: [
        `Overbought exhaustion: RSI ${rsi} historically reverts into 20-EMA within 2 sessions`,
        `High Volatility flush vulnerability with VIX at ${vix}`,
        `Asymmetric downside: Risk/Reward ratio inverted (< 0.8:1)`
      ],
      isDA: true
    };
    consensusStatus = "VETOED";
  } else if (trend === "CHOPPY") {
    // Downsized Case
    quantVote = {
      name: "Quant Specialist",
      role: "Technicals, Indicators & Momentum",
      vote: "BUY",
      voteClass: "vote-buy",
      score: "+35.0",
      confidence: "65%",
      thesis: `Price testing support near lower Bollinger Band. Mild stochastic oversold recovery.`,
      bullets: ["Support bounce holding", "ATR volatility compressed"]
    };
    newsVote = {
      name: "News & Sentiment Agent",
      role: "Catalysts, Filings & Market Sentiment",
      vote: "HOLD",
      voteClass: "vote-hold",
      score: "0.0",
      confidence: "55%",
      thesis: `Mixed headline sentiment. No high-impact catalysts scheduled for the current session.`,
      bullets: ["Neutral sentiment index", "Average social volume"]
    };
    macroVote = {
      name: "Macro & Regime Agent",
      role: "Sector Rotation & Market Beta",
      vote: "BUY",
      voteClass: "vote-buy",
      score: "+30.0",
      confidence: "60%",
      thesis: `Sector ETF showing steady accumulation.`,
      bullets: ["Inflows into consumer discretionary", "Yield curve stable"]
    };
    optionsVote = {
      name: "Options Flow Agent",
      role: "Derivatives, Gamma Exposure & Put/Call Skew",
      vote: "HOLD",
      voteClass: "vote-hold",
      score: "0.0",
      confidence: "60%",
      thesis: `Balanced straddle pricing ahead of weekly expiry. Put/Call ratio neutral at 0.95.`,
      bullets: ["Moderate implied volatility", "No large institutional block sweeps"]
    };
    daReview = {
      name: "Devil's Advocate (Adversarial Dissent)",
      role: "Trap Detection, Stress-Testing & VETO Power",
      vote: "DOWNSIZE (50%)",
      voteClass: "vote-hold",
      score: "TRAP RISK: 52%",
      confidence: "80%",
      thesis: `Range-Bound Fakeout Concern: Ticker is trapped in a 50-EMA consolidation chop box. Approving with 50% capital downsize and tight ATR trailing stop.`,
      bullets: [
        "Range-bound false breakout frequency > 60%",
        "Downsize position by 50% ($500 USD risk allocation)",
        "Strict 1.5x ATR bracket enforcement"
      ],
      isDA: true
    };
    consensusStatus = "DOWNSIZED";
  } else {
    // Approved Case (Healthy Trend)
    quantVote = {
      name: "Quant Specialist",
      role: "Technicals, Indicators & Momentum",
      vote: "BUY",
      voteClass: "vote-buy",
      score: "+70.0",
      confidence: "88%",
      thesis: `Clean bullish trend structure. Price ($${price.toLocaleString()}) > EMA20 > EMA50. Healthy RSI (${rsi}).`,
      bullets: ["Golden cross confirmed", "Volume expansion on bullish candles"]
    };
    newsVote = {
      name: "News & Sentiment Agent",
      role: "Catalysts, Filings & Market Sentiment",
      vote: "BUY",
      voteClass: "vote-buy",
      score: "+65.0",
      confidence: "85%",
      thesis: `Strong institutional inflows and positive ecosystem adoption metrics.`,
      bullets: ["Major network hash rate / volume ATH", "Bullish sentiment ratio 84%"]
    };
    macroVote = {
      name: "Macro & Regime Agent",
      role: "Sector Rotation & Market Beta",
      vote: "BUY",
      voteClass: "vote-buy",
      score: "+55.0",
      confidence: "78%",
      thesis: `Macro regime is constructive. Low correlation with broad market volatility.`,
      bullets: ["VIX at peaceful levels (" + vix + ")", "Institutional allocation accelerating"]
    };
    optionsVote = {
      name: "Options Flow Agent",
      role: "Derivatives, Gamma Exposure & Put/Call Skew",
      vote: "BUY",
      voteClass: "vote-buy",
      score: "+60.0",
      confidence: "82%",
      thesis: `Bullish institutional call flow sweep detected. Low Put/Call ratio (0.65).`,
      bullets: ["Positive dealer gamma pin providing price floor", "Call volume 2.4x above 30-day average"]
    };
    daReview = {
      name: "Devil's Advocate (Adversarial Dissent)",
      role: "Trap Detection, Stress-Testing & VETO Power",
      vote: "APPROVED (PASS)",
      voteClass: "vote-buy",
      score: "TRAP RISK: 18%",
      confidence: "92%",
      thesis: `Stress-test passed. No fatal bull traps or liquidity grabs detected. Favorable 2.4:1 Risk/Reward structure confirmed.`,
      bullets: [
        "RSI is in healthy expansion zone without exhaustion",
        "Support structural depth is strong",
        "Approved for full 1.0% account risk allocation"
      ],
      isDA: true
    };
    consensusStatus = "APPROVED";
  }

  // Sequence Agent Card Renderings
  const sequence = [quantVote, newsVote, macroVote, optionsVote, daReview];
  let delay = 0;

  sequence.forEach((agent, index) => {
    setTimeout(() => {
      const card = document.createElement("div");
      card.className = `agent-card ${agent.isDA ? 'da-card' : ''}`;
      
      const avatar = agent.isDA ? '😈' : (index === 0 ? '📊' : (index === 1 ? '📰' : '🌐'));
      const bulletClass = agent.isDA ? 'bullet-list traps' : 'bullet-list';

      card.innerHTML = `
        <div class="agent-card-header">
          <div class="agent-identity">
            <span class="agent-avatar">${avatar}</span>
            <div>
              <span class="agent-name">${agent.name}</span>
              <span class="agent-role">${agent.role}</span>
            </div>
          </div>
          <span class="vote-badge ${agent.voteClass}">${agent.vote}</span>
        </div>
        <div class="agent-body">
          <p class="agent-thesis"><strong>Thesis:</strong> ${agent.thesis}</p>
          <ul class="${bulletClass}">
            ${agent.bullets.map(b => `<li>${b}</li>`).join('')}
          </ul>
        </div>
      `;
      container.appendChild(card);

      if (index === sequence.length - 1) {
        // Show Consensus Verdict
        setTimeout(() => {
          renderConsensusVerdict(ticker, consensusStatus, price, rsi);
        }, 400);
      }
    }, delay);
    delay += 500;
  });
}

function renderConsensusVerdict(ticker, status, price, rsi) {
  const statusBadge = document.getElementById("deliberationStatusBadge");
  const consensusFooter = document.getElementById("consensusFooter");
  const banner = document.getElementById("consensusBanner");
  const icon = document.getElementById("consensusIcon");
  const title = document.getElementById("consensusVerdictTitle");
  const summary = document.getElementById("consensusSummaryText");
  const viewPassportBtn = document.getElementById("viewGeneratedPassportBtn");

  statusBadge.textContent = "DELIBERATION COMPLETE";
  statusBadge.className = "badge badge-success";
  consensusFooter.classList.remove("hidden");

  banner.className = `consensus-status-banner ${status.toLowerCase()}`;

  if (status === "VETOED") {
    icon.textContent = "❌";
    title.textContent = `DECISION: VETOED BY DEVIL'S ADVOCATE (${ticker})`;
    summary.textContent = `Trade discarded before sending to Alpaca. Prevented entry into high-risk trap (RSI: ${rsi}). 0 capital allocated.`;
  } else if (status === "DOWNSIZED") {
    icon.textContent = "⚠️";
    title.textContent = `DECISION: APPROVED WITH CAUTION (${ticker})`;
    summary.textContent = `Capital downsized by 50% ($500 USD Risk) due to Devil's Advocate concerns over range consolidation.`;
  } else {
    icon.textContent = "✅";
    title.textContent = `DECISION: APPROVED FOR EXECUTION (${ticker})`;
    summary.textContent = `Multi-agent consensus confirmed. Dispatched bracket order to Alpaca Paper Trading Engine with 1:2.4 R/R.`;
  }

  // Create new passport record
  const newPassport = {
    id: `PASSPORT-20260828-${ticker.replace('/', '')}${Math.floor(Math.random() * 90 + 10)}`,
    ticker: ticker,
    class: ticker.includes("/") ? "CRYPTO" : "US_EQUITY",
    direction: "LONG",
    price: price,
    status: status,
    confidence: status === "VETOED" ? 0.72 : (status === "DOWNSIZED" ? 0.65 : 0.88),
    score: status === "VETOED" ? -55.0 : (status === "DOWNSIZED" ? 42.0 : 68.0),
    vetoReason: status === "VETOED" ? `Devil's Advocate VETO: Overbought Trap (RSI ${rsi}).` : "None / Approved.",
    riskReward: 2.2,
    entry: price,
    sl: roundPrice(price * 0.96),
    tp: roundPrice(price * 1.08),
    shares: status === "VETOED" ? 0 : (ticker.includes("BTC") ? 0.43 : 45),
    riskUsd: status === "VETOED" ? 0 : (status === "DOWNSIZED" ? 500 : 1000),
    execution: status === "VETOED" ? "VETOED_BEFORE_DISPATCH" : "EXECUTED_ON_ALPACA",
    timestamp: new Date().toISOString(),
    postReflection: status === "VETOED" ? "Adversarial VETO protected capital from exhaustion trap." : "Order successfully monitored in Alpaca paper account."
  };

  currentPassports.unshift(newPassport);
  renderPassportsGrid("ALL");

  viewPassportBtn.onclick = () => {
    openPassportModal(newPassport);
  };
}

function roundPrice(val) {
  return parseFloat(val.toFixed(2));
}

// Trade Passport Archive
function initPassportArchive() {
  const filterBtns = document.querySelectorAll(".filter-btn");
  filterBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      filterBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      renderPassportsGrid(btn.dataset.filter);
    });
  });

  renderPassportsGrid("ALL");

  // Modal close handlers
  const closeModalBtn = document.getElementById("closeModalBtn");
  const modal = document.getElementById("passportModal");
  const backdrop = modal.querySelector(".modal-backdrop");

  closeModalBtn.addEventListener("click", () => modal.classList.add("hidden"));
  backdrop.addEventListener("click", () => modal.classList.add("hidden"));
}

function renderPassportsGrid(filter) {
  const grid = document.getElementById("passportsGrid");
  grid.innerHTML = "";

  const filtered = currentPassports.filter(p => {
    if (filter === "ALL") return true;
    return p.status === filter;
  });

  filtered.forEach(p => {
    const card = document.createElement("div");
    card.className = "passport-card";
    
    let statusClass = "text-green";
    if (p.status === "VETOED") statusClass = "text-red";
    if (p.status === "DOWNSIZED") statusClass = "text-yellow";

    card.innerHTML = `
      <div class="passport-header">
        <div>
          <span class="ticker-symbol">${p.ticker}</span>
          <span class="ticker-class">${p.class}</span>
        </div>
        <span class="vote-badge ${p.status === 'VETOED' ? 'vote-veto' : (p.status === 'DOWNSIZED' ? 'vote-hold' : 'vote-buy')}">${p.status}</span>
      </div>
      <div class="passport-id">${p.id}</div>
      <div class="passport-meta-row" style="margin-top: 10px;">
        <div class="passport-meta-item">
          <span class="lbl">Entry Price</span>
          <span class="val">$${p.entry.toLocaleString()}</span>
        </div>
        <div class="passport-meta-item">
          <span class="lbl">Stop Loss</span>
          <span class="val text-red">$${p.sl.toLocaleString()}</span>
        </div>
        <div class="passport-meta-item">
          <span class="lbl">Target Profit</span>
          <span class="val text-green">$${p.tp.toLocaleString()}</span>
        </div>
      </div>
      <div class="passport-meta-row">
        <div class="passport-meta-item">
          <span class="lbl">Allocated Risk</span>
          <span class="val">$${p.riskUsd} USD</span>
        </div>
        <div class="passport-meta-item">
          <span class="lbl">Execution</span>
          <span class="val text-cyan">${p.execution}</span>
        </div>
      </div>
      <div class="passport-da-callout">
        <strong>Audit Note:</strong> ${p.vetoReason}
      </div>
    `;

    card.addEventListener("click", () => openPassportModal(p));
    grid.appendChild(card);
  });
}

function openPassportModal(p) {
  const modal = document.getElementById("passportModal");
  const title = document.getElementById("modalPassportTitle");
  const body = document.getElementById("modalPassportBody");

  title.textContent = `TRADE PASSPORT: ${p.ticker} (${p.id})`;

  body.innerHTML = `
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; padding-bottom: 10px; border-bottom: 1px solid var(--border-color);">
      <div>
        <span style="font-size: 20px; font-weight: 800; color: var(--color-cyan);">${p.ticker}</span>
        <span style="margin-left: 8px; font-size: 11px; color: var(--text-dim);">${p.direction} • ${p.class}</span>
      </div>
      <span class="vote-badge ${p.status === 'VETOED' ? 'vote-veto' : (p.status === 'DOWNSIZED' ? 'vote-hold' : 'vote-buy')}">${p.status}</span>
    </div>

    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; background: rgba(0,0,0,0.3); padding: 12px; border-radius: var(--radius-sm);">
      <div>
        <span style="font-size: 11px; color: var(--text-dim); display: block;">TARGET ENTRY</span>
        <strong style="font-family: var(--font-mono); font-size: 14px;">$${p.entry.toLocaleString()}</strong>
      </div>
      <div>
        <span style="font-size: 11px; color: var(--text-dim); display: block;">STOP LOSS (1.5x ATR)</span>
        <strong style="font-family: var(--font-mono); font-size: 14px; color: var(--color-red);">$${p.sl.toLocaleString()}</strong>
      </div>
      <div>
        <span style="font-size: 11px; color: var(--text-dim); display: block;">TAKE PROFIT (MIN 2:1)</span>
        <strong style="font-family: var(--font-mono); font-size: 14px; color: var(--color-green);">$${p.tp.toLocaleString()}</strong>
      </div>
    </div>

    <div style="margin-bottom: 16px;">
      <h4 style="font-size: 13px; color: var(--text-main); margin-bottom: 6px;">😈 Devil's Advocate Dissent & Stress Test</h4>
      <div style="background: rgba(239, 68, 68, 0.08); border-left: 3px solid var(--color-red); padding: 10px 14px; border-radius: 4px; font-size: 12px; color: #FCA5A5;">
        ${p.vetoReason}
      </div>
    </div>

    <div style="margin-bottom: 16px;">
      <h4 style="font-size: 13px; color: var(--text-main); margin-bottom: 6px;">📝 Chief Auditor Post-Trade Reflection</h4>
      <div style="background: rgba(6, 182, 212, 0.08); border-left: 3px solid var(--color-cyan); padding: 10px 14px; border-radius: 4px; font-size: 12px; color: #BAE6FD;">
        ${p.postReflection}
      </div>
    </div>

    <div style="display: flex; justify-content: space-between; font-size: 11px; color: var(--text-dim); border-top: 1px solid var(--border-color); padding-top: 10px;">
      <span>Timestamp: ${p.timestamp}</span>
      <span>Alpaca Dispatch: ${p.execution}</span>
    </div>
  `;

  modal.classList.remove("hidden");
}

// Benchmark Arena
function initBenchmarkArena(scenarioIndex) {
  const arena = document.getElementById("comparisonArena");
  const btns = document.querySelectorAll(".scenario-btn");

  btns.forEach((btn, idx) => {
    btn.addEventListener("click", () => {
      btns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      renderScenario(idx);
    });
  });

  renderScenario(scenarioIndex);
}

function renderScenario(index) {
  const arena = document.getElementById("comparisonArena");
  const s = BENCHMARK_SCENARIOS[index];

  arena.innerHTML = `
    <!-- Left: Blind Bot -->
    <div class="arena-card blind-bot">
      <div class="arena-header">
        <h4 style="color: #F87171;">❌ Conventional "Blind" Trading Bot</h4>
        <span class="vote-badge vote-sell">NO SELF-DISSENT</span>
      </div>
      <div style="font-size: 13px; font-weight: 700; color: var(--text-main); margin-bottom: 12px;">
        Action: ${s.blindBot.action}
      </div>
      <div class="arena-steps">
        ${s.blindBot.steps.map(step => `<div class="step-box">${step}</div>`).join('')}
      </div>
      <div class="arena-result ${s.blindBot.resultClass}">
        ${s.blindBot.result}
      </div>
    </div>

    <!-- Right: ARGUS AI -->
    <div class="arena-card argus-bot">
      <div class="arena-header">
        <h4 style="color: #34D399;">🛡️ ARGUS AI Multi-Agent Desk</h4>
        <span class="vote-badge vote-buy">SELF-DISSENT ACTIVE</span>
      </div>
      <div style="font-size: 13px; font-weight: 700; color: var(--text-main); margin-bottom: 12px;">
        Action: ${s.argusBot.action}
      </div>
      <div class="arena-steps">
        ${s.argusBot.steps.map(step => `<div class="step-box" style="border-left: 2px solid var(--color-cyan);">${step}</div>`).join('')}
      </div>
      <div class="arena-result ${s.argusBot.resultClass}">
        ${s.argusBot.result}
      </div>
    </div>
  `;
}

// Emergency Kill Switch
function initKillSwitch() {
  const manualBtn = document.getElementById("manualKillSwitchBtn");
  const testTripBtn = document.getElementById("testTripKillSwitchBtn");
  const resetBtn = document.getElementById("resetKillSwitchBtn");
  const banner = document.getElementById("killSwitchBanner");
  const badge = document.getElementById("killSwitchBadge");

  function tripKillSwitch(reason) {
    isKillSwitchActive = true;
    banner.classList.remove("hidden");
    document.getElementById("killSwitchReason").textContent = reason;

    badge.innerHTML = `
      <span class="dot" style="background: #EF4444; box-shadow: 0 0 8px #EF4444;"></span>
      <span style="color: #EF4444;">CIRCUIT BREAKER: <strong>TRIPPED (FROZEN)</strong></span>
    `;

    // Visual feedback
    console.warn("[ARGUS KILL SWITCH] Circuit breaker engaged. All pending orders cancelled.");
  }

  function resetKillSwitch() {
    isKillSwitchActive = false;
    banner.classList.add("hidden");

    badge.innerHTML = `
      <span class="dot green-dot"></span>
      <span>CIRCUIT BREAKER: <strong>ARMED</strong></span>
    `;
  }

  manualBtn.addEventListener("click", () => {
    tripKillSwitch("Manual operator emergency trigger engaged. Desk execution suspended.");
  });

  testTripBtn.addEventListener("click", () => {
    tripKillSwitch("Simulated max daily drawdown spike (3.4% > 3.0% threshold). All Alpaca open orders cancelled.");
  });

  resetBtn.addEventListener("click", () => {
    resetKillSwitch();
  });
}
