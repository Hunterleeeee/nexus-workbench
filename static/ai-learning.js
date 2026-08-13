// 学习轨道：同一套课程/自测/练习/批改机制服务多条轨道，
// 由页面路径决定当前是哪一条（/projects/embodied vs /projects/ai-learning）。
const LEARNING_TRACK = location.pathname.includes("/embodied") ? "embodied" : "ai-transformation";
const trackQuery = (extra = "") => `track=${encodeURIComponent(LEARNING_TRACK)}${extra ? `&${extra}` : ""}`;

const learnQuery = (selector, root = document) => root.querySelector(selector);
const learnQueryAll = (selector, root = document) => [...root.querySelectorAll(selector)];
const learnEscape = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const learningState = { dashboard: null, draftTimer: 0, draftRevision: 0, draftPromise: null, history: [], historyExpanded: false, openedLessonId: 0, openingLessonId: 0, draftLessonId: 0, draftText: null, explorations: [], exploreKind: "term", exercise: null };

function learningSetStatus(message = "", tone = "") {
  const node = learnQuery("#learning-page-status");
  if (!node) return;
  node.textContent = message;
  node.className = `learning-page-status${tone ? ` ${tone}` : ""}`;
  node.setAttribute("role", tone === "error" ? "alert" : "status");
}

