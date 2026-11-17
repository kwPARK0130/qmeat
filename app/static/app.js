const $ = (s) => document.querySelector(s),
  $$ = (s) => [...document.querySelectorAll(s)];
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const num = (v, d = 0) =>
  v === null || v === undefined
    ? "—"
    : Number(v).toLocaleString("ko-KR", { maximumFractionDigits: d });
const labels = {
  overview: "종합 현황",
  market: "시장 통계",
  calculator: "원가 계산기",
  fx: "환율·단위 환산",
  saved: "관심 품목·저장 검색",
  import: "파일 가져오기",
  updates: "자료 조회·갱신",
};
const titles = {
  overview: "시장을 읽고, 원가를 판단하세요.",
  market: "수입 시장을 같은 기준으로.",
  calculator: "견적에서 판매가격까지.",
  fx: "환율과 단위를 정확하게.",
  saved: "다시 찾을 조건, 보존할 계산.",
  import: "흩어진 자료를 하나의 흐름으로.",
  updates: "자료의 최신 상태를 확인하세요.",
};
const statusLabels = {
  queued: "대기",
  running: "진행 중",
  succeeded_changed: "갱신 완료",
  succeeded_unchanged: "새 자료 없음",
  partial_failed: "부분 실패",
  failed: "실패",
  cancelled: "취소",
  authentication_required: "인증 필요",
  file_only: "파일 가져오기",
  verification_required: "연결 검증 필요",
  configured_unverified: "설정됨 · 검증 전",
};
let page = "overview",
  stats = null,
  pageNo = 1,
  lastCalc = null,
  previewId = null,
  searches = [],
  calculations = [],
  parentId = null;
function toast(message, error = false) {
  $("#alert").hidden = false;
  $("#alert").className = error ? "error" : "";
  $("#alert").textContent = message;
  window.scrollTo({ top: 0, behavior: "smooth" });
}
async function api(url, options = {}) {
  const r = await fetch(url, options);
  if (!r.ok) {
    let e;
    try {
      e = await r.json();
    } catch {
      e = { detail: "서버 응답 오류" };
    }
    throw Error(
      typeof e.detail === "string" ? e.detail : JSON.stringify(e.detail),
    );
  }
  return r.json();
}
const post = (url, data) =>
  api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
