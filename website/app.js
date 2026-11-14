(function () {
  function showToast(msg) {
    const el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.classList.add("show");
    window.setTimeout(() => el.classList.remove("show"), 2200);
  }

  const navToggle = document.getElementById("nav-toggle");
  const navLinks = document.getElementById("nav-links");
  if (navToggle && navLinks) {
    navToggle.addEventListener("click", () => {
      const open = navLinks.classList.toggle("open");
      navToggle.setAttribute("aria-expanded", String(open));
    });
  }

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = btn.getAttribute("data-copy") || "";
      try {
        await navigator.clipboard.writeText(text);
        showToast("Copied to clipboard");
      } catch (err) {
        showToast("Copy failed: " + (err && err.message ? err.message : "clipboard unavailable"));
      }
    });
  });

  const tickerEl = document.getElementById("studio-ticker");
  const regimeEl = document.getElementById("studio-regime");
  const vixEl = document.getElementById("studio-vix");
  const dissentEl = document.getElementById("studio-dissent");
  const runBtn = document.getElementById("studio-run");

  function evaluateCommittee() {
    if (!tickerEl || !regimeEl || !vixEl || !dissentEl) return;
    const ticker = tickerEl.value;
    const regime = regimeEl.value;
    const vix = Math.max(10, Number(vixEl.value || 18));
    const dissent = Math.min(100, Math.max(0, Number(dissentEl.value || 35)));

    let gate = "APPROVED";
    let riskPct = 1.0;
    if (dissent >= 75 || vix >= 32) {
      gate = "VETO";
      riskPct = 0.0;
    } else if (dissent >= 45 || regime === "CHOPPY") {
      gate = "DOWNSIZE_50";
      riskPct = 0.5;
    }

    const outEl = document.getElementById("studio-output");
    if (outEl) {
      outEl.textContent = JSON.stringify({
        ticker,
        market_regime: regime,
        vix_level: vix,
        devils_advocate_risk_pct: dissent,
        consensus_gate: gate,
        authorized_equity_risk_pct: riskPct,
        stop_atr_multiplier: 1.5,
        take_profit_rr_floor: 2.0,
        passport_invariants: [
          "BRACKET_GEOMETRY",
          "RISK_REWARD_FLOOR",
          "DEVILS_ADVOCATE_VETO_AUTHORITY",
          "ACCOUNT_RISK_BUDGET_CAP"
        ]
      }, null, 2);
    }
  }

  if (runBtn) {
    runBtn.addEventListener("click", evaluateCommittee);
    [tickerEl, regimeEl, vixEl, dissentEl].forEach((el) => {
      if (el) el.addEventListener("change", evaluateCommittee);
    });
    evaluateCommittee();
  }
})();