function learningBusy(button, busy, label = "处理中…") {
  if (!button) return;
  if (busy) {
    button.dataset.idleText = button.textContent;
    button.textContent = label;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
  } else {
    button.textContent = button.dataset.idleText || button.textContent;
    delete button.dataset.idleText;
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
}

function renderLearningStats(stats = {}) {
  learnQuery("#stat-streak").textContent = stats.streak ?? 0;
  learnQuery("#stat-completed").textContent = stats.completed ?? 0;
  learnQuery("#stat-weekly").textContent = stats.weekly_completed ?? 0;
  learnQuery("#stat-accuracy").textContent = stats.quiz_accuracy ?? 0;
}

function lessonSourceLabel(lesson = {}) {
  return lesson.source === "personalized" ? "按目标生成" : "内置课程";
}

function aiReviewMarkup(lesson) {
  const fb = lesson.feedback || {};
  if (!fb.reviewed_at) {
    return `<div id="ai-review" class="ai-review empty"><div class="ai-review-head"><strong>AI 点评本节</strong><small>综合练习题作答与本节整体产出，按交付物标准对照</small></div><p class="ai-review-hint">做完至少一道题（或写下本节整体产出）后点这里，会得到：哪些做到了、差在哪（引用你的原话）、一份保留你业务场景的改写版本。答错的自测题还会说明你选的那个选项背后的误解。</p><button id="request-ai-review" class="secondary-button" type="button">让 AI 点评本节产出</button></div>`;
  }
  const list = (items, cls) => (items || []).length ? `<ul class="ai-review-list ${cls}">${items.map((x) => `<li>${learnEscape(x)}</li>`).join("")}</ul>` : "";
  const verdictClass = fb.verdict === "达标" ? "pass" : fb.verdict === "未达标" ? "fail" : "partial";
  return `<div id="ai-review" class="ai-review ${verdictClass}">
    <div class="ai-review-head"><strong>AI 批改：${learnEscape(fb.verdict)}</strong>${fb.score ? `<span class="ai-review-score">${learnEscape(fb.score)}/100</span>` : ""}<small>${learnEscape(formatReviewTime(fb.reviewed_at))}</small></div>
    ${fb.raw_only ? `<p>${learnEscape(fb.rewrite)}</p>` : `
      ${list(fb.met, "met")}
      ${fb.gaps && fb.gaps.length ? `<div class="ai-review-block"><h4>差在哪</h4>${list(fb.gaps, "gaps")}</div>` : ""}
      ${fb.misconception ? `<div class="ai-review-block"><h4>自测这题的误解</h4><p>${learnEscape(fb.misconception)}</p></div>` : ""}
      ${fb.rewrite ? `<details class="ai-review-block"><summary>达标版本改写（保留你的业务场景）</summary><pre class="ai-review-rewrite">${learnEscape(fb.rewrite)}</pre></details>` : ""}
      ${fb.next_question ? `<div class="ai-review-block next"><h4>下一步想一想</h4><p>${learnEscape(fb.next_question)}</p></div>` : ""}`}
    <p class="ai-review-policy">${learnEscape(fb.policy || "")}</p>
    <button id="request-ai-review" class="secondary-button" type="button">重新批改</button>
  </div>`;
}

function formatReviewTime(value) {
  try { return new Date(value).toLocaleString("zh-CN", { hour12: false }); } catch { return ""; }
}

function lessonFeedbackMarkup(lesson, quiz) {
  if (!lesson.completed) return '<div id="quiz-feedback" class="quiz-feedback" role="status" aria-live="polite"></div>';
  const correct = Boolean(lesson.quiz_correct);
  return `<div id="quiz-feedback" class="quiz-feedback visible ${correct ? "correct" : "review"}" role="status"><strong>${correct ? "回答正确" : "这题值得再看一遍"}</strong><br>${learnEscape(quiz.explanation || "课程已完成，重点是把方法用进自己的工作。")}</div>`;
}

// 「当前屏幕上是哪一节课」的唯一事实来源。
//
// 之前所有动作处理器都直接读 learningState.dashboard.today，可屏幕上显示的
// 未必是今天那节——从学习记录点开第一课时，显示的是第一课，而批改、存草稿、
// 保存笔记全都打在今天那节上：批改批的是另一节，批完还把页面刷成今天那节。
// 这不只是跳页，是写错了对象。
function currentLesson() {
  return learningState.currentLesson || learningState.dashboard?.today || {};
}

function isViewingToday() {
  const todayId = Number(learningState.dashboard?.today?.id || 0);
  return !todayId || Number(currentLesson().id || 0) === todayId;
}

function renderTodayLesson(lesson = {}) {
  learningState.currentLesson = lesson || {};
  window.clearTimeout(learningState.draftTimer);
  learningState.draftTimer = 0;
  learningState.draftRevision += 1;
  const host = learnQuery("#today-lesson");
  const content = lesson.content || {};
  const caseItem = content.case || {};
  const practice = content.practice || {};
  const quiz = content.quiz || {};
  const knowledge = Array.isArray(content.knowledge) ? content.knowledge : [];
  const steps = Array.isArray(practice.steps) ? practice.steps : [];
  const completed = Boolean(lesson.completed);
  const selectedAnswer = Number(lesson.quiz_answer);
  host.innerHTML = `
    <header class="lesson-head">
      <div>
        <div class="lesson-meta">
          <span class="lesson-chip">第 ${learnEscape(lesson.day_index || 1)} 课</span>
          <span class="lesson-chip">${learnEscape(lesson.module || "AI 转型")}</span>
          <span class="lesson-chip source">${learnEscape(lessonSourceLabel(lesson))}</span>
          ${completed ? '<span class="lesson-chip complete">今日已完成</span>' : ""}
        </div>
        <h2 id="lesson-title">${learnEscape(lesson.title || content.title || "今日 AI 转型课")}</h2>
        <p class="lesson-objective">${learnEscape(content.objective || "今天完成一个知识、案例、练习与复盘闭环。")}</p>
      </div>
      <div class="lesson-actions">
        <button id="regenerate-lesson" class="secondary-button" type="button" ${completed ? "disabled" : ""} title="按当前学习设置重新生成课程"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M19 8a7 7 0 1 0 1 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M19 4v4h-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>换一节</button>
        <button id="save-lesson-note" class="secondary-button" type="button" ${!completed || lesson.note_artifact_id ? "disabled" : ""} title="${completed ? "保存课程和学习记录" : "完成课程后可保存"}"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 4.5h12v15H6z" stroke="currentColor" stroke-width="1.5"/><path d="M9 8h6M9 11h6M9 14h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>${lesson.note_artifact_id ? "已存笔记" : completed ? "存为笔记" : "完成后存笔记"}</button>
      </div>
    </header>
    <div class="lesson-body">
      <section class="lesson-section" aria-labelledby="knowledge-title">
        <div class="lesson-section-head"><span class="lesson-step">1</span><div><h3 id="knowledge-title">知识</h3><p>理解今天要用的方法</p></div></div>
        <ol class="knowledge-list">${knowledge.map((item) => `<li>${learnEscape(item)}</li>`).join("")}</ol>
      </section>
      <section class="lesson-section" aria-labelledby="case-title">
        <div class="lesson-section-head"><span class="lesson-step">2</span><div><h3 id="case-title">工作案例</h3><p>查看方法在工作中的用法</p></div></div>
        <div class="case-panel"><div class="case-flow">
          <div class="case-item"><span>场景</span><p>${learnEscape(caseItem.situation || "")}</p></div>
          <div class="case-item"><span>做法</span><p>${learnEscape(caseItem.approach || "")}</p></div>
          <div class="case-item"><span>结果</span><p>${learnEscape(caseItem.result || "")}</p></div>
          <div class="case-item"><span>经验</span><p>${learnEscape(caseItem.lesson || "")}</p></div>
        </div>${caseItem.answer ? `<details class="case-answer"><summary>先自己想：这个情境下你会怎么做？想好再展开对答案</summary><p>${learnEscape(caseItem.answer)}</p></details>` : ""}</div>
      </section>
      <section class="lesson-section" aria-labelledby="practice-title">
        <div class="lesson-section-head"><span class="lesson-step">3</span><div><h3 id="practice-title">练习</h3><p>按步骤完成并记录结果</p></div></div>
        <div class="practice-box"><div><h4>${learnEscape(practice.task || "把今天的方法用到一项真实工作中。")}</h4><ol class="practice-steps">${steps.map((step) => `<li>${learnEscape(step)}</li>`).join("")}</ol></div><div class="deliverable-card"><span>交付物</span><strong>${learnEscape(practice.deliverable || "一条可复用的方法记录")}</strong></div></div>
        <div class="exercise-block" id="exercise-block" data-lesson-id="${learnEscape(lesson.id)}">
          <div class="exercise-intro"><div><strong>手上没有现成的工作场景？</strong><p>让 AI 按这节课出一道题，背景给全，你只需要思考和作答，答完再对参考答案。</p></div><button type="button" id="exercise-new" class="secondary-button">出一道题</button></div>
          <div id="exercise-host" class="exercise-host"></div>
          <div class="section-output-block">
            <div class="section-output-head">
              <span class="section-output-title">本节整体产出 <small>选填</small></span>
              <span id="draft-status" class="draft-status ${completed || lesson.status === "in_progress" ? "saved" : ""}" role="status" aria-live="polite">${completed ? "已完成" : lesson.status === "in_progress" ? "已保存" : "自动保存"}</span>
            </div>
            <p class="section-output-note">写不下做题时的想法也行，把今天实际完成的、学到的记在这里；下方「点评本节」会连同练习题一并参考。</p>
            <textarea id="practice-output" rows="5" maxlength="8000" placeholder="写下你实际完成了什么、学到了什么（选填）" ${completed ? "readonly" : ""}>${learnEscape(lesson.practice_output || "")}</textarea>
            ${completed ? "" : `<div class="section-output-actions"><button type="button" id="reset-practice" class="practice-reset" title="清空这一节的练习、复盘和 AI 批改">清空这一节</button></div>`}
          </div>
        </div>
      </section>
      <section class="lesson-section" aria-labelledby="quiz-title">
        <div class="lesson-section-head"><span class="lesson-step">4</span><div><h3 id="quiz-title">自测与复盘</h3><p>选择答案并记录复盘</p></div></div>
        <form id="lesson-complete-form" class="quiz-form" data-lesson-id="${learnEscape(lesson.id)}">
          <p class="quiz-question">${learnEscape(quiz.question || "今天最重要的一点是什么？")}</p>
          <div class="quiz-options" role="radiogroup" aria-label="选择答案">${(quiz.options || []).map((option, index) => `<label class="quiz-option"><input type="radio" name="quiz_answer" value="${index}" ${selectedAnswer === index ? "checked" : ""} ${completed ? "disabled" : ""} /><span>${learnEscape(option)}</span></label>`).join("")}</div>
          ${completed ? `<div class="lesson-complete-summary"><strong>今日课程已完成</strong><p>${learnEscape(lesson.reflection || "没有填写复盘。")}</p></div>` : `<div class="reflection-grid"><label for="lesson-reflection">复盘<textarea id="lesson-reflection" rows="3" maxlength="4000" placeholder="这节课对你的工作有什么用？">${learnEscape(lesson.reflection || "")}</textarea></label><label class="confidence-field" for="lesson-confidence">掌握程度<select id="lesson-confidence">${[1, 2, 3, 4, 5].map((value) => `<option value="${value}" ${Number(lesson.confidence || 3) === value ? "selected" : ""}>${value} · ${["还没懂", "有点模糊", "基本理解", "能用起来", "能教别人"][value - 1]}</option>`).join("")}</select></label></div>`}
          ${lessonFeedbackMarkup(lesson, quiz)}
          ${aiReviewMarkup(lesson)}
          <div class="quiz-actions"><span class="takeaway"><strong>本课要点：</strong>${learnEscape(content.takeaway || "把方法用到真实任务里。")}</span>${completed ? "" : `<button class="primary-button" type="submit">${Number(learningState.dashboard?.today?.id || 0) === Number(lesson.id) ? "完成今日学习" : "记录这一节"}</button>`}</div>
        </form>
      </section>
    </div>`;
  const sourceNode = learnQuery("#learning-source-status");
  const viewingHistory = learningState.openedLessonId && learningState.openedLessonId === lesson.id;
  sourceNode.textContent = viewingHistory
    ? `正在回看 ${lesson.lesson_date} 的课程`
    : completed ? "今日课程已完成" : `${lessonSourceLabel(lesson)} · 约 ${learningState.dashboard?.profile?.daily_minutes || 25} 分钟`;
  sourceNode.className = "learning-status-chip";
  if (viewingHistory) {
    const back = document.createElement("button");
    back.type = "button"; back.id = "back-to-today"; back.className = "secondary-button back-to-today";
    back.textContent = "← 回到今日课程";
    learnQuery("#today-lesson")?.prepend(back);
    back.addEventListener("click", closeHistoryLesson);
  }
  if (lesson.generation_warning) learningSetStatus(lesson.generation_warning);
  bindLessonActions();
}

function renderLearningProfile(profile = {}) {
  learnQuery("#current-role").value = profile.current_role || "";
  learnQuery("#target-role").value = profile.target_role || "";
  learnQuery("#experience").value = profile.experience || "beginner";
  learnQuery("#focus").value = profile.focus || "work-efficiency";
  learnQuery("#learning-goal").value = profile.goal || "";
  learnQuery("#daily-minutes").value = String(profile.daily_minutes || 25);
  const summary = learnQuery("#profile-summary");
  if (profile.current_role || profile.target_role) {
    summary.textContent = `${profile.current_role || "当前岗位"} → ${profile.target_role || "目标岗位"} · 每天 ${profile.daily_minutes || 25} 分钟`;
  } else {
    summary.textContent = "填写岗位和学习目标后，课程会优先使用相关工作场景。";
    learnQuery("#profile-form").classList.remove("hidden");
    learnQuery("#profile-toggle").setAttribute("aria-expanded", "true");
  }
}

function renderLearningPush(dashboard = {}) {
  const profile = dashboard.profile || {};
  const rule = dashboard.automation || {};
  const push = dashboard.push || {};
  learnQuery("#push-enabled").checked = Boolean(profile.daily_push_enabled);
  learnQuery("#push-time").value = profile.push_time || "08:30";
  learnQuery("#push-rule-note").textContent = profile.daily_push_enabled ? `每天 ${profile.push_time || "08:30"} · 本地时间` : "已暂停";
  learnQuery("#push-dot").classList.toggle("ready", Boolean(profile.daily_push_enabled));
  const status = learnQuery("#learning-push-status");
  status.className = `learning-status-chip ${push.ready ? "" : "neutral"}`.trim();
  status.textContent = profile.daily_push_enabled ? `${rule.enabled ? "每日提醒已开启" : "提醒规则待启用"}${push.ready ? " · 浏览器已订阅" : " · 仅工作台内提醒"}` : "每日提醒已暂停";
  learnQuery("#subscribe-push").textContent = push.ready ? "管理当前设备 Push" : "订阅浏览器 Push";
}

function renderLearningPhases(phases = [], today = {}) {
  const activeIndex = Math.min(phases.length - 1, Math.max(0, Math.floor((Number(today.day_index || 1) - 1) / 4)));
  learnQuery("#learning-phases").innerHTML = phases.map((phase, index) => `<li class="learning-phase ${index === activeIndex ? "active" : ""}"><span class="phase-index">${index + 1}</span><div><strong>${learnEscape(phase.title)}</strong><small>${learnEscape(phase.description)}</small><span>${learnEscape(phase.days)}</span></div></li>`).join("");
}

async function openHistoryLesson(lessonId) {
  // data-lesson-id 缺失或不是数字时，原来是一句静默 return——点了完全没反应，
  // 连一条能报上来的线索都没有。现在明确说出来。
  const id = Number(lessonId || 0);
  if (!Number.isFinite(id) || id <= 0) {
    learningSetStatus(`这条记录没有可用的课程编号（拿到的是 ${JSON.stringify(lessonId)}），请刷新页面重试。`, "error");
    return;
  }
  if (learningState.openedLessonId === id) { closeHistoryLesson(); return; }
  if (learningState.openingLessonId) return;   // 连点两下不该发两个请求
  learningState.openingLessonId = id;
  await flushPendingDraft();
  // 先把这一行标成加载中：点击到内容替换之间有一段网络时间，这段时间里
  // 页面上任何地方都不动，看起来就是「点了没反应」。
  const row = learnQuery(`#lesson-history [data-lesson-id="${id}"]`);
  row?.classList.add("loading");
  learningSetStatus(`正在打开第 ${id} 节…`);
  try {
    let body;
    try {
      body = await requestJson(`/api/ai-learning/lessons/${encodeURIComponent(id)}`);
    } catch (error) {
      // 网络类失败重试一次：进程刚起、连接刚被回收这类一次性抖动，
      // 第一下失败第二下就好，没必要让用户自己去点第二次。
      if (error?.code !== "network" && error?.code !== "timeout") throw error;
      await new Promise((resolve) => window.setTimeout(resolve, 600));
      body = await requestJson(`/api/ai-learning/lessons/${encodeURIComponent(id)}`);
    }
    learningState.openedLessonId = id;
    renderTodayLesson(body.lesson || {});
    renderLessonHistory(learningState.history);
    learningSetStatus("");
    learnQuery("#today-lesson")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    // 带上 id 和状态码：这条错误此前只有一句「打开失败」，报上来也没法定位
    // 是课程不存在、后端没起来，还是请求超时。
    const status = error?.status ? `HTTP ${error.status}` : error?.code ? error.code : "";
    learningSetStatus(`打开第 ${id} 节失败：${error.message}${status ? `（${status}）` : ""}`, "error");
  } finally {
    learningState.openingLessonId = 0;
    learnQuery(`#lesson-history [data-lesson-id="${id}"]`)?.classList.remove("loading");
  }
}

function closeHistoryLesson() {
  learningState.openedLessonId = 0;
  renderTodayLesson(learningState.dashboard?.today || {});
  renderLessonHistory(learningState.history);
}

function renderLessonHistory(items = []) {
  const host = learnQuery("#lesson-history");
  learningState.history = items;
  if (!items.length) {
    host.innerHTML = '<div class="history-empty">完成第一节课后，这里会留下学习记录。</div>';
    return;
  }
  host.innerHTML = items.slice(0, learningState.historyExpanded ? 60 : 8).map((item) => `<article class="history-item ${item.completed ? "completed" : item.status === "in_progress" ? "in-progress" : ""} ${learningState.openedLessonId === item.id ? "opened" : ""}" role="button" tabindex="0" data-lesson-id="${learnEscape(item.id)}" title="打开这节课"><span class="history-state" aria-hidden="true"></span><div class="history-copy"><strong>${learnEscape(item.title)}</strong><small>${learnEscape(item.module || "AI 转型")} · ${learnEscape(item.lesson_date)}</small></div><span>${item.completed ? (item.quiz_correct ? "答对" : "已学") : item.status === "in_progress" ? "进行中" : "待完成"}</span></article>`).join("") + (items.length > 8 ? `<button type="button" id="toggle-history" class="history-toggle">${learningState.historyExpanded ? "收起" : `查看全部 ${items.length} 条记录`}</button>` : "");
}

function renderLearningTrack(track) {
  // 标题随轨道走：同一个页面模板要能同时讲清"AI 转型"和"具身智能"。
  if (!track || !track.title) return;
  const title = learnQuery("#learning-title");
  if (title) title.textContent = track.title;
  const subtitle = learnQuery("#learning-subtitle");
  if (subtitle && track.subtitle) subtitle.textContent = `${track.subtitle}（共 ${track.lesson_count || 0} 节）`;
  document.title = `${track.title} · Workbench`;
}

function renderLearningDashboard(dashboard) {
  learningState.dashboard = dashboard;
  renderLearningTrack(dashboard.track);
  renderLearningStats(dashboard.stats || {});
  renderLearningProfile(dashboard.profile || {});
  renderLearningPush(dashboard);
  renderLearningPhases(dashboard.phases || [], dashboard.today || {});
  renderLessonHistory(dashboard.history || []);
  renderTodayLesson(dashboard.today || {});
}

async function loadLearningDashboard() {
  learningSetStatus("");
  try {
    const dashboard = await requestJson(`/api/ai-learning/dashboard?${trackQuery()}`);
    renderLearningDashboard(dashboard);
  } catch (error) {
    learningSetStatus(`读取学习项目失败：${error.message}`, "error");
    learnQuery("#today-lesson").innerHTML = `<div class="lesson-loading" role="alert"><strong>今日课程暂时没有读取成功</strong><p>${learnEscape(error.message)}</p><button id="retry-learning" class="secondary-button" type="button">重新加载</button></div>`;
    learnQuery("#retry-learning")?.addEventListener("click", loadLearningDashboard);
  }
}

async function regenerateTodayLesson(button) {
  if (hasCurrentLessonDraft() && !window.confirm("这节课已有练习或复盘内容。换课后会清空，确定继续吗？")) return;
  window.clearTimeout(learningState.draftTimer);
  learningState.draftTimer = 0;
  learningState.draftRevision += 1;
  if (learningState.draftPromise) {
    try { await learningState.draftPromise; } catch (_error) { /* confirmed replacement can continue */ }
  }
  if (!isViewingToday()) {
    // 「换一节」换的永远是今天那节。在看历史课时点它，今天那节会被悄悄换掉，
    // 而屏幕上显示的还是历史课——看起来像什么都没发生。
    learningSetStatus("「换一节」只对今天的课程生效。先点右侧「学习记录」里今天那一节回到今日课程，再换。", "error");
    learningBusy(button, false);
    return;
  }
  learningBusy(button, true, "生成中…");
  learningSetStatus("正在重新生成今日课程…");
  try {
    const body = await requestJson(`/api/ai-learning/lessons/today/generate?${trackQuery()}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh: true }) });
    learningState.dashboard.today = body.lesson;
    renderTodayLesson(body.lesson);
    learningSetStatus(body.lesson.generation_warning || (body.lesson.source === "personalized" ? "今日课程已按学习设置重新生成。" : "已换为内置课程。配置全局 LLM 后可按学习设置生成。"));
  } catch (error) {
    learningSetStatus(`重新生成失败：${error.message}`, "error");
  } finally {
    learningBusy(button, false);
  }
}

async function saveLessonNote(button, lessonId) {
  learningBusy(button, true, "保存中…");
  try {
    const body = await requestJson(`/api/ai-learning/lessons/${encodeURIComponent(lessonId)}/note`, { method: "POST" });
    learningSetStatus(body.created ? "课程和学习记录已保存到知识库。" : "这节课已经保存过了。", "");
    if (body.lesson) {
      syncLessonEverywhere(body.lesson);
      renderTodayLesson(body.lesson);
    }
  } catch (error) {
    learningSetStatus(`保存笔记失败：${error.message}`, "error");
  } finally {
    learningBusy(button, false);
  }
}

function currentLessonDraftPayload() {
  return {
    practice_output: learnQuery("#practice-output")?.value.trim() || "",
    reflection: learnQuery("#lesson-reflection")?.value.trim() || "",
    confidence: Number(learnQuery("#lesson-confidence")?.value || 3),
  };
}

// 一节课被批改或存草稿之后，它可能同时出现在三个地方：当前视图、今日课程、
// 历史列表。只更新其中一个，另外两个就会显示过期状态。
function syncLessonEverywhere(lesson) {
  if (!lesson?.id) return;
  if (Number(currentLesson().id || 0) === Number(lesson.id)) learningState.currentLesson = lesson;
  if (learningState.dashboard && Number(learningState.dashboard.today?.id || 0) === Number(lesson.id)) {
    learningState.dashboard.today = lesson;
  }
  const history = learningState.dashboard?.history || learningState.history || [];
  const index = history.findIndex((item) => Number(item.id) === Number(lesson.id));
  if (index >= 0) {
    history[index] = lesson;
    renderLessonHistory(history);
  }
}

function hasCurrentLessonDraft() {
  const lesson = currentLesson();
  const draft = currentLessonDraftPayload();
  return lesson.status === "in_progress" || Boolean(draft.practice_output || draft.reflection);
}

function setLessonDraftStatus(message, tone = "") {
  const node = learnQuery("#draft-status");
  if (!node) return;
  node.textContent = message;
  node.className = `draft-status${tone ? ` ${tone}` : ""}`;
}

async function saveCurrentLessonDraft() {
  const pendingId = learningState.draftLessonId;
  const lesson = pendingId && Number(currentLesson().id || 0) !== pendingId
    ? { id: pendingId, completed: false }
    : currentLesson();
  const payload = pendingId && Number(currentLesson().id || 0) !== pendingId
    ? learningState.draftText || currentLessonDraftPayload()
    : currentLessonDraftPayload();
  if (!lesson.id || lesson.completed) return null;
  window.clearTimeout(learningState.draftTimer);
  learningState.draftTimer = 0;
  const revision = learningState.draftRevision;
  setLessonDraftStatus("保存中…", "saving");
  const previous = learningState.draftPromise;
  const operation = (previous ? previous.catch(() => {}) : Promise.resolve()).then(() => requestJson(`/api/ai-learning/lessons/${encodeURIComponent(lesson.id)}/progress`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }));
  learningState.draftPromise = operation;
  try {
    const body = await operation;
    if (body.lesson) {
      syncLessonEverywhere(body.lesson);
    }
    if (revision === learningState.draftRevision) setLessonDraftStatus("已保存", "saved");
    return body;
  } catch (error) {
    if (revision === learningState.draftRevision) setLessonDraftStatus(`保存失败：${error.message}`, "error");
    throw error;
  } finally {
    if (learningState.draftPromise === operation) learningState.draftPromise = null;
  }
}

function scheduleLessonDraftSave() {
  learningState.draftRevision += 1;
  // 记下「你是在哪一节里敲的键」。700ms 的防抖窗口内切换课程时，
  // 定时器回调看到的 currentLesson() 已经是新的那一节了。
  learningState.draftLessonId = Number(currentLesson().id || 0);
  learningState.draftText = currentLessonDraftPayload();
  setLessonDraftStatus("未保存");
  window.clearTimeout(learningState.draftTimer);
  learningState.draftTimer = window.setTimeout(() => saveCurrentLessonDraft().catch(() => {}), 700);
}

async function flushPendingDraft() {
  // 切换课程之前先把待写入的草稿落盘。renderTodayLesson 会 clearTimeout，
  // 不先冲一次的话，700ms 内敲的内容会被静默丢掉。
  if (!learningState.draftTimer) return;
  window.clearTimeout(learningState.draftTimer);
  learningState.draftTimer = 0;
  try { await saveCurrentLessonDraft(); } catch (_error) { /* 冲不掉也不该挡住切换 */ }
}

async function requestAiReview(button, lessonId) {
  const output = (learnQuery("#practice-output")?.value || "").trim();
  const answeredCurrent = Boolean(learningState.exercise?.answered);
  if (!output && !answeredCurrent) {
    learningSetStatus("先做一道题，或写下本节整体产出，AI 才有东西可点评。");
    learnQuery("#exercise-host")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return;
  }
  window.WorkbenchUX?.wbSetBusy?.(button, true, "批改中…");
  try {
    // 先把草稿存下来，避免批改的是上一次保存的旧内容。
    await saveCurrentLessonDraft();
    // 批改要跑一次 LLM，用写入类默认超时（120s），不要用读取的 15s。
    const body = await requestJson(`/api/ai-learning/lessons/${encodeURIComponent(lessonId)}/review`, { method: "POST" });
    // 渲染刚批改的那一节，而不是今天那节——两者不一定是同一节。
    if (body.lesson) syncLessonEverywhere(body.lesson);
    renderTodayLesson(body.lesson || currentLesson());
    learnQuery("#ai-review")?.dispatchEvent(new CustomEvent("review-refreshed"));
    learnQuery("#ai-review")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    learningSetStatus(error.message || "批改失败，请稍后重试。");
  } finally {
    window.WorkbenchUX?.wbSetBusy?.(button, false);
  }
}

function bindLessonActions() {
  const lesson = currentLesson();
  learnQuery("#regenerate-lesson")?.addEventListener("click", (event) => regenerateTodayLesson(event.currentTarget));
  learnQuery("#save-lesson-note")?.addEventListener("click", (event) => saveLessonNote(event.currentTarget, lesson.id));
  [learnQuery("#practice-output"), learnQuery("#lesson-reflection")].forEach((field) => field?.addEventListener("input", scheduleLessonDraftSave));
  learnQuery("#lesson-confidence")?.addEventListener("change", scheduleLessonDraftSave);
  // 联动：练习成果/复盘改了之后，如果已经有 AI 批改，给批改区加一条提示并把"重新批改"按钮置顶，
  // 让用户清楚知道改完练习后批改还停留在旧版本。
  [learnQuery("#practice-output"), learnQuery("#lesson-reflection")].forEach((field) => {
    if (!field) return;
    field.addEventListener("input", () => {
      const review = learnQuery("#ai-review");
      if (!review || review.classList.contains("empty")) return;
      if (review.dataset.stale === "1") return;
      review.dataset.stale = "1";
      const hint = document.createElement("div");
      hint.className = "ai-review-stale";
      hint.innerHTML = '<strong>练习已更新</strong><span>批改还停留在旧版本，点「重新批改」基于最新练习重评。</span>';
      review.querySelector(".ai-review-head")?.after(hint);
      const btn = review.querySelector("#request-ai-review");
      if (btn) { btn.textContent = "重新批改（基于最新练习）"; btn.classList.add("primary-button"); btn.classList.remove("secondary-button"); }
    });
  });
  // 重新批改成功后清掉提示
  learnQuery("#ai-review")?.addEventListener("review-refreshed", () => {
    learnQuery("#ai-review")?.querySelector(".ai-review-stale")?.remove();
    if (learnQuery("#ai-review")) learnQuery("#ai-review").dataset.stale = "";
    const btn = learnQuery("#request-ai-review");
    if (btn) { btn.textContent = "重新批改"; btn.classList.add("secondary-button"); btn.classList.remove("primary-button"); }
  });
  learnQuery("#request-ai-review")?.addEventListener("click", (event) => requestAiReview(event.currentTarget, lesson.id));
  learnQuery("#reset-practice")?.addEventListener("click", async (event) => {
    if (!window.confirm("清空这一节的整体产出、复盘和 AI 批改？课程内容（知识点、案例、题目）会保留。")) return;
    const button = event.currentTarget;
    learningBusy(button, true, "清空中…");
    try {
      // 先取消待写入的草稿，否则 700ms 后它会把刚清掉的内容原样写回去。
      window.clearTimeout(learningState.draftTimer);
      learningState.draftTimer = 0;
      learningState.draftLessonId = 0;
      learningState.draftText = null;
      const body = await requestJson(`/api/ai-learning/lessons/${encodeURIComponent(lesson.id)}/reset-practice`, { method: "POST" });
      if (body.lesson) { syncLessonEverywhere(body.lesson); renderTodayLesson(body.lesson); }
      learningSetStatus("已清空这一节的作答记录。");
    } catch (error) {
      learningSetStatus(`清空失败：${error.message}`, "error");
    } finally {
      learningBusy(button, false);
    }
  });
  learnQuery("#lesson-complete-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const draft = currentLessonDraftPayload();
    const selected = form.querySelector('input[name="quiz_answer"]:checked');
    if (!selected) {
      const feedback = learnQuery("#quiz-feedback");
      feedback.className = "quiz-feedback visible review";
      feedback.textContent = "先选择一个答案，再完成今天的学习。";
      form.querySelector('input[name="quiz_answer"]')?.focus();
      return;
    }
    const button = event.submitter || form.querySelector('button[type="submit"]');
    window.clearTimeout(learningState.draftTimer);
    learningState.draftTimer = 0;
    learningState.draftRevision += 1;
    learningBusy(button, true, "记录中…");
    try {
      const completedId = Number(form.dataset.lessonId);
      const wasToday = Number(learningState.dashboard?.today?.id || 0) === completedId;
      const body = await requestJson(`/api/ai-learning/lessons/${encodeURIComponent(completedId)}/complete`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ quiz_answer: Number(selected.value), ...draft }) });
      learningSetStatus(body.quiz.correct ? "回答正确，这一节已记录。" : "这一节已记录；答案解释已经展开，建议再读一遍。", "");
      // loadLearningDashboard 会把视图重置回今天那节。补完一节旧课之后被弹回
      // 今天，看起来就像刚才那一节没保存上——所以只在补的就是今天那节时才跳。
      await loadLearningDashboard();
      if (!wasToday) {
        learningState.openedLessonId = 0;
        await openHistoryLesson(completedId);
      }
      learnQuery("#today-lesson")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      learningSetStatus(`记录学习失败：${error.message}`, "error");
    } finally {
      learningBusy(button, false);
    }
  });
}

function currentProfilePayload(overrides = {}) {
  const current = learningState.dashboard?.profile || {};
  return {
    current_role: learnQuery("#current-role")?.value.trim() ?? current.current_role ?? "",
    target_role: learnQuery("#target-role")?.value.trim() ?? current.target_role ?? "",
    experience: learnQuery("#experience")?.value || current.experience || "beginner",
    focus: learnQuery("#focus")?.value || current.focus || "work-efficiency",
    goal: learnQuery("#learning-goal")?.value.trim() ?? current.goal ?? "",
    daily_minutes: Number(learnQuery("#daily-minutes")?.value || current.daily_minutes || 25),
    push_time: learnQuery("#push-time")?.value || current.push_time || "08:30",
    daily_push_enabled: learnQuery("#push-enabled")?.checked ?? Boolean(current.daily_push_enabled),
    ...overrides,
  };
}

function setupLearningProfile() {
  learnQuery("#profile-toggle")?.addEventListener("click", (event) => {
    const form = learnQuery("#profile-form");
    const open = form.classList.toggle("hidden") === false;
    event.currentTarget.setAttribute("aria-expanded", String(open));
    if (open) learnQuery("#current-role")?.focus();
  });
  learnQuery("#profile-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    const message = learnQuery("#profile-message");
    learningBusy(button, true, "保存中…");
    message.textContent = "正在保存学习设置…";
    try {
      const body = await requestJson(`/api/ai-learning/profile?${trackQuery()}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(currentProfilePayload()) });
      learningState.dashboard.profile = body.profile;
      learningState.dashboard.automation = body.automation;
      renderLearningProfile(body.profile);
      renderLearningPush(learningState.dashboard);
      message.textContent = "学习设置已保存。点击“换一节”后，新课程会使用这些设置。";
    } catch (error) {
      message.textContent = `保存失败：${error.message}`;
    } finally {
      learningBusy(button, false);
    }
  });
}