function protect(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (e) {
      toast(e.message, true);
    }
  };
}
function shiftMonth(value, n) {
  const [y, m] = value.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + n, 1));
  return d.toISOString().slice(0, 7);
}
const thisMonth = new Intl.DateTimeFormat("sv-SE", {
  timeZone: "Asia/Seoul",
  year: "numeric",
  month: "2-digit",
}).format(new Date());
const end = shiftMonth(thisMonth, -1);
$("#filters").elements.start.value = shiftMonth(end, -11);
$("#filters").elements.end.value = end;
$("#clock").textContent = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul",
  month: "long",
  day: "numeric",
  weekday: "short",
}).format(new Date());
function filter() {
  return Object.fromEntries(new FormData($("#filters")));
}
function applyFilter(f) {
  Object.entries(f).forEach(([k, v]) => {
    if ($("#filters").elements[k]) $("#filters").elements[k].value = v;
  });
}
function urlState() {
  const u = new URL(location.href);
  u.hash = page;
  u.search = new URLSearchParams(filter());
  history.pushState({}, "", u);
}
function empty(title, message, button = true) {
  return `<div class="empty"><span>▥</span><h3>${esc(title)}</h3><p>${esc(message)}</p>${button ? '<button data-go="import">공식 파일 가져오기 ↗</button>' : ""}</div>`;
}
async function navigate(next, push = true) {
  page = labels[next] ? next : "overview";
  $$(".page").forEach((x) => (x.hidden = x.id !== `page-${page}`));
  $$("nav button").forEach((x) =>
    x.classList.toggle("active", x.dataset.page === page),
  );
  $("#crumb").textContent = labels[page];
  $("#title").textContent = titles[page];
  $("#filter-panel").hidden = !["overview", "market"].includes(page);
  $("#subtitle").textContent =
    page === "overview"
      ? "수입 통계부터 견적의 수익성까지, 매일의 판단을 한곳에서."
      : "출처와 계산 근거를 보존하는 육류 유통 워크스페이스";
  if (push) urlState();
  if (["overview", "market"].includes(page)) await loadStats();
  if (page === "fx") await loadFx();
  if (page === "saved") await loadSaved();
  if (page === "updates") await loadUpdates();
}
async function loadStats() {
  const f = filter();
  stats = await api(
    "/api/stats?" + new URLSearchParams({ ...f, page: pageNo }),
  );
  const t = stats.total;
  $("#period-label").textContent = `${f.start} — ${f.end} · 조회기간 합계`;
  const cards = [
    ["수입 중량", t.weight_kg, "kg", "조회기간 순중량 합계"],
    ["수입 금액", t.amount_usd, "USD", "CIF 기준은 관세청 자료에 적용"],
    ["가중 평균 수입단가", t.unit_price, "USD/kg", "금액 합계 ÷ 순중량 합계"],
    [
      "동기간 누계 증감",
      stats.ytd_change.value,
      "% ",
      stats.ytd_change.reason || "전년 동일 월수 기준",
    ],
  ];
  $("#kpis").innerHTML = cards
    .map(
      ([label, value, unit, note]) =>
        `<button class="kpi" data-go="market"><div class="kpi-label">${label}<span>↗</span></div><div class="kpi-value">${num(value, unit.includes("kg") || unit.includes("%") ? 2 : 0)}<small>${unit}</small></div><div class="kpi-note">${value === null ? "저장된 비교 가능 자료 없음" : note}</div></button>`,
    )
    .join("");
  renderChart(stats.timeline, f.metric);
  $("#chart-foot").textContent =
    `자료 기준 ${stats.meta.latest_data_period || "미조회"} · 누락 ${stats.meta.missing_months.length}개월 · 수집 범위: ${stats.meta.coverage_status === "partial" ? "부분 자료" : "미조회"}`;
  $("#countries").innerHTML = stats.countries.length
    ? stats.countries
        .map(
          (r) =>
            `<div class="country-row"><div><span>${esc(r.country)}</span><span>${num(r.weight_kg)} kg · ${num(r.share_pct, 1)}%</span></div><div class="bar-track"><div class="bar" style="width:${Math.max(0, Math.min(100, Number(r.share_pct) || 0))}%"></div></div></div>`,
        )
        .join("")
    : empty(
        "비교할 국가 자료가 없습니다",
        "조회 국가의 자료가 모이면 비교할 수 있습니다.",
        false,
      );
  $("#table-meta").textContent =
    `${stats.meta.source} · ${stats.meta.unit} · 자료 기준 ${stats.meta.latest_data_period || "미조회"} · 마지막 확인 ${stats.meta.last_success_at ? new Date(stats.meta.last_success_at).toLocaleString("ko-KR") : "없음"} · 전체시장 완전성 미확인`;
  $("#stats-rows").innerHTML = stats.rows.length
    ? stats.rows
        .map(
          (r) =>
            `<tr><td>${esc(r.period)}</td><td>${esc(r.product)}<small>${esc(r.code)} · ${esc(r.classification_version)}</small></td><td>${esc(r.country)}<small>${esc(r.country_role)}</small></td><td>${num(r.weight_kg, 2)}</td><td>${num(r.amount_usd, 2)}</td><td>${Number(r.weight_kg) ? num(Number(r.amount_usd) / Number(r.weight_kg), 4) : "—"}</td><td>${r.provisional ? "잠정" : "제공 자료"}<small>${esc(r.source)}</small></td></tr>`,
        )
        .join("")
    : '<tr><td colspan="7">' +
      empty(
        "저장된 통계가 없습니다",
        "공식 파일을 가져오거나 현재 조건의 원자료를 요청하세요.",
      ) +
      "</td></tr>";
  $("#monthly").innerHTML = stats.rows.length
    ? `<table><thead><tr><th>월별 비교</th>${stats.timeline.map((r) => `<th>${r.period}</th>`).join("")}</tr></thead><tbody><tr><td>전월 대비</td>${stats.timeline.map((r) => `<td title="${esc(r.mom.reason || "")}">${num(r.mom.value, 1)}${r.mom.value === null ? "" : "%"}</td>`).join("")}</tr><tr><td>전년 동월 대비</td>${stats.timeline.map((r) => `<td title="${esc(r.yoy.reason || "")}">${num(r.yoy.value, 1)}${r.yoy.value === null ? "" : "%"}</td>`).join("")}</tr></tbody></table>`
    : "";
  $("#pagination").textContent = `${pageNo}페이지 · 전체 ${stats.row_count}행`;
  $("#prev-page").disabled = pageNo <= 1;
  $("#next-page").disabled = pageNo * 50 >= stats.row_count;
}
function renderChart(rows, metric) {
  if (!rows.some((r) => r[metric] !== null)) {
    $("#chart").innerHTML = empty(
      "아직 수집된 월별 자료가 없습니다",
      "공식 자료를 연결하면 월별 추이를 확인할 수 있습니다.",
    );
    return;
  }
  const max = Math.max(...rows.map((r) => Number(r[metric]) || 0), 1),
    w = 640,
    h = 220;
  let chunks = [],
    part = [];
  rows.forEach((r, i) => {
    if (r[metric] === null) {
      if (part.length) chunks.push(part);
      part = [];
      return;
    }
    part.push([
      50 + (i * 560) / Math.max(rows.length - 1, 1),
      175 - (Number(r[metric]) / max) * 135,
      r,
    ]);
  });
  if (part.length) chunks.push(part);
  $("#chart").innerHTML =
    `<svg class="chart-svg" viewBox="0 0 ${w} ${h}" role="img" aria-label="월별 수입 추이">${[0, 1, 2, 3].map((n) => `<line x1="50" y1="${40 + n * 45}" x2="615" y2="${40 + n * 45}"/><text x="0" y="${43 + n * 45}">${num((max * (3 - n)) / 3)}</text>`).join("")}${chunks.map((c) => `<polyline points="${c.map((p) => p.slice(0, 2).join(",")).join(" ")}"/>${c.map(([x, y, r]) => `<circle cx="${x}" cy="${y}" r="3"><title>${r.period}: ${num(r[metric], 4)}</title></circle>`).join("")}`).join("")}${rows.map((r, i) => (i % Math.max(1, Math.ceil(rows.length / 8)) === 0 ? `<text x="${40 + (i * 560) / Math.max(rows.length - 1, 1)}" y="205">${r.period.slice(2)}</text>` : "")).join("")}</svg><div class="chart-legend">${{ weight_kg: "순중량 kg", amount_usd: "금액 USD", unit_price: "가중 평균 USD/kg" }[metric]}</div>`;
}
async function sources() {
  const rows = await api("/api/sources");
  $("#source-summary").innerHTML = rows
    .map(
      (r) =>
        `<div class="source-row"><div class="source-name">${esc(r.name)}<small>${esc(r.population)}</small></div><span class="tag">${statusLabels[r.status]}</span></div>`,
    )
    .join("");
  $("#sources-detail").innerHTML = rows
    .map(
      (r) =>
        `<article class="card source-detail"><div><h3>${esc(r.name)} <span class="tag">${statusLabels[r.status]}</span></h3><p>${esc(r.verification)} · 지원 필터: ${r.filters.map(esc).join(", ")}</p><p>마지막 성공: ${r.last_success_at ? esc(new Date(r.last_success_at).toLocaleString("ko-KR")) : "없음"}</p><a href="${esc(r.url)}" target="_blank" rel="noopener">공식 안내 ↗</a></div><button data-source="${r.id}">⟳ 제공처 새로고침</button></article>`,
    )
    .join("");
}
async function refresh(sourcesList) {
  const f = filter();
  const result = await post("/api/jobs", {
    ...f,
    sources: sourcesList,
    version: f.version || "HS2022",
  });
  toast("갱신 작업이 접수되었습니다. 작업 ID: " + result.id.slice(0, 8));
  await navigate("updates");
}
$("#filters").onsubmit = protect(async (e) => {
  e.preventDefault();
  pageNo = 1;
  urlState();
  await loadStats();
});
$("#refresh-all").onclick = protect(() => refresh(["customs", "mfds", "exim"]));
$("#refresh-filter").onclick = protect(() => refresh([filter().source]));
$("#prev-page").onclick = protect(async () => {
  pageNo--;
  await loadStats();
});
$("#next-page").onclick = protect(async () => {
  pageNo++;
  await loadStats();
});
for (const fmt of ["csv", "xlsx"])
  $(`#export-${fmt}`).onclick = () => {
    location.href =
      "/api/export?" + new URLSearchParams({ ...filter(), format: fmt });
  };