function urlBase64ToBytes(value) {
  const padding = "=".repeat((4 - value.length % 4) % 4);
  const base64 = (value + padding).replaceAll("-", "+").replaceAll("_", "/");
  return Uint8Array.from(atob(base64), (character) => character.charCodeAt(0));
}

async function ensureLearningServiceWorker() {
  if (!("serviceWorker" in navigator)) throw new Error("当前浏览器不支持 Service Worker");
  await navigator.serviceWorker.register("/static/sw.js?v=0.3.172", { scope: "/" });
  return navigator.serviceWorker.ready;
}

async function subscribeLearningPush(button) {
  learningBusy(button, true, "订阅中…");
  const message = learnQuery("#push-message");
  try {
    if (!("PushManager" in window) || !("Notification" in window)) throw new Error("当前浏览器不支持 Web Push");
    const config = await requestJson("/api/push/config");
    if (!config.public_key) throw new Error("服务端尚未配置 VAPID 公钥，目前只能收到工作台内提醒");
    const permission = await Notification.requestPermission();
    if (permission !== "granted") throw new Error("浏览器通知权限未授权");
    const registration = await ensureLearningServiceWorker();
    const existing = await registration.pushManager.getSubscription();
    const subscription = existing || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToBytes(config.public_key) });
    const json = subscription.toJSON();
    await requestJson("/api/push/subscriptions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ endpoint: json.endpoint, keys: json.keys || {}, user_agent: navigator.userAgent, quiet_start: "22:00", quiet_end: "07:00", enabled: true }) });
    learningState.dashboard.push = { ...(learningState.dashboard.push || {}), ready: true, subscriptions: Math.max(1, Number(learningState.dashboard.push?.subscriptions || 0)) };
    renderLearningPush(learningState.dashboard);
    message.textContent = "当前浏览器已订阅每日学习提醒。";
  } catch (error) {
    message.textContent = error.message;
  } finally {
    learningBusy(button, false);
  }
}

function setupLearningPush() {
  ensureLearningServiceWorker().catch(() => {});
  learnQuery("#push-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.submitter;
    const message = learnQuery("#push-message");
    learningBusy(button, true, "保存中…");
    try {
      const body = await requestJson(`/api/ai-learning/profile?${trackQuery()}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(currentProfilePayload()) });
      learningState.dashboard.profile = body.profile;
      learningState.dashboard.automation = body.automation;
      renderLearningPush(learningState.dashboard);
      message.textContent = body.profile.daily_push_enabled ? `已设置为每天 ${body.profile.push_time} 提醒。` : "每日学习提醒已暂停。";
    } catch (error) {
      message.textContent = `保存失败：${error.message}`;
    } finally {
      learningBusy(button, false);
    }
  });
  learnQuery("#subscribe-push")?.addEventListener("click", (event) => subscribeLearningPush(event.currentTarget));
}

function setupLearningHistory() {
  // 委托绑定在容器上：条目本身每次渲染都会重建，绑在条目上会随之丢失。
  const host = learnQuery("#lesson-history");
  if (!host) return;
  host.addEventListener("click", (event) => {
    if (event.target.closest("#toggle-history")) {
      learningState.historyExpanded = !learningState.historyExpanded;
      renderLessonHistory(learningState.history);
      return;
    }
    const item = event.target.closest("[data-lesson-id]");
    if (item) openHistoryLesson(item.dataset.lessonId);
  });
  host.addEventListener("keydown", (event) => {
    const item = event.target.closest("[data-lesson-id]");
    if (item && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      openHistoryLesson(item.dataset.lessonId);
    }
  });
}

// ---------------------------------------------------------------------------
// 主动学习：名词 / 最近热点 / 理论
//
// 在这之前学习只有一条被动通道——每天推一节，学完为止。临时想弄懂一个词，
// 只能等课程哪天刚好讲到。
// ---------------------------------------------------------------------------
const EXPLORE_PLACEHOLDER = {
  term: "想弄懂哪个名词？例如：RAG、向量检索、Agent 记忆",
  hotspot: "留空让 AI 从真实热点里挑；也可写方向，例如：国内 AI 应用",
  theory: "想搞懂哪个理论？例如：Scaling Law 为什么有效",
  method: "想学什么方法？例如：Agent 的工具怎么设计、RAG 怎么落地",
};

// 每个 kind 的字段不同，但都遵循「先讲清楚，再说边界」的顺序。
const EXPLORE_FIELDS = [
  ["definition", "一句话说清楚"],
  ["core_idea", "核心主张"],
  ["whats_new", "发生了什么"],
  ["mechanism", "它为什么成立"],
  ["why_it_matters", "为什么重要"],
  ["what_to_learn", "真正值得学的是什么"],
  ["misconceptions", "常见误解"],
  ["skeptic", "被高估的地方"],
  ["evidence", "支持依据"],
  ["steps", "照着做"],
  ["key_choices", "关键取舍"],
  ["common_mistakes", "常见坑"],
  ["boundary", "什么时候不适用"],
  ["in_your_work", "在你的工作里怎么用"],
  ["check", "自查一下你是否真懂"],
];

function exploreValueMarkup(value) {
  if (Array.isArray(value)) return `<ul>${value.map((item) => `<li>${learnEscape(item)}</li>`).join("")}</ul>`;
  return `<p>${learnEscape(value)}</p>`;
}

function renderExploration(exploration) {
  const host = learnQuery("#explore-result");
  if (!host) return;
  const content = exploration?.content || {};
  const rows = EXPLORE_FIELDS
    .filter(([key]) => content[key] && (!Array.isArray(content[key]) || content[key].length))
    .map(([key, label]) => `<div class="explore-field ${key === "boundary" || key === "misconceptions" || key === "skeptic" ? "caution" : ""}"><strong>${label}</strong>${exploreValueMarkup(content[key])}</div>`)
    .join("");
  host.hidden = false;
  host.innerHTML = `<div class="explore-result-head"><strong>${learnEscape(content.title || exploration.title || "小专题")}</strong><button type="button" id="explore-close" class="secondary-button">收起</button></div>${rows || "<p class=\"form-note\">这次没有生成可展示的内容，换个说法再试一次。</p>"}
    <div class="explore-foot"><button type="button" class="secondary-button" data-explore-exercise="${learnEscape(exploration.id)}">就这个出道题考我</button></div>`;
}

function renderExploreHistory(items = []) {
  const host = learnQuery("#explore-history");
  if (!host) return;
  host.innerHTML = items.length
    ? `<div class="explore-history-head">最近问过</div>` + items.slice(0, 6).map((item) => `<button type="button" class="explore-history-item" data-exploration-id="${learnEscape(item.id)}"><span>${learnEscape(item.title || item.topic || "小专题")}</span><small>${learnEscape(item.kind === "term" ? "名词" : item.kind === "hotspot" ? "热点" : "理论")}</small></button>`).join("")
    : "";
}

async function loadExploreHistory() {
  try {
    const body = await requestJson(`/api/ai-learning/explorations?${trackQuery()}&limit=12`);
    learningState.explorations = body.explorations || [];
    renderExploreHistory(learningState.explorations);
  } catch (error) {
    // 历史读不到不该挡住主功能，静默即可——真正的入口是上面的输入框。
  }
}

function setupExplore() {
  const form = learnQuery("#explore-form");
  if (!form) return;
  const input = learnQuery("#explore-topic");
  const message = learnQuery("#explore-message");
  learningState.exploreKind = "term";
  learnQuery(".explore-kinds").addEventListener("click", (event) => {
    const button = event.target.closest("[data-explore-kind]");
    if (!button) return;
    learningState.exploreKind = button.dataset.exploreKind;
    learnQueryAll(".explore-kind").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    input.placeholder = EXPLORE_PLACEHOLDER[learningState.exploreKind] || "";
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type=submit]");
    wbSetBusy(button, true, "正在准备…");
    message.textContent = "";
    try {
      const body = await requestJson(`/api/ai-learning/explorations?${trackQuery()}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: learningState.exploreKind, topic: input.value.trim() }),
      });
      renderExploration(body.exploration || {});
      input.value = "";
      void loadExploreHistory();
    } catch (error) {
      message.textContent = error.message;
    } finally {
      wbSetBusy(button, false);
    }
  });
  learnQuery("#explore-result").addEventListener("click", (event) => {
    if (event.target.closest("#explore-close")) { learnQuery("#explore-result").hidden = true; return; }
    const ask = event.target.closest("[data-explore-exercise]");
    if (ask) {
      const item = (learningState.explorations || []).find((entry) => String(entry.id) === ask.dataset.exploreExercise);
      void createExercise({ topic: item?.title || item?.topic || "" });
    }
  });
  learnQuery("#explore-history").addEventListener("click", (event) => {
    const item = event.target.closest("[data-exploration-id]");
    if (!item) return;
    const found = (learningState.explorations || []).find((entry) => String(entry.id) === item.dataset.explorationId);
    if (found) renderExploration(found);
  });
  void loadExploreHistory();
}