$("#save-search").onclick = () => $("#save-dialog").showModal();
$("#close-dialog").onclick = () => $("#save-dialog").close();
$("#save-search-form").onsubmit = protect(async (e) => {
  e.preventDefault();
  await post("/api/documents/search", {
    ...Object.fromEntries(new FormData(e.target)),
    filters: filter(),
  });
  $("#save-dialog").close();
  toast("관심 품목·검색조건을 저장했습니다. 자동 갱신 대상에 포함됩니다.");
});
async function loadSaved() {
  [searches, calculations] = await Promise.all([
    api("/api/documents/search"),
    api("/api/documents/calculation"),
  ]);
  $("#saved-searches").innerHTML = searches.length
    ? searches
        .map(
          (s, i) =>
            `<div class="saved-row"><h3>☆ ${esc(s.name)}</h3><p>${esc(s.filters.source)} · ${esc(s.filters.code || "전체 품목")} · ${esc(s.filters.country || "전체 국가")} · ${s.period_mode === "recent" ? `최근 ${s.months}개월` : `${esc(s.filters.start)} ~ ${esc(s.filters.end)}`}</p><button data-search="${i}">저장 조건 조회 →</button></div>`,
        )
        .join("")
    : empty(
        "저장한 검색이 없습니다",
        "통계 화면에서 관심 품목과 검색조건을 저장하세요.",
        false,
      );
  $("#saved-calculations").innerHTML = calculations.length
    ? calculations
        .map(
          (c, i) =>
            `<div class="saved-row"><h3>${esc(c.name)}</h3><p>${esc(new Date(c.created_at).toLocaleString("ko-KR"))} · ${num(c.result.cost_per_kg, 2)} KRW/kg · ${esc(c.result.status)}</p><button data-calculation="${i}">입력 복사·새 버전 계산</button><a href="/api/document/${c.id}/export">Excel 내보내기 ↓</a></div>`,
        )
        .join("")
    : empty(
        "저장한 원가 계산이 없습니다",
        "계산 결과를 저장하면 당시 환율과 비용을 보관합니다.",
        false,
      );
}
const costNames = ["운임", "보험", "검사·통관", "국내 운송", "보관", "세금"];
$("#cost-lines").innerHTML = costNames
  .map(
    (name, i) =>
      `<div class="cost-line" data-index="${i}"><span>${name}</span><select class="cost-status" aria-label="${name} 반영 상태"><option value="missing">미입력</option><option value="included">구매가격 포함</option><option value="additional">추가</option><option value="na">해당 없음</option></select><input class="cost-amount" type="number" min="0" step="any" placeholder="금액" aria-label="${name} 금액"><div class="cost-extra"><input class="cost-currency" value="KRW" aria-label="${name} 통화"><input class="cost-rate" type="number" value="1" min="0" step="any" aria-label="${name} 환율"><label><input class="cost-recoverable" type="checkbox">회수가능</label></div></div>`,
  )
  .join("");