// ---------------------------------------------------------------------------
// AI 出题 → 我作答 → AI 评判
//
// 原来的练习假设「你手上正好有一个真实场景可以拿来练」。大多数时候没有，
// 于是练习框空着，AI 批改也就无从批起。
// ---------------------------------------------------------------------------
function renderExercise(exercise) {
  const host = learnQuery("#exercise-host");
  if (!host || !exercise?.id) return;
  learningState.exercise = exercise;
  const feedback = exercise.feedback || {};
  const criteria = (exercise.criteria || []).map((item) => `<li>${learnEscape(item)}</li>`).join("");
  const answered = Boolean(exercise.answered);
  const list = (items) => (items || []).map((item) => `<li>${learnEscape(item)}</li>`).join("");
  host.innerHTML = `<article class="exercise-card">
    <div class="exercise-question"><span>题目</span><p>${learnEscape(exercise.question || "")}</p></div>
    ${exercise.context ? `<div class="exercise-context"><span>背景</span><p>${learnEscape(exercise.context)}</p></div>` : ""}
    ${criteria ? `<details class="exercise-criteria"><summary>这道题会按什么标准评</summary><ul>${criteria}</ul></details>` : ""}
    <label class="exercise-answer-field" for="exercise-answer"><span>你的答案</span><textarea id="exercise-answer" rows="5" maxlength="4000" placeholder="写下你的判断和理由，不用长，但要说清楚为什么" ${answered ? "readonly" : ""}>${learnEscape(exercise.user_answer || "")}</textarea></label>
    ${answered ? "" : '<div class="exercise-actions"><button type="button" id="exercise-submit" class="primary-button">评判这道题</button></div>'}
    ${answered ? `<div class="exercise-feedback">
      <div class="exercise-score"><strong>${exercise.score >= 0 ? `${learnEscape(exercise.score)} 分` : "已评判"}</strong><span>${learnEscape(feedback.verdict || "")}</span></div>
      ${feedback.hits?.length ? `<div class="exercise-hits"><strong>答到了</strong><ul>${list(feedback.hits)}</ul></div>` : ""}
      ${feedback.misses?.length ? `<div class="exercise-misses"><strong>差在哪</strong><ul>${list(feedback.misses)}</ul></div>` : ""}
      ${feedback.rewrite ? `<div class="exercise-rewrite"><strong>改写成合格答案</strong><p>${learnEscape(feedback.rewrite)}</p></div>` : ""}
      ${exercise.reference_answer ? `<details class="exercise-reference"><summary>参考答案</summary><p>${learnEscape(exercise.reference_answer)}</p></details>` : ""}
      ${feedback.next_step ? `<p class="exercise-next"><strong>下一步：</strong>${learnEscape(feedback.next_step)}</p>` : ""}
    </div>` : ""}
  </article>`;
}

async function createExercise({ lessonId = 0, topic = "" } = {}) {
  const button = learnQuery("#exercise-new");
  const host = learnQuery("#exercise-host");
  if (host) host.innerHTML = '<p class="form-note">正在出题…</p>';
  wbSetBusy(button, true, "出题中…");
  try {
    const body = await requestJson(`/api/ai-learning/exercises?${trackQuery()}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lesson_id: Number(lessonId) || 0, topic }),
    });
    renderExercise(body.exercise || {});
  } catch (error) {
    if (host) host.innerHTML = `<p class="form-note error">${learnEscape(error.message)}</p>`;
  } finally {
    wbSetBusy(button, false);
  }
}

function setupExercises() {
  // 委托绑定到 #today-lesson：课程内容每次重渲染都会重建这些节点。
  const root = learnQuery("#today-lesson");
  if (!root) return;
  root.addEventListener("click", (event) => {
    if (event.target.closest("#exercise-new")) {
      const block = event.target.closest(".exercise-block") || learnQuery("#exercise-block");
      void createExercise({ lessonId: block?.dataset.lessonId || 0 });
      return;
    }
    if (event.target.closest("#exercise-submit")) {
      const answer = learnQuery("#exercise-answer")?.value.trim() || "";
      const exerciseId = learningState.exercise?.id;
      if (!exerciseId) return;
      if (!answer) { learnQuery("#exercise-host").insertAdjacentHTML("beforeend", '<p class="form-note error">先写下你的答案。</p>'); return; }
      const button = event.target.closest("#exercise-submit");
      wbSetBusy(button, true, "评判中…");
      requestJson(`/api/ai-learning/exercises/${encodeURIComponent(exerciseId)}/answer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answer }),
      }).then((body) => renderExercise(body.exercise || {}))
        .catch((error) => { wbSetBusy(button, false); learnQuery("#exercise-host").insertAdjacentHTML("beforeend", `<p class="form-note error">${learnEscape(error.message)}</p>`); });
    }
  });
}

function setupAILearning() {
  setupLearningProfile();
  setupLearningPush();
  setupLearningHistory();
  setupExplore();
  setupExercises();
  loadLearningDashboard();
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setupAILearning);
else setupAILearning();