function calcInput() {
  const p = Object.fromEntries(new FormData($("#calc-form")));
  p.costs = $$(".cost-line").map((r, i) => ({
    name: costNames[i],
    status: r.querySelector(".cost-status").value,
    amount: r.querySelector(".cost-amount").value,
    rate: r.querySelector(".cost-rate").value,
    currency: r.querySelector(".cost-currency").value,
    recoverable: r.querySelector(".cost-recoverable").checked,
  }));
  return p;
}
$("#calc-form").addEventListener("input", () => {
  lastCalc = null;
  const b = $("#save-calc");
  if (b) {
    b.disabled = true;
    b.textContent = "입력 변경됨 · 다시 계산하세요";
  }
});
$("#calc-form").onsubmit = protect(async (e) => {
  e.preventDefault();
  const input = calcInput(),
    r = await post("/api/calculate", input);
  lastCalc = { input, result: r };
  $("#calc-result").className = "result-content";
  const keys = {
    product_krw: "원화 제품대금",
    landed_cost: "총 도입원가",
    cash_required: "필요 자금",
    target_price: "목표 순판매단가 / kg",
    markup_price: "가산율 방식 단가 / kg",
    allocated_cost: "판매분 원가",
    remaining_cost: "잔여 원가",
    net_revenue: "순매출",
    profit: "예상 거래이익",
    profit_pct: "거래이익률 %",
  };
  $("#calc-result").innerHTML =
    `<div class="result-main"><label>판매 가능 kg당 원가</label><strong>${num(r.cost_per_kg, 2)} <small>원/kg</small></strong></div>${r.missing_costs.length ? `<div class="warning">미입력 비용: ${r.missing_costs.map(esc).join(", ")}<br>누락 비용을 제외한 잠정 계산입니다.</div>` : ""}${Object.entries(
      keys,
    )
      .map(
        ([k, l]) =>
          `<div class="result-line"><span>${l}</span><strong>${num(r[k], 2)}</strong></div>`,
      )
      .join(
        "",
      )}<p class="muted">수식 v${esc(r.formula_version)} · 중간 반올림 없음 · 표시 소수 2자리</p><button id="save-calc" class="primary full">계산 스냅샷 저장</button>`;
});
async function loadFx() {
  const rows = await api("/api/fx");
  $("#fx-rates").innerHTML = rows.length
    ? rows
        .map(
          (r) =>
            `<div class="source-row"><div class="source-name">${esc(r.currency)} <strong>${num(r.rate, 4)} 원</strong><small>실제 기준일 ${esc(r.date)} · ${esc(r.kind)} · ${esc(r.source)}</small></div><span class="pill">원 / 1외화</span></div>`,
        )
        .join("")
    : empty(
        "저장된 고시환율이 없습니다",
        "인증키 연결 또는 확인한 고시환율을 입력하세요.",
        false,
      );
}
$("#fx-form").onsubmit = protect(async (e) => {
  e.preventDefault();
  await post("/api/fx/manual", Object.fromEntries(new FormData(e.target)));
  toast("고시 기준일과 원래 단위를 함께 저장했습니다.");
  await loadFx();
});
$("#convert-form").onsubmit = protect(async (e) => {
  e.preventDefault();
  const p = Object.fromEntries(new FormData(e.target));
  const r = await post("/api/convert", p);
  $("#convert-result").textContent = `${num(r.value, 8)} ${r.unit}`;
});
const mapFields = {
  period: "기준월 YYYY-MM",
  code: "품목코드 (문자)",
  country: "국가",
  weight: "중량",
  amount: "금액",
  product: "제품명",
  species: "축종 (선택)",
  storage: "보관 (선택)",
  provisional: "잠정 여부 (선택)",
};
$("#mapping").innerHTML = Object.entries(mapFields)
  .map(([k, l]) => `<label>${l}<input data-map="${k}" value="${k}"></label>`)
  .join("");
$("#import-form").onsubmit = protect(async (e) => {
  e.preventDefault();
  const options = Object.fromEntries(new FormData(e.target));
  options.mapping = Object.fromEntries(
    $$("[data-map]").map((x) => [x.dataset.map, x.value]),
  );
  const form = new FormData();
  form.append("file", $("#import-file").files[0]);
  form.append("options", JSON.stringify(options));
  const r = await api("/api/import/preview", { method: "POST", body: form });
  previewId = r.id;
  $("#import-preview").innerHTML =
    `<h3>미리보기 · 정상 ${r.valid_count}행 / 오류 ${r.errors.length}행</h3><p class="muted">시트: ${r.sheets.map(esc).join(", ")}<br>발견한 열: ${r.headers.map(esc).join(", ")}</p>${r.already_imported ? '<div class="warning">이미 가져온 파일입니다. 동일 관측값은 중복 저장하지 않습니다.</div>' : ""}<div class="table-wrap"><table><thead><tr><th>월</th><th>품목코드</th><th>국가</th><th>kg</th><th>USD</th></tr></thead><tbody>${r.rows.map((row) => `<tr><td>${esc(row.period)}</td><td>${esc(row.code)}</td><td>${esc(row.country)}</td><td>${num(row.weight_kg, 2)}</td><td>${num(row.amount_usd, 2)}</td></tr>`).join("")}</tbody></table></div>${
      r.errors.length
        ? `<div class="warning">${r.errors
            .slice(0, 30)
            .map((x) => `${x.row}행: ${esc(x.reason)}`)
            .join(
              "<br>",
            )}</div><label class="inline"><input id="accept-valid" type="checkbox"> 오류행을 제외하고 정상행만 저장합니다</label>`
        : ""
    }<button id="commit-import" class="primary" ${r.valid_count ? "" : "disabled"}>확정 저장</button>`;
});
async function loadUpdates() {
  await sources();
  const s = await api("/api/schedule");
  $("#schedule-form").elements.time.value = s.time;
  $("#schedule-form").elements.enabled.checked = s.enabled;
  $("#schedule-status").textContent =
    `다음 예약 ${s.next_run ? new Date(s.next_run).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" }) : "중지"} · 작업 실행기 ${s.worker_enabled ? "활성" : "비활성"}`;
  await loadJobs();
}
async function loadJobs() {
  const rows = await api("/api/jobs");
  $("#jobs").innerHTML = rows.length
    ? rows
        .map(
          (j) =>
            `<details class="job"><summary><span class="tag ${j.status.startsWith("succeeded") ? "good" : ""}">${statusLabels[j.status] || esc(j.status)}</span><span>${esc(j.request.sources?.join(" · "))} · ${esc(j.request.start)} ~ ${esc(j.request.end)}</span><small>${new Date(j.created_at).toLocaleString("ko-KR")}</small></summary><p class="muted">작업 ${j.id} · ${j.tasks.filter((t) => t.status === "succeeded").length}/${j.tasks.length}구간 완료</p>${j.tasks.map((t) => `<div class="result-line"><span>${esc(t.request.source)} ${esc(t.request.date || `${t.request.start} ~ ${t.request.end}`)}</span><span>${esc(t.status)} · 시도 ${t.attempts}</span></div><p class="muted">${esc(t.error || (t.counts ? `신규 ${t.counts.new} / 수정 ${t.counts.revised} / 동일 ${t.counts.unchanged}` : "대기 중"))}</p>`).join("")}${j.tasks.some((t) => t.status === "failed") ? `<button data-retry="${j.id}">실패 구간만 재시도</button>` : ""}${["queued", "running"].includes(j.status) ? `<button data-cancel="${j.id}">작업 취소</button>` : ""}</details>`,
        )
        .join("")
    : empty(
        "갱신 작업이 없습니다",
        "전체·현재 조건·제공처별로 원자료를 요청할 수 있습니다.",
        false,
      );
}
$("#reload-jobs").onclick = protect(loadJobs);
$("#schedule-form").onsubmit = protect(async (e) => {
  e.preventDefault();
  await post("/api/schedule", {
    time: e.target.elements.time.value,
    enabled: e.target.elements.enabled.checked,
  });
  toast("예약 설정을 저장했습니다.");
  await loadUpdates();
});
document.addEventListener(
  "click",
  protect(async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.page || b.dataset.go)
      await navigate(b.dataset.page || b.dataset.go);
    if (b.dataset.source) await refresh([b.dataset.source]);
    if (b.dataset.search) {
      const s = searches[Number(b.dataset.search)],
        f = { ...s.filters };
      if (s.period_mode === "recent") {
        f.end = end;
        f.start = shiftMonth(end, 1 - s.months);
      }
      applyFilter(f);
      pageNo = 1;
      await navigate("market");
    }
    if (b.dataset.calculation) {
      const c = calculations[Number(b.dataset.calculation)];
      parentId = c.id;
      Object.entries(c.input).forEach(([k, v]) => {
        if ($("#calc-form").elements[k]) $("#calc-form").elements[k].value = v;
      });
      $$(".cost-line").forEach((r, i) => {
        const cost = c.input.costs.find((x) => x.name === costNames[i]);
        if (!cost) return;
        r.querySelector(".cost-status").value = cost.status;
        r.querySelector(".cost-amount").value = cost.amount;
        r.querySelector(".cost-rate").value = cost.rate;
        r.querySelector(".cost-currency").value = cost.currency;
        r.querySelector(".cost-recoverable").checked = cost.recoverable;
      });
      lastCalc = null;
      $("#calc-result").innerHTML = empty(
        "저장 입력을 복사했습니다",
        "원래 스냅샷은 보존됩니다. 변경 후 다시 계산하세요.",
        false,
      );
      await navigate("calculator");
    }
    if (b.id === "save-calc" && lastCalc) {
      await post("/api/documents/calculation", {
        name: lastCalc.input.product || "원가 계산",
        input: lastCalc.input,
        parent_id: parentId,
      });
      b.disabled = true;
      toast("당시 입력·환율·수식 버전을 저장했습니다.");
    }
    if (b.id === "commit-import") {
      const r = await post(`/api/import/${previewId}/commit`, {
        accept_valid_only: $("#accept-valid")?.checked || false,
      });
      toast(
        `가져오기 완료 · 신규 ${r.new || 0} / 수정 ${r.revised || 0} / 동일 ${r.unchanged || 0}`,
      );
      b.disabled = true;
      await sources();
    }
    if (b.dataset.retry) {
      await post(`/api/jobs/${b.dataset.retry}/retry`, {});
      await loadJobs();
    }
    if (b.dataset.cancel) {
      await post(`/api/jobs/${b.dataset.cancel}/cancel`, {});
      await loadJobs();
    }
  }),
);
window.addEventListener(
  "popstate",
  protect(async () => {
    applyFilter(Object.fromEntries(new URLSearchParams(location.search)));
    await navigate(location.hash.slice(1) || "overview", false);
  }),
);
setInterval(() => {
  if (page === "updates") protect(loadJobs)();
}, 5000);
protect(async () => {
  applyFilter(Object.fromEntries(new URLSearchParams(location.search)));
  await sources();
  await navigate(location.hash.slice(1) || "overview", false);
})();
